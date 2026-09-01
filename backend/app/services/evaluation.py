"""Evaluation orchestration — facts in, gated ControlEvaluation out (TASK-110).

This service is the retrofit's centre of gravity. It replaces the path where an
LLM read evidence and proposed a compliance status with one where:

    extracted facts → rule_engine (mechanical) → evidence_gate (mechanical)
                    → ControlEvaluation → Finding (pending human review)

The LLM is not in that chain anywhere. `genai_service` is called afterwards, on
an already-written result, purely to render prose — and its output lands in
`Finding.ai_explanation`, a column the engine never reads.

This module coordinates; it does not decide. The verdict comes from
`rule_engine.evaluate`, the gate verdict from `evidence_gate.run_gate`, and this
file's job is to gather their inputs from the database and persist their outputs
in one transaction (03_DATA_MODEL.md §8.3 — an evaluation must never exist with
no gate verdict, even transiently).
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.models.audit import Audit
from app.models.corpus import ControlDefinition
from app.models.enums import ApplicabilityStatus, FindingStatus, VerificationStatus
from app.models.evaluation import ControlEvaluation, EvidenceFact
from app.models.evidence import EvidenceDocument
from app.models.scoping import ScopedControl
from app.pipelines import extraction
from app.repositories.evaluation import ControlEvaluationRepository, EvidenceFactRepository
from app.services import applicability as applicability_engine
from app.services import (
    evidence_gate,
    evidence_strength,
    fact_service,
    file_storage,
    rule_engine,
)
from app.services.audit import AuditService
from app.services.evidence_gate import FactCitation, GateInput

if TYPE_CHECKING:
    from app.api.deps import Actor

logger = logging.getLogger(__name__)


@dataclass
class EvaluationSummary:
    evaluation: ControlEvaluation
    control: ControlDefinition
    # The scope row this control was evaluated under. Carried so the Finding can
    # be linked back to it — an unlinked Finding would not appear in the
    # finalization blocker check, and an unreviewed item that does not block
    # sign-off defeats the product's central claim that a human saw every one.
    scoped_control_id: uuid.UUID | None = None


class EvaluationService:
    def __init__(self, db: DBSession) -> None:
        self._db = db
        self._facts = EvidenceFactRepository(db)
        self._evaluations = ControlEvaluationRepository(db)
        self._audits = AuditService(db)

    # --- Fact extraction ------------------------------------------------------

    def extract_facts_for_document(self, document: EvidenceDocument) -> int:
        """Extract every fact the audit's scoped controls declare, from one
        document. Called by the worker after extraction completes.

        Scoping the search to the audit's own confirmed controls is what keeps
        this bounded — a document is never scanned for facts belonging to a
        control nobody put in scope.
        """
        controls = self._scoped_controls(document.audit_id)
        if not controls:
            return 0

        sections = self._sections_for(document)
        if not sections:
            return 0

        created = 0
        for control in controls:
            schema = [f for f in control.facts if isinstance(f, dict)]
            if not schema:
                continue
            for candidate in fact_service.extract_facts(sections, schema):
                self._facts.create(
                    audit_id=document.audit_id,
                    control_definition_id=control.id,
                    document_id=document.id,
                    name=candidate.name,
                    value=candidate.value,
                    value_type=candidate.value_type,
                    page=candidate.page,
                    line=candidate.line,
                    cell=candidate.cell,
                    source_hash=document.content_hash,
                    observed_at=candidate.observed_at,
                    extractor_version=candidate.extractor_version,
                    verification_status=candidate.verification_status,
                )
                created += 1

        logger.info(
            "facts.extracted document=%s audit=%s count=%d",
            document.id,
            document.audit_id,
            created,
        )
        return created

    # --- Evaluation -----------------------------------------------------------

    def evaluate_audit(
        self, audit_id: uuid.UUID, actor: Actor | None = None
    ) -> list[EvaluationSummary]:
        """Run the engine and the gate for every scoped control on this audit.

        Always produces a row per control, including for controls with no
        evidence — INSUFFICIENT_EVIDENCE is a real result and a silent gap would
        be worse than a truthful "nothing to check this against yet".
        """
        if actor is not None:
            self._audits.get(audit_id, actor)

        summaries: list[EvaluationSummary] = []
        for scoped_id, control in self._scoped_controls_with_ids(audit_id):
            summaries.append(self.evaluate_control(audit_id, control, scoped_control_id=scoped_id))
        return summaries

    def evaluate_control(
        self,
        audit_id: uuid.UUID,
        control: ControlDefinition,
        *,
        scoped_control_id: uuid.UUID | None = None,
    ) -> EvaluationSummary:
        """Evaluate one control, gate the result, and persist both together."""
        facts = self._facts.list_for_control(audit_id, control.id)
        now = datetime.now(UTC)

        # Applicability is re-derived here rather than read from the ScopedControl
        # stamp. The company profile may have been corrected since scoping, and
        # this is a pure function over data already loaded — so trusting a stale
        # stamp would buy nothing and could evaluate a control that no longer
        # applies.
        audit = self._db.get(Audit, audit_id)
        applicability = applicability_engine.evaluate(
            [c for c in control.applicability_conditions if isinstance(c, dict)],
            self._profile_for(audit),
        )
        applicable = applicability.status is not ApplicabilityStatus.NOT_APPLICABLE

        outcome = rule_engine.evaluate(
            rules=[r for r in control.rules if isinstance(r, dict)],
            facts=list(facts),
            evaluation_mode=control.evaluation_mode,
            # STRUCTURED asks whether the control's declared facts are present and
            # well-formed, so its input is the fact schema rather than the rules.
            required_facts=[
                str(f["name"]) for f in control.facts if isinstance(f, dict) and "name" in f
            ],
            freshness_window_days=control.freshness_window_days,
            applicable=applicable,
            now=now,
        )

        gate_outcome = evidence_gate.run_gate(
            self._build_gate_input(audit_id, control, outcome, applicable=applicable), now=now
        )

        # Graded from the *full* fact set, not `outcome.facts_used` — the engine
        # keeps one fact per rule even when several documents agree, so
        # corroboration is invisible there and STRONG would be unreachable.
        strength = evidence_strength.assess(
            [
                evidence_strength.StrengthFact(
                    name=f.name,
                    value=f.value,
                    document_id=str(f.document_id),
                    verification_status=f.verification_status,
                    page=f.page,
                    line=f.line,
                    cell=f.cell,
                    observed_at=f.observed_at,
                )
                for f in facts
            ],
            gate_status=gate_outcome.status,
            stale=outcome.stale,
            has_contradictions=bool(outcome.contradictions),
            freshness_window_days=control.freshness_window_days,
            now=now,
        )

        evaluation = self._evaluations.create(
            audit_id=audit_id,
            control_definition_id=control.id,
            result=outcome.result,
            evaluation_mode=control.evaluation_mode,
            facts_used=[f.id for f in outcome.facts_used],
            # Snapshot, not a reference: a later edit to this control's rules
            # must not change what this row claims to have checked.
            rules_used=[dict(r) for r in control.rules if isinstance(r, dict)],
            evidence_locations=self._citations(outcome.facts_used),
            contradictions=outcome.contradictions or None,
            stale=outcome.stale,
            gate_status=gate_outcome.status,
            gate_checks_failed=gate_outcome.checks_failed,
            evidence_strength=strength.grade,
            strength_factors=strength.factors,
            engine_version=outcome.engine_version,
            # The deterministic path involves no model at any point. Logged as a
            # monitorable invariant rather than an assumption
            # (02_ARCHITECTURE.md §7.8).
            llm_involved=False,
        )

        logger.info(
            "evaluation.created audit=%s control=%s result=%s gate=%s strength=%s "
            "applicable=%s engine_version=%s llm_involved=false",
            audit_id,
            control.control_id,
            evaluation.result.value,
            evaluation.gate_status.value,
            evaluation.evidence_strength.value,
            applicable,
            evaluation.engine_version,
        )
        return EvaluationSummary(
            evaluation=evaluation, control=control, scoped_control_id=scoped_control_id
        )

    # --- Gate input assembly --------------------------------------------------

    def _build_gate_input(
        self,
        audit_id: uuid.UUID,
        control: ControlDefinition,
        outcome: rule_engine.EvaluationOutcome,
        *,
        applicable: bool = True,
    ) -> GateInput:
        """Resolve each used fact against the document it cites.

        Every value handed to the gate is a stored column or a recomputed
        document property. Nothing here asks a model anything.
        """
        citations: list[FactCitation] = []
        for fact in outcome.facts_used:
            document = self._db.get(EvidenceDocument, fact.document_id)
            sections = self._sections_for(document) if document is not None else []
            page_count, line_count = _document_extent(sections)

            citations.append(
                FactCitation(
                    fact_id=fact.id,
                    fact_name=fact.name,
                    audit_id=fact.audit_id,
                    document_id=fact.document_id,
                    # The document actually resolved from the fact's own FK. A
                    # mismatch would mean the citation pointed somewhere else.
                    cited_document_id=document.id if document is not None else None,
                    page=fact.page,
                    line=fact.line,
                    cell=fact.cell,
                    source_hash=fact.source_hash,
                    current_document_hash=document.content_hash if document is not None else None,
                    document_page_count=page_count,
                    document_line_count=line_count,
                    observed_at=fact.observed_at,
                    document_exists=document is not None,
                    supports_claim=(
                        fact_service.recheck(
                            sections,
                            name=fact.name,
                            value=fact.value or "",
                            value_type=fact.value_type,
                            page=fact.page,
                            line=fact.line,
                            cell=fact.cell,
                        )
                        if sections
                        else None
                    ),
                )
            )

        return GateInput(
            audit_id=audit_id,
            control_definition_id=control.id,
            evaluation_mode=control.evaluation_mode,
            freshness_window_days=control.freshness_window_days,
            applicable=applicable,
            facts=citations,
            has_unresolved_contradiction=bool(outcome.contradictions),
            invented_facts=any(
                f.verification_status != VerificationStatus.VERIFIED for f in outcome.facts_used
            ),
        )

    # --- Helpers --------------------------------------------------------------

    @staticmethod
    def _profile_for(audit: Audit | None) -> dict[str, Any]:
        """The company profile applicability runs against.

        `entity_type` and `merchant_level` live in their own columns for
        historical reasons; they are folded in here so a condition can reference
        them by name without the profile duplicating storage.
        """
        if audit is None:
            return {}
        profile = dict(audit.company_profile or {})
        profile["entity_type"] = audit.entity_type.value
        if audit.merchant_level is not None:
            profile["merchant_level"] = audit.merchant_level.value
        return profile

    def _scoped_controls(self, audit_id: uuid.UUID) -> list[ControlDefinition]:
        return [control for _, control in self._scoped_controls_with_ids(audit_id)]

    def _scoped_controls_with_ids(
        self, audit_id: uuid.UUID
    ) -> list[tuple[uuid.UUID, ControlDefinition]]:
        """The audit's confirmed controls, with their scope row ids.

        Unconfirmed scope is not evaluated: confirmation is the human gate, and
        evaluating past it would make the gate decorative.
        """
        rows = self._db.execute(
            select(ScopedControl.id, ControlDefinition)
            .join(ControlDefinition, ScopedControl.control_definition_id == ControlDefinition.id)
            .where(
                ScopedControl.audit_id == audit_id,
                ScopedControl.confirmed.is_(True),
            )
        ).all()
        return [(row[0], row[1]) for row in rows]

    def _sections_for(self, document: EvidenceDocument) -> list[extraction.ExtractedSection]:
        """Re-read the stored file and re-extract its sections.

        Deliberately re-read from storage rather than from `extracted_text`: the
        gate's job includes noticing that the file on disk is no longer what was
        extracted, and trusting the cached text would defeat that.
        """
        try:
            content = file_storage.read_stored(document.storage_path)
        except (OSError, ValueError):
            logger.warning("Gate could not read stored file for document %s", document.id)
            return []
        result = extraction.extract(content, document.mime_type)
        return result.sections if result.success else []

    @staticmethod
    def _citations(facts: list[EvidenceFact]) -> list[dict[str, object]]:
        return [
            {
                "fact": fact.name,
                "value": fact.value,
                "evidence_document_id": str(fact.document_id),
                # The SHA-256 the fact was extracted under. Carried here so it
                # reaches the frozen report — 00_PRODUCT.md specifies "evidence
                # hashes and locations", and a citation a reader cannot verify
                # the integrity of is only half a citation.
                "source_hash": fact.source_hash,
                "location": fact.location_label,
                "page": fact.page,
                "line": fact.line,
                "cell": fact.cell,
            }
            for fact in facts
        ]


def _document_extent(
    sections: list[extraction.ExtractedSection],
) -> tuple[int | None, int | None]:
    """How many pages and lines the document actually has.

    This is what makes the fabricated-citation check real: a citation to page 17
    is compared against the number of pages the document genuinely yielded, so
    "page 17 of a 5-page PDF" fails structurally rather than being taken on
    trust.
    """
    pages = [
        int(match.group(1))
        for section in sections
        if (match := re.match(r"^page\s+(\d+)", section.location, re.IGNORECASE))
    ]
    page_count = max(pages) if pages else None
    line_count = max((len(s.text.splitlines()) for s in sections), default=None)
    return page_count, line_count


def result_requires_review(evaluation: ControlEvaluation) -> bool:
    """Every result requires human review at Level 0 (Evidence Gate check 10).

    Kept as a named function rather than a literal `True` so the day someone
    proposes auto-approving a class of result, the change has to happen here,
    in one place, deliberately.
    """
    return True


def initial_finding_status(evaluation: ControlEvaluation) -> FindingStatus:
    """A Finding always starts awaiting a human, including a gate-REJECTED one.

    A REJECTED evaluation is surfaced, not hidden: 01_REQUIREMENTS.md requires
    the auditor be told "the system could not verify this evaluation, manual
    assessment required" rather than it being silently dropped or defaulted to
    any particular compliance status.
    """
    return FindingStatus.pending_review


__all__ = [
    "EvaluationService",
    "EvaluationSummary",
    "initial_finding_status",
    "result_requires_review",
]
