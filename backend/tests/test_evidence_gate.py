"""Evidence Gate tests (TASK-109).

08_TESTING.md: "Each of the 10 Evidence Gate checks individually, with a fixture
engineered to fail exactly that one check." That is what this file does — every
test below starts from a citation that passes all ten, then breaks precisely one.

The gate takes plain dataclasses, so none of this needs a database. That is the
same property the rule engine has and for the same reason: a verification step
that can only be exercised through the full application is a verification step
nobody will exercise.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.enums import EvaluationMode, GateCheck, GateStatus
from app.services.evidence_gate import FactCitation, GateInput, run_gate

NOW = datetime(2026, 6, 1, tzinfo=UTC)
AUDIT_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
HASH = "a" * 64


def citation(**overrides: object) -> FactCitation:
    """A citation that passes every check. Each test breaks exactly one field."""
    defaults: dict[str, object] = {
        "fact_id": uuid.uuid4(),
        "fact_name": "minimum_password_length",
        "audit_id": AUDIT_ID,
        "document_id": DOCUMENT_ID,
        "cited_document_id": DOCUMENT_ID,
        "page": 3,
        "line": None,
        "cell": None,
        "source_hash": HASH,
        "current_document_hash": HASH,
        "document_page_count": 5,
        "document_line_count": 40,
        "observed_at": NOW - timedelta(days=5),
        "document_exists": True,
        "supports_claim": True,
    }
    defaults.update(overrides)
    return FactCitation(**defaults)  # type: ignore[arg-type]


def gate_input(*citations: FactCitation, **overrides: object) -> GateInput:
    defaults: dict[str, object] = {
        "audit_id": AUDIT_ID,
        "control_definition_id": uuid.uuid4(),
        "evaluation_mode": EvaluationMode.DETERMINISTIC,
        "freshness_window_days": 90,
        "facts": list(citations) or [citation()],
        "has_unresolved_contradiction": False,
        "invented_facts": False,
    }
    defaults.update(overrides)
    return GateInput(**defaults)  # type: ignore[arg-type]


class TestTheHappyPath:
    def test_a_fully_sound_citation_is_verified(self) -> None:
        outcome = run_gate(gate_input(), now=NOW)
        assert outcome.status == GateStatus.VERIFIED
        assert outcome.checks_failed == []


class TestEachCheckIndividually:
    """One test per check, each breaking exactly one thing."""

    def test_1_missing_document_fails_evidence_exists(self) -> None:
        outcome = run_gate(gate_input(citation(document_exists=False)), now=NOW)
        assert GateCheck.EVIDENCE_EXISTS.value in outcome.checks_failed

    def test_2_evidence_from_another_audit_fails_ownership(self) -> None:
        """The cross-tenant case. Evidence belonging to a different audit must
        never silently support this one's result."""
        outcome = run_gate(gate_input(citation(audit_id=uuid.uuid4())), now=NOW)
        assert GateCheck.BELONGS_TO_AUDIT.value in outcome.checks_failed

    def test_3_citation_resolving_to_another_document_fails(self) -> None:
        outcome = run_gate(gate_input(citation(cited_document_id=uuid.uuid4())), now=NOW)
        assert GateCheck.BELONGS_TO_DOCUMENT.value in outcome.checks_failed

    def test_4_page_beyond_the_documents_length_fails_location(self) -> None:
        """The fabricated-citation case named in 00_PRODUCT.md §5.6: page 17 of
        a 5-page document."""
        outcome = run_gate(gate_input(citation(page=17, document_page_count=5)), now=NOW)
        assert GateCheck.LOCATION_VALID.value in outcome.checks_failed

    def test_4b_an_unknown_page_count_cannot_confirm_a_page_citation(self) -> None:
        """ "We could not check" is not "it checks out"."""
        outcome = run_gate(gate_input(citation(document_page_count=None)), now=NOW)
        assert GateCheck.LOCATION_VALID.value in outcome.checks_failed

    def test_4c_a_citation_with_no_location_at_all_fails(self) -> None:
        outcome = run_gate(gate_input(citation(page=None, line=None, cell=None)), now=NOW)
        assert GateCheck.LOCATION_VALID.value in outcome.checks_failed

    def test_5_a_changed_file_fails_the_support_check(self) -> None:
        """08_TESTING.md, evidence-tampering row: the file was altered after
        extraction, so the stored fact can no longer be trusted."""
        outcome = run_gate(gate_input(citation(current_document_hash="b" * 64)), now=NOW)
        assert GateCheck.SUPPORTS_CLAIM.value in outcome.checks_failed

    def test_5b_evidence_that_no_longer_states_the_value_fails(self) -> None:
        outcome = run_gate(gate_input(citation(supports_claim=False)), now=NOW)
        assert GateCheck.SUPPORTS_CLAIM.value in outcome.checks_failed

    def test_5c_an_unrecheckable_citation_is_not_treated_as_verified(self) -> None:
        outcome = run_gate(gate_input(citation(supports_claim=None)), now=NOW)
        assert GateCheck.SUPPORTS_CLAIM.value in outcome.checks_failed

    def test_6_evidence_past_the_freshness_window_fails(self) -> None:
        outcome = run_gate(gate_input(citation(observed_at=NOW - timedelta(days=400))), now=NOW)
        assert GateCheck.FRESH.value in outcome.checks_failed

    def test_6b_no_freshness_window_means_the_check_does_not_fire(self) -> None:
        outcome = run_gate(
            gate_input(
                citation(observed_at=NOW - timedelta(days=4000)),
                freshness_window_days=None,
            ),
            now=NOW,
        )
        assert GateCheck.FRESH.value not in outcome.checks_failed

    def test_7_an_unresolved_contradiction_fails(self) -> None:
        outcome = run_gate(gate_input(has_unresolved_contradiction=True), now=NOW)
        assert GateCheck.NO_CONTRADICTION.value in outcome.checks_failed

    def test_8_a_human_assisted_control_must_not_arrive_mechanically_judged(self) -> None:
        """A routing bug, caught independently of the engine's own refusal to
        evaluate such a control."""
        outcome = run_gate(gate_input(evaluation_mode=EvaluationMode.HUMAN_ASSISTED), now=NOW)
        assert GateCheck.VALID_EVALUATION_METHOD.value in outcome.checks_failed

    def test_9_an_invented_fact_is_rejected_outright(self) -> None:
        """The only check whose failure is REJECTED rather than UNCERTAIN: a
        fabricated fact is not a doubt to flag, it is a result to refuse."""
        outcome = run_gate(gate_input(invented_facts=True), now=NOW)
        assert outcome.status == GateStatus.REJECTED
        assert GateCheck.NO_INVENTED_FACTS.value in outcome.checks_failed

    def test_10_a_result_with_no_citations_is_never_silently_verified(self) -> None:
        """An INSUFFICIENT_EVIDENCE result legitimately arrives with no facts.
        It must be reviewed, not auto-passed."""
        outcome = run_gate(gate_input(facts=[]), now=NOW)
        assert outcome.status != GateStatus.VERIFIED
        assert GateCheck.EVIDENCE_EXISTS.value in outcome.checks_failed


class TestVerdictMapping:
    def test_any_non_fabrication_failure_routes_to_needs_review(self) -> None:
        """01_REQUIREMENTS.md: never silently downgraded, never silently passed —
        surfaced as UNCERTAIN with the specific check named."""
        outcome = run_gate(gate_input(citation(observed_at=NOW - timedelta(days=999))), now=NOW)
        assert outcome.status == GateStatus.UNCERTAIN

    def test_every_check_runs_rather_than_short_circuiting(self) -> None:
        """ "Which checks failed" is the data you would want when demonstrating
        this system's trustworthiness, so the gate does not stop at the first."""
        outcome = run_gate(
            gate_input(
                citation(
                    page=99,
                    document_page_count=5,
                    observed_at=NOW - timedelta(days=999),
                    current_document_hash="b" * 64,
                ),
                has_unresolved_contradiction=True,
            ),
            now=NOW,
        )
        assert {
            GateCheck.LOCATION_VALID.value,
            GateCheck.SUPPORTS_CLAIM.value,
            GateCheck.FRESH.value,
            GateCheck.NO_CONTRADICTION.value,
        } <= set(outcome.checks_failed)

    def test_failed_checks_are_reported_by_name(self) -> None:
        outcome = run_gate(gate_input(citation(page=99)), now=NOW)
        assert outcome.checks_failed == sorted(outcome.checks_failed)
        assert all(isinstance(name, str) for name in outcome.checks_failed)


class TestTheGateIsMechanical:
    def test_the_module_imports_no_llm_client(self) -> None:
        """01_REQUIREMENTS.md § Evidence Gate, Explicitly Forbidden Behavior:
        an LLM-based check here would reintroduce the exact failure mode this
        architecture exists to remove."""
        import ast
        import pathlib

        from app.services import evidence_gate

        tree = ast.parse(pathlib.Path(evidence_gate.__file__).read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        assert not [
            name for name in imported if "llm" in name.lower() or "anthropic" in name.lower()
        ]

    def test_the_verdict_is_reproducible(self) -> None:
        data = gate_input()
        assert len({run_gate(data, now=NOW).status for _ in range(10)}) == 1
