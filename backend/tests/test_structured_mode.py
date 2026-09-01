"""STRUCTURED evaluation mode tests.

STRUCTURED asks a different question from DETERMINISTIC. DETERMINISTIC asks
"is the configured value acceptable"; STRUCTURED asks "is the required
information present and well-formed at all". A password minimum of 4 is a
perfectly good *structured* answer and a bad *deterministic* one.

Before this existed, `evaluation_mode` was read twice in `rule_engine.evaluate`
and never again — STRUCTURED fell through the same operator loop as
DETERMINISTIC, so the third mode was a label. `TestTheModeIsGenuinelyDistinct`
is what stops it quietly becoming a label again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import EvaluationMode, EvaluationResult, FactValueType
from app.services.rule_engine import evaluate

REQUIRED = ["incident_response_plan_owner", "incident_response_plan_approved_date"]


@dataclass
class StubFact:
    name: str
    value: str | None
    value_type: FactValueType = FactValueType.string
    observed_at: datetime | None = None
    id: str = "fact-1"


def structured(facts: list[StubFact], required: list[str] | None = None):  # type: ignore[no-untyped-def]
    return evaluate(
        rules=[],
        facts=facts,
        evaluation_mode=EvaluationMode.STRUCTURED,
        required_facts=required if required is not None else REQUIRED,
    )


class TestPresenceAndCompleteness:
    def test_every_required_field_present_passes(self) -> None:
        outcome = structured(
            [
                StubFact("incident_response_plan_owner", "Jane Okafor"),
                StubFact(
                    "incident_response_plan_approved_date",
                    "2026-01-15",
                    FactValueType.date,
                ),
            ]
        )
        assert outcome.result is EvaluationResult.PASS

    def test_some_present_is_partial(self) -> None:
        outcome = structured([StubFact("incident_response_plan_owner", "Jane Okafor")])
        assert outcome.result is EvaluationResult.PARTIAL

    def test_none_present_is_insufficient_evidence(self) -> None:
        assert structured([]).result is EvaluationResult.INSUFFICIENT_EVIDENCE

    def test_no_required_fields_is_not_a_pass(self) -> None:
        """Unreachable in practice — the DB CHECK and the loader both refuse a
        STRUCTURED control with no declared facts. Checking nothing still never
        demonstrates anything."""
        assert structured([], required=[]).result is EvaluationResult.INSUFFICIENT_EVIDENCE


class TestMalformedValues:
    def test_a_value_that_will_not_parse_fails(self) -> None:
        """FAIL rather than INSUFFICIENT_EVIDENCE: the evidence was supplied and
        is structurally wrong, which the document itself demonstrates. That is a
        finding, not a gap."""
        outcome = structured(
            [
                StubFact("incident_response_plan_owner", "Jane Okafor"),
                StubFact(
                    "incident_response_plan_approved_date", "last Tuesday", FactValueType.date
                ),
            ]
        )
        assert outcome.result is EvaluationResult.FAIL

    def test_the_failure_names_the_field_and_the_expected_type(self) -> None:
        outcome = structured(
            [
                StubFact("incident_response_plan_owner", "Jane Okafor"),
                StubFact(
                    "incident_response_plan_approved_date", "last Tuesday", FactValueType.date
                ),
            ]
        )
        detail = next(o.detail for o in outcome.rule_outcomes if o.result is EvaluationResult.FAIL)
        assert "incident_response_plan_approved_date" in detail
        assert "date" in detail


class TestContradiction:
    def test_documents_disagreeing_on_a_required_field_conflict(self) -> None:
        outcome = structured(
            [
                StubFact("incident_response_plan_owner", "Jane Okafor"),
                StubFact("incident_response_plan_owner", "Sam Ito"),
                StubFact("incident_response_plan_approved_date", "2026-01-15", FactValueType.date),
            ]
        )
        assert outcome.result is EvaluationResult.CONFLICT
        assert outcome.contradictions[0]["fact"] == "incident_response_plan_owner"


class TestTheModeIsGenuinelyDistinct:
    def test_structured_ignores_rules_entirely(self) -> None:
        """The same facts and rules that FAIL deterministically PASS
        structurally, because the value is present and readable. If STRUCTURED
        ever falls back through the operator loop again, this flips."""
        facts = [StubFact("minimum_password_length", "4", FactValueType.integer)]
        rules = [{"fact": "minimum_password_length", "operator": ">=", "expected": 12}]

        deterministic = evaluate(
            rules=rules, facts=facts, evaluation_mode=EvaluationMode.DETERMINISTIC
        )
        as_structured = evaluate(
            rules=rules,
            facts=facts,
            evaluation_mode=EvaluationMode.STRUCTURED,
            required_facts=["minimum_password_length"],
        )

        assert deterministic.result is EvaluationResult.FAIL
        assert as_structured.result is EvaluationResult.PASS

    def test_outcomes_are_labelled_as_presence_checks(self) -> None:
        outcome = structured([StubFact("incident_response_plan_owner", "Jane Okafor")])
        assert {o.operator for o in outcome.rule_outcomes} == {"REQUIRED"}


class TestAuthoringGuards:
    def test_the_database_refuses_a_structured_control_with_no_facts(
        self, db: Session, make_requirement: Any
    ) -> None:
        """Belt and braces behind the loader check. Such a control would check
        nothing and return INSUFFICIENT_EVIDENCE forever, reading as missing
        evidence rather than as the authoring mistake it is."""
        with pytest.raises(IntegrityError, match="ck_structured_requires_facts"):
            make_requirement(
                control_id="12.10.9",
                family=12,
                evaluation_mode=EvaluationMode.STRUCTURED,
                facts=[],
            )
            db.flush()

    def test_the_shipped_corpus_has_a_structured_control(self) -> None:
        """The mode ships exercised rather than as a dead capability."""
        from app.corpus.loader import load_corpus_file

        data = load_corpus_file()
        structured_rows = [r for r in data["requirements"] if r["evaluation_mode"] == "STRUCTURED"]
        assert structured_rows
        assert all(r["facts"] for r in structured_rows)
