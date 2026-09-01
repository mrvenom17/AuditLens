"""Evidence strength — how well-supported a result is (Gap 4).

The result says *what* the evidence showed. Strength says *how much weight it can
bear*. An auditor reviewing a queue needs both: a PASS resting on one undated
page-level citation is a different proposition from a PASS three independent
exports agree on.

Two design commitments:

* **Ordered gates, not a weighted score.** A number produced by summing weights
  and comparing against a threshold cannot be explained to an auditor, let alone
  to a regulator asking why a control was graded as it was. Each grade here is a
  named set of conditions, and `strength_factors` records which ones fired.
* **No model, ever.** Every input is provenance the system already holds. An
  LLM-scored strength would smuggle judgment back into a pipeline built to
  exclude it — and it would be judgment about how much to trust the evidence,
  which is precisely the auditor's job.

This module imports only `app.models.enums`, and an AST import-boundary test
holds it to the same zero-LLM bar as the rule engine and the gate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.models.enums import EvidenceStrength, GateStatus, VerificationStatus

logger = logging.getLogger(__name__)

# A control's evidence has to sit comfortably inside its freshness window to
# count as STRONG, not merely scrape in. Evidence at 95% of a 90-day window is
# technically fresh and about to stop being so.
FRESHNESS_MARGIN = 0.5


@dataclass(frozen=True)
class StrengthFact:
    """One fact as the rubric sees it.

    Deliberately not `EvidenceFact` — keeping this a plain dataclass is what lets
    the rubric be tested exhaustively with no database.
    """

    name: str
    value: str | None
    document_id: str
    verification_status: VerificationStatus
    page: int | None = None
    line: int | None = None
    cell: str | None = None
    observed_at: datetime | None = None

    @property
    def is_precise(self) -> bool:
        """Whether the citation points at a line or a cell rather than a whole page.

        A page-level citation is checkable but coarse; an auditor still has to
        scan the page. Cell and line citations point at the value itself.
        """
        return self.line is not None or self.cell is not None


@dataclass
class StrengthOutcome:
    grade: EvidenceStrength
    factors: list[str] = field(default_factory=list)


def assess(
    facts: list[StrengthFact],
    *,
    gate_status: GateStatus,
    stale: bool,
    has_contradictions: bool,
    freshness_window_days: int | None,
    now: datetime | None = None,
) -> StrengthOutcome:
    """Grade the evidence behind one evaluation.

    `facts` must be **every** fact extracted for the control, not the engine's
    `facts_used`. The engine records one fact per rule even when several
    documents agree — corroboration is invisible in that list, so grading from it
    would make STRONG unreachable.
    """
    now = now or datetime.now(UTC)
    factors: list[str] = []

    if not facts:
        return StrengthOutcome(grade=EvidenceStrength.NONE, factors=["no_supporting_facts"])

    # --- WEAK: anything that undermines the evidence outright ---------------
    if gate_status is not GateStatus.VERIFIED:
        factors.append("gate_not_verified")
    if any(f.verification_status is not VerificationStatus.VERIFIED for f in facts):
        factors.append("unverified_fact")
    if stale:
        factors.append("stale_evidence")
    if has_contradictions:
        factors.append("contradictory_evidence")

    if factors:
        return StrengthOutcome(grade=EvidenceStrength.WEAK, factors=factors)

    # --- Everything below is at least MODERATE ------------------------------
    corroborated = _corroborated_facts(facts)
    if corroborated:
        factors.append(f"corroborated:{','.join(corroborated)}")

    precise = all(f.is_precise for f in facts)
    if precise:
        factors.append("precise_citations")

    comfortable = _comfortably_fresh(facts, freshness_window_days, now)
    if comfortable:
        factors.append("well_inside_freshness_window")
    elif freshness_window_days is None:
        # Without a window there is no margin to be inside, so the control cannot
        # reach STRONG. Deliberate: all eight Level 0 deterministic controls
        # declare a window, so this costs nothing today and stays honest if a
        # control without one is ever added.
        factors.append("no_freshness_window")

    if corroborated and precise and comfortable:
        return StrengthOutcome(grade=EvidenceStrength.STRONG, factors=factors)

    if not corroborated:
        factors.append("single_source")
    if not precise:
        factors.append("page_level_citation_only")

    return StrengthOutcome(grade=EvidenceStrength.MODERATE, factors=factors)


def _corroborated_facts(facts: list[StrengthFact]) -> list[str]:
    """Fact names that two or more *independent documents* agree on.

    Independence is by `document_id`. Two facts with the same name and value
    extracted twice from one document are one observation, not two — counting
    them as corroboration would let a single repetitive export grade STRONG.
    """
    by_claim: dict[tuple[str, str | None], set[str]] = {}
    for fact in facts:
        by_claim.setdefault((fact.name, fact.value), set()).add(fact.document_id)
    return sorted({name for (name, _), docs in by_claim.items() if len(docs) > 1})


def _comfortably_fresh(facts: list[StrengthFact], window_days: int | None, now: datetime) -> bool:
    """Whether every dated fact sits well inside the control's freshness window.

    Undated evidence cannot be shown to be fresh, so it never counts as
    comfortable — the same reasoning that stops the rule engine treating an
    undated document as current.
    """
    if window_days is None:
        return False
    threshold = timedelta(days=window_days * FRESHNESS_MARGIN)
    for fact in facts:
        if fact.observed_at is None:
            return False
        observed = fact.observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        if now - observed > threshold:
            return False
    return True
