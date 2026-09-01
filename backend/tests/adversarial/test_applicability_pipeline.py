"""Applicability end to end, with no model available.

Two things are proved here that unit tests cannot.

**`NOT_APPLICABLE` is reachable.** Before this work nothing anywhere passed
`applicable=False` to the rule engine, so one of the six documented result states
was dead code — the system could describe it and never produce it.

**A NOT_APPLICABLE control does not trip the Evidence Gate.** Such a control
correctly has no evidence, and the gate's no-facts branch would otherwise fail
`EVIDENCE_EXISTS`, mark it UNCERTAIN, and surface it to the auditor under the
loudest "the system could not verify this" banner in the UI. A false alarm on
every correctly-excluded control would train auditors to ignore the one flag that
matters.

Like everything in this package, these run with the LLM and embedding clients
wired to raise — so they also demonstrate that applicability never consults a
model.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session as DBSession

from app.models.enums import (
    ApplicabilityStatus,
    EvaluationResult,
    GateStatus,
    ScopeSource,
)
from app.models.scoping import ScopedControl
from app.services.evaluation import EvaluationService
from app.services.scoping import ScopingService

# 12.9.1 is "Additional requirement for service providers", so it cannot apply to
# a merchant. That is a rule, not a judgement call — exactly what belongs in the
# deterministic path.
SERVICE_PROVIDER_ONLY = "12.9.1"


def merchant_profile() -> dict[str, Any]:
    """A fully-answered merchant profile. Every question the frozen conditions
    ask has an answer, so nothing lands on UNDETERMINED for the wrong reason."""
    return {
        "industry": "retail_in_person",
        "environment": "on_premises",
        "systems": ["pos_terminals"],
        "data_types": ["pan"],
        "cloud_providers": [],
        "stores_cardholder_data": True,
        "transmits_cardholder_data": False,
        "outsources_card_processing": False,
    }


class TestNotApplicableIsReachable:
    def test_the_engine_produces_not_applicable_for_an_excluded_control(
        self, db: DBSession, test_audit: Any, frozen_controls: dict, make_user: Any
    ) -> None:
        """The state that was previously impossible to produce."""
        from app.models.corpus import ControlDefinition

        test_audit.company_profile = merchant_profile()
        control = ControlDefinition(
            control_id=SERVICE_PROVIDER_ONLY,
            requirement_family=12,
            name="Service-provider-only requirement",
            requirement_text="Additional requirement for service providers.",
            evaluation_mode="HUMAN_ASSISTED",
            applicability_conditions=[
                {"fact": "entity_type", "operator": "==", "expected": "service_provider"}
            ],
            corpus_version="pci-dss-v4.0.1-poc-2",
        )
        db.add(control)
        db.flush()

        summary = EvaluationService(db).evaluate_control(test_audit.id, control)

        assert summary.evaluation.result is EvaluationResult.NOT_APPLICABLE

    def test_a_not_applicable_control_does_not_trip_the_gate(
        self, db: DBSession, test_audit: Any, make_user: Any
    ) -> None:
        """The false-alarm bug. A correctly-excluded control has no evidence, and
        that must not read as "could not verify"."""
        from app.models.corpus import ControlDefinition

        test_audit.company_profile = merchant_profile()
        control = ControlDefinition(
            control_id="12.9.2",
            requirement_family=12,
            name="Another service-provider-only requirement",
            requirement_text="Additional requirement for service providers.",
            evaluation_mode="HUMAN_ASSISTED",
            applicability_conditions=[
                {"fact": "entity_type", "operator": "==", "expected": "service_provider"}
            ],
            corpus_version="pci-dss-v4.0.1-poc-2",
        )
        db.add(control)
        db.flush()

        evaluation = EvaluationService(db).evaluate_control(test_audit.id, control).evaluation

        assert evaluation.result is EvaluationResult.NOT_APPLICABLE
        assert evaluation.gate_status is GateStatus.VERIFIED
        assert evaluation.gate_checks_failed == []

    def test_an_applicable_control_with_no_evidence_still_warns(
        self, db: DBSession, test_audit: Any, frozen_controls: dict
    ) -> None:
        """The counterpart — the exemption above must not have disabled the
        no-evidence check generally."""
        test_audit.company_profile = merchant_profile()
        db.flush()

        evaluation = (
            EvaluationService(db)
            .evaluate_control(test_audit.id, frozen_controls["8.3.6"])
            .evaluation
        )

        assert evaluation.result is EvaluationResult.INSUFFICIENT_EVIDENCE
        assert evaluation.gate_status is not GateStatus.VERIFIED


class TestDeterministicScoping:
    def test_scoping_excludes_service_provider_controls_from_a_merchant_audit(
        self, db: DBSession, test_audit: Any, frozen_controls: dict, make_user: Any
    ) -> None:
        """The engine reaches its verdict with no model available at all."""
        from app.corpus.loader import ingest

        test_audit.company_profile = merchant_profile()
        db.flush()
        ingest(db)

        from app.repositories.scoping import CorpusRepository

        corpus = CorpusRepository(db).list_by_version("pci-dss-v4.0.1-poc-2")
        ScopingService(db).apply_applicability(test_audit, corpus)

        excluded = {
            row.control.control_id
            for row in db.query(ScopedControl).filter(ScopedControl.audit_id == test_audit.id).all()
            if row.applicability_status is ApplicabilityStatus.NOT_APPLICABLE
        }

        # Every "additional requirement for service providers" control.
        assert {"12.9.1", "12.9.2", "12.4.1", "11.4.6"} <= excluded

    def test_exclusions_record_why(
        self, db: DBSession, test_audit: Any, frozen_controls: dict
    ) -> None:
        from app.corpus.loader import ingest
        from app.repositories.scoping import CorpusRepository

        test_audit.company_profile = merchant_profile()
        db.flush()
        ingest(db)
        corpus = CorpusRepository(db).list_by_version("pci-dss-v4.0.1-poc-2")
        ScopingService(db).apply_applicability(test_audit, corpus)

        row = next(
            r
            for r in db.query(ScopedControl).filter(ScopedControl.audit_id == test_audit.id).all()
            if r.control.control_id == SERVICE_PROVIDER_ONLY
        )

        assert row.source is ScopeSource.deterministic
        assert row.applicability_evidence
        assert row.applicability_evidence[0]["fact"] == "entity_type"
        assert "Not applicable" in (row.rationale or "")

    def test_an_unanswered_profile_excludes_nothing_it_cannot_justify(
        self, db: DBSession, test_audit: Any, frozen_controls: dict
    ) -> None:
        """The safety property, end to end.

        `entity_type` lives in its own column and is always answered, so
        conditions on it stay decidable even with a blank profile — those
        exclusions are correct and are expected here. What must *not* happen is
        an exclusion justified by a question nobody answered.
        """
        from app.corpus.loader import ingest
        from app.repositories.scoping import CorpusRepository

        test_audit.company_profile = {}
        db.flush()
        ingest(db)
        corpus = CorpusRepository(db).list_by_version("pci-dss-v4.0.1-poc-2")
        determinations = ScopingService(db).apply_applicability(test_audit, corpus)

        # Conditions on unanswered attributes stay undetermined...
        wireless = determinations["11.2.1"]
        assert wireless.status is ApplicabilityStatus.UNDETERMINED
        assert wireless.unanswered == ["systems.wireless_network"]

        storage = determinations["3.5.1"]
        assert storage.status is ApplicabilityStatus.UNDETERMINED

        # ...and none of them was written as an exclusion.
        written = {
            r.control.control_id
            for r in db.query(ScopedControl).filter(ScopedControl.audit_id == test_audit.id).all()
            if r.source is ScopeSource.deterministic
            and r.applicability_status is ApplicabilityStatus.NOT_APPLICABLE
        }
        assert "11.2.1" not in written
        assert "3.5.1" not in written

        # Only the always-answerable condition produced exclusions.
        assert "12.9.1" in written
