"""Evidence strength rubric tests.

The rubric's whole value is that a grade can be explained. So these tests assert
the *factors* as well as the grade — a STRONG that nobody can account for is no
better than a model's opinion, which is what this replaced.

The trap worth naming: corroboration counts **distinct documents**, not distinct
facts. Two extractions of the same value from one export are one observation. If
that check were wrong, a single repetitive config dump would grade STRONG, and
every test below would still pass unless one specifically pins it —
`test_two_facts_from_one_document_are_not_corroboration` is that test.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import UTC, datetime, timedelta

from app.models.enums import EvidenceStrength, GateStatus, VerificationStatus
from app.services import evidence_strength
from app.services.evidence_strength import StrengthFact, assess

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def fact(
    *,
    name: str = "minimum_password_length",
    value: str = "14",
    document_id: str = "doc-a",
    verified: bool = True,
    line: int | None = 4,
    page: int | None = 1,
    age_days: int = 10,
) -> StrengthFact:
    return StrengthFact(
        name=name,
        value=value,
        document_id=document_id,
        verification_status=(
            VerificationStatus.VERIFIED if verified else VerificationStatus.UNVERIFIED
        ),
        page=page,
        line=line,
        observed_at=NOW - timedelta(days=age_days),
    )


def grade(facts: list[StrengthFact], **kwargs: object) -> evidence_strength.StrengthOutcome:
    return assess(
        facts,
        gate_status=kwargs.pop("gate_status", GateStatus.VERIFIED),  # type: ignore[arg-type]
        stale=kwargs.pop("stale", False),  # type: ignore[arg-type]
        has_contradictions=kwargs.pop("has_contradictions", False),  # type: ignore[arg-type]
        freshness_window_days=kwargs.pop("freshness_window_days", 90),  # type: ignore[arg-type]
        now=NOW,
    )


class TestNone:
    def test_no_facts_is_none(self) -> None:
        outcome = grade([])
        assert outcome.grade is EvidenceStrength.NONE
        assert outcome.factors == ["no_supporting_facts"]


class TestWeak:
    def test_an_unverified_fact_is_weak(self) -> None:
        outcome = grade([fact(verified=False)])
        assert outcome.grade is EvidenceStrength.WEAK
        assert "unverified_fact" in outcome.factors

    def test_a_gate_that_did_not_verify_is_weak(self) -> None:
        outcome = grade([fact()], gate_status=GateStatus.UNCERTAIN)
        assert outcome.grade is EvidenceStrength.WEAK
        assert "gate_not_verified" in outcome.factors

    def test_stale_evidence_is_weak(self) -> None:
        outcome = grade([fact()], stale=True)
        assert outcome.grade is EvidenceStrength.WEAK
        assert "stale_evidence" in outcome.factors

    def test_contradictions_are_weak(self) -> None:
        outcome = grade([fact()], has_contradictions=True)
        assert outcome.grade is EvidenceStrength.WEAK
        assert "contradictory_evidence" in outcome.factors

    def test_weakness_reasons_accumulate(self) -> None:
        """An auditor should see everything wrong with the evidence, not the
        first thing wrong with it."""
        outcome = grade([fact(verified=False)], stale=True, gate_status=GateStatus.REJECTED)
        assert set(outcome.factors) >= {
            "unverified_fact",
            "stale_evidence",
            "gate_not_verified",
        }


class TestStrong:
    def test_corroborated_precise_and_fresh_is_strong(self) -> None:
        outcome = grade(
            [
                fact(document_id="doc-a"),
                fact(document_id="doc-b"),
            ]
        )
        assert outcome.grade is EvidenceStrength.STRONG
        assert any(f.startswith("corroborated:") for f in outcome.factors)
        assert "precise_citations" in outcome.factors
        assert "well_inside_freshness_window" in outcome.factors

    def test_two_facts_from_one_document_are_not_corroboration(self) -> None:
        """The trap this rubric is most likely to fall into. One export stating
        the same value twice is one observation."""
        outcome = grade(
            [
                fact(document_id="doc-a"),
                fact(document_id="doc-a"),
            ]
        )
        assert outcome.grade is EvidenceStrength.MODERATE
        assert "single_source" in outcome.factors

    def test_documents_disagreeing_are_not_corroboration(self) -> None:
        """Corroboration is agreement on a *claim*, not merely two documents
        mentioning the same field."""
        outcome = grade(
            [
                fact(document_id="doc-a", value="14"),
                fact(document_id="doc-b", value="16"),
            ]
        )
        assert outcome.grade is EvidenceStrength.MODERATE


class TestModerate:
    def test_a_single_sound_source_is_moderate(self) -> None:
        outcome = grade([fact()])
        assert outcome.grade is EvidenceStrength.MODERATE
        assert "single_source" in outcome.factors

    def test_a_page_only_citation_cannot_be_strong(self) -> None:
        """A page-level citation is checkable but coarse — the auditor still has
        to hunt for the value on the page."""
        outcome = grade(
            [
                fact(document_id="doc-a", line=None, page=3),
                fact(document_id="doc-b", line=None, page=3),
            ]
        )
        assert outcome.grade is EvidenceStrength.MODERATE
        assert "page_level_citation_only" in outcome.factors

    def test_evidence_scraping_into_the_window_cannot_be_strong(self) -> None:
        """Evidence at 80% of a 90-day window is technically fresh and about to
        stop being so."""
        outcome = grade(
            [
                fact(document_id="doc-a", age_days=72),
                fact(document_id="doc-b", age_days=72),
            ]
        )
        assert outcome.grade is EvidenceStrength.MODERATE

    def test_undated_evidence_cannot_be_strong(self) -> None:
        undated = StrengthFact(
            name="n",
            value="14",
            document_id="doc-a",
            verification_status=VerificationStatus.VERIFIED,
            line=2,
            observed_at=None,
        )
        assert grade([undated]).grade is EvidenceStrength.MODERATE

    def test_a_control_with_no_freshness_window_cannot_be_strong(self) -> None:
        """Deliberate: with no window there is no margin to sit inside. Recorded
        as a factor so the ceiling is visible rather than mysterious."""
        outcome = grade(
            [fact(document_id="doc-a"), fact(document_id="doc-b")],
            freshness_window_days=None,
        )
        assert outcome.grade is EvidenceStrength.MODERATE
        assert "no_freshness_window" in outcome.factors


class TestZeroLLMDependency:
    def test_the_module_imports_no_llm_client(self) -> None:
        tree = ast.parse(pathlib.Path(evidence_strength.__file__).read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not [
            name
            for name in imported
            if "llm" in name.lower() or "embedding" in name.lower() or "anthropic" in name.lower()
        ]

    def test_the_grade_is_reproducible(self) -> None:
        facts = [fact(document_id="doc-a"), fact(document_id="doc-b")]
        assert len({grade(facts).grade for _ in range(20)}) == 1
