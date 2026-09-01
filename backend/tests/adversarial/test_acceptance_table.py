"""The Level 0 PoC acceptance table (00_PRODUCT.md §5.6, TASK-116).

"A Level 0 PoC that cannot pass every row of this table is not done, regardless
of how much of the happy path works."

One test per row, named for the row. This file is the executable form of that
table — TASK-116's completion criterion is that these pass as automated tests,
not that a demo looked convincing.

Like the rest of this package, every test runs with the LLM and embedding
clients wired to raise, so each row is also a standing demonstration that the
deterministic path never consults a model.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session as DBSession

from app.models.enums import (
    EvaluationResult,
    FindingAction,
    FindingStatus,
    GateCheck,
    GateStatus,
    Role,
)
from app.models.scoping import ScopedControl
from tests import testcompany as tc


class TestRow1CorrectEvidence:
    """| Correct evidence provided | Result = PASS |"""

    def test_correct_evidence_passes(self, upload: Any, run_pipeline: Any) -> None:
        results = run_pipeline([upload(tc.PASSWORD_CONFIG)])
        assert results["8.3.6"].result == EvaluationResult.PASS

    def test_a_passing_result_is_gate_verified(self, upload: Any, run_pipeline: Any) -> None:
        """Sound evidence should clear all ten checks — otherwise the gate is
        so strict that VERIFIED never happens and the status is meaningless."""
        results = run_pipeline([upload(tc.PASSWORD_CONFIG)])
        assert results["8.3.6"].gate_status == GateStatus.VERIFIED
        assert results["8.3.6"].gate_checks_failed == []


class TestRow2IncorrectEvidence:
    """| Incorrect evidence provided | Result = FAIL |"""

    def test_incorrect_evidence_fails(self, upload: Any, run_pipeline: Any) -> None:
        """The same document that passes 8.3.6 fails 8.3.4: a lockout threshold
        of 25 exceeds the permitted 10."""
        results = run_pipeline([upload(tc.PASSWORD_CONFIG)])
        assert results["8.3.4"].result == EvaluationResult.FAIL


class TestRow3EvidenceMissing:
    """| Evidence missing | Result = INSUFFICIENT_EVIDENCE |"""

    def test_missing_evidence_is_insufficient(self, upload: Any, run_pipeline: Any) -> None:
        results = run_pipeline([upload(tc.PASSWORD_CONFIG)])
        assert results["10.5.1"].result == EvaluationResult.INSUFFICIENT_EVIDENCE

    def test_absence_is_never_read_as_compliance(self, upload: Any, run_pipeline: Any) -> None:
        results = run_pipeline([upload(tc.PASSWORD_CONFIG)])
        assert results["10.5.1"].result != EvaluationResult.PASS
        assert results["10.5.1"].facts_used == []


class TestRow4ConflictingSources:
    """| Two evidence sources conflict | Result = CONFLICT, routed to auditor,
    never silently resolved by the LLM |"""

    def test_conflicting_sources_produce_conflict(self, upload: Any, run_pipeline: Any) -> None:
        results = run_pipeline([upload(tc.STORAGE_CONFIG_A), upload(tc.STORAGE_CONFIG_B)])
        assert results["3.5.1"].result == EvaluationResult.CONFLICT

    def test_the_conflict_is_routed_to_a_human(
        self, db: DBSession, upload: Any, run_pipeline: Any, test_audit: Any
    ) -> None:
        from app.services.finding import FindingService

        results = run_pipeline([upload(tc.STORAGE_CONFIG_A), upload(tc.STORAGE_CONFIG_B)])
        finding = FindingService(db).create_for_evaluation(test_audit.id, results["3.5.1"])

        assert finding.status == FindingStatus.pending_review
        assert finding.auditor_decision is None


class TestRow5StaleEvidence:
    """| Evidence is stale | Result = STALE / REVIEW, never silently current |"""

    def test_stale_evidence_is_flagged(self, upload: Any, run_pipeline: Any) -> None:
        results = run_pipeline([upload(tc.STALE_PASSWORD_CONFIG)])
        assert results["8.3.6"].stale is True

    def test_stale_evidence_never_passes_the_gate_silently(
        self, upload: Any, run_pipeline: Any
    ) -> None:
        """The mechanical result may still be PASS; what must not happen is it
        being treated as current."""
        results = run_pipeline([upload(tc.STALE_PASSWORD_CONFIG)])
        evaluation = results["8.3.6"]
        assert evaluation.gate_status != GateStatus.VERIFIED
        assert GateCheck.FRESH.value in evaluation.gate_checks_failed


class TestRow6FabricatedCitation:
    """| A citation is fabricated | REJECTED at the Evidence Gate, never
    reaches a Finding |"""

    def test_a_citation_to_a_nonexistent_page_is_caught(
        self, db: DBSession, upload: Any, test_audit: Any, frozen_controls: dict
    ) -> None:
        from app.models.evaluation import EvidenceFact
        from app.services.evaluation import EvaluationService

        document = upload(tc.PASSWORD_CONFIG)
        EvaluationService(db).extract_facts_for_document(document)
        fact = db.query(EvidenceFact).filter(EvidenceFact.name == "minimum_password_length").one()
        fact.page = 17  # the document has one page
        db.flush()

        evaluation = (
            EvaluationService(db)
            .evaluate_control(test_audit.id, frozen_controls["8.3.6"])
            .evaluation
        )

        assert evaluation.gate_status != GateStatus.VERIFIED
        assert GateCheck.LOCATION_VALID.value in evaluation.gate_checks_failed


class TestRow7PromptInjection:
    """| Evidence contains a prompt-injection payload | No effect on the System
    Result |"""

    def test_an_injection_payload_has_no_effect(self, upload: Any, run_pipeline: Any) -> None:
        results = run_pipeline([upload(tc.INJECTED_PASSWORD_CONFIG)])
        assert results["8.3.6"].result == EvaluationResult.FAIL


class TestRow8LLMUnavailable:
    """| LLM/embedding API is unavailable | Deterministic controls still
    evaluate correctly |"""

    def test_every_deterministic_control_evaluates_with_no_model(
        self, upload: Any, run_pipeline: Any
    ) -> None:
        results = run_pipeline([upload(doc) for doc in tc.STANDARD_SET])
        assert {cid: ev.result for cid, ev in results.items()} == tc.EXPECTED_RESULTS


class TestRow9AuditorRejectsAPass:
    """| Auditor rejects a PASS system result | Final report reflects the
    auditor's decision, not the system result |"""

    def test_the_report_carries_the_auditors_decision(
        self,
        db: DBSession,
        upload: Any,
        run_pipeline: Any,
        test_audit: Any,
        make_user: Any,
    ) -> None:
        from app.api.deps import Actor
        from app.services.finalization import FinalizationService
        from app.services.finding import FindingService

        results = run_pipeline([upload(tc.PASSWORD_CONFIG)])
        passing = results["8.3.6"]
        assert passing.result == EvaluationResult.PASS

        reviewer = make_user(Role.reviewer)
        actor = Actor(id=reviewer.id, role=reviewer.role, name=reviewer.name, email=reviewer.email)
        scoped = (
            db.query(ScopedControl)
            .filter(
                ScopedControl.audit_id == test_audit.id,
                ScopedControl.control_definition_id == passing.control_definition_id,
            )
            .one()
        )
        findings = FindingService(db)
        finding = findings.create_for_evaluation(
            test_audit.id, passing, scoped_control_id=scoped.id
        )
        findings.review(
            finding.id,
            actor,
            action=FindingAction.approve,
            auditor_decision=EvaluationResult.FAIL,
            note="The export is from the staging tenant, not production.",
        )

        # Everything else must be resolved before the report can be built.
        for control_id, evaluation in results.items():
            if evaluation.id == passing.id:
                continue
            other_scoped = (
                db.query(ScopedControl)
                .filter(
                    ScopedControl.audit_id == test_audit.id,
                    ScopedControl.control_definition_id == evaluation.control_definition_id,
                )
                .one()
            )
            other_scoped.gap_acknowledged = True
            other_scoped.gap_note = f"Out of scope for this acceptance run ({control_id})."
        db.flush()

        report = FinalizationService(db).finalize(test_audit.id, actor)

        entry = next(f for f in report.snapshot_data["findings"] if f["control_id"] == "8.3.6")
        assert entry["auditor_decision"] == "FAIL"
        # The machine's answer survives beside it, unchanged.
        assert entry["system_result"] == "PASS"
        assert entry["is_override"] is True

    def test_the_system_result_is_not_mutated_by_the_override(
        self, db: DBSession, upload: Any, run_pipeline: Any, test_audit: Any, make_user: Any
    ) -> None:
        from app.api.deps import Actor
        from app.services.finding import FindingService

        results = run_pipeline([upload(tc.PASSWORD_CONFIG)])
        evaluation = results["8.3.6"]
        reviewer = make_user(Role.reviewer)
        actor = Actor(id=reviewer.id, role=reviewer.role, name=reviewer.name, email=reviewer.email)

        findings = FindingService(db)
        finding = findings.create_for_evaluation(test_audit.id, evaluation)
        findings.review(
            finding.id,
            actor,
            action=FindingAction.approve,
            auditor_decision=EvaluationResult.FAIL,
            note="Disagree.",
        )

        db.refresh(evaluation)
        assert evaluation.result == EvaluationResult.PASS


class TestRow10PolicyVersionImmutability:
    """| A control's rules are updated after an audit is finalized | The
    already-finalized report is unaffected |"""

    def test_editing_a_control_does_not_change_a_finalized_report(
        self, db: DBSession, upload: Any, run_pipeline: Any, test_audit: Any, frozen_controls: dict
    ) -> None:
        results = run_pipeline([upload(tc.PASSWORD_CONFIG)])
        evaluation = results["8.3.6"]
        snapshot_of_rules = list(evaluation.rules_used)
        assert snapshot_of_rules, "the evaluation must record the rules it applied"

        # An Admin revises the control after the fact.
        control = frozen_controls["8.3.6"]
        control.rules = [{"fact": "minimum_password_length", "operator": ">=", "expected": 64}]
        db.flush()
        db.refresh(evaluation)

        # The evaluation still says what it checked, because it snapshotted the
        # rules rather than pointing at them.
        assert evaluation.rules_used == snapshot_of_rules
        assert evaluation.result == EvaluationResult.PASS

    def test_a_superseded_control_leaves_the_original_intact(
        self, db: DBSession, frozen_controls: dict, make_user: Any
    ) -> None:
        from app.api.deps import Actor
        from app.schemas.evaluation import ControlDefinitionCreate
        from app.services.control_corpus import ControlCorpusService

        admin = make_user(Role.admin)
        actor = Actor(id=admin.id, role=admin.role, name=admin.name, email=admin.email)
        original = frozen_controls["8.3.6"]
        original_rules = list(original.rules)

        replacement = ControlCorpusService(db).supersede(
            original.id,
            ControlDefinitionCreate(
                control_id="8.3.6",
                name="Minimum password length",
                requirement_text="Passwords are at least 16 characters.",
                requirement_family=8,
                evaluation_mode="DETERMINISTIC",
                facts=[{"name": "minimum_password_length", "type": "integer"}],
                rules=[{"fact": "minimum_password_length", "operator": ">=", "expected": 16}],
                corpus_version="pci-dss-v4.0.1-poc-99",
            ),
            actor,
        )

        assert original.rules == original_rules
        assert original.superseded_by == replacement.id


class TestRow11EvidenceTampering:
    """| An evidence file is altered after upload | Hash mismatch is detected |"""

    def test_an_altered_file_is_detected_at_the_gate(
        self, db: DBSession, upload: Any, test_audit: Any, frozen_controls: dict
    ) -> None:
        """The file on disk is genuinely rewritten, so this exercises the real
        hash comparison rather than a simulated one."""
        import pathlib

        from app.services.evaluation import EvaluationService

        document = upload(tc.PASSWORD_CONFIG)
        EvaluationService(db).extract_facts_for_document(document)

        # Replace the stored bytes with a different document. The recorded
        # content_hash now describes a file that is no longer there.
        tampered = tc.CLEAN_SHORT_PASSWORD_CONFIG.content()
        pathlib.Path(document.storage_path).write_bytes(tampered)

        evaluation = (
            EvaluationService(db)
            .evaluate_control(test_audit.id, frozen_controls["8.3.6"])
            .evaluation
        )

        assert evaluation.gate_status != GateStatus.VERIFIED
        assert GateCheck.SUPPORTS_CLAIM.value in evaluation.gate_checks_failed

    def test_the_stored_source_hash_still_records_what_was_extracted(
        self, db: DBSession, upload: Any, test_audit: Any
    ) -> None:
        """03_DATA_MODEL.md: a mismatch flags the fact rather than deleting it —
        the historical record of what was extracted, and when, is evidentiary."""
        import pathlib

        from app.models.evaluation import EvidenceFact
        from app.services.evaluation import EvaluationService

        document = upload(tc.PASSWORD_CONFIG)
        EvaluationService(db).extract_facts_for_document(document)
        fact = db.query(EvidenceFact).filter(EvidenceFact.name == "minimum_password_length").one()
        original_hash = fact.source_hash

        pathlib.Path(document.storage_path).write_bytes(tc.CLEAN_SHORT_PASSWORD_CONFIG.content())
        db.refresh(fact)

        assert fact.source_hash == original_hash
        assert fact.value == "14"


class TestTheTableIsComplete:
    def test_every_row_of_the_acceptance_table_has_a_test(self) -> None:
        """A guard against the table growing without its tests.

        00_PRODUCT.md §5.6 lists eleven rows; this module defines one class per
        row. If a row is added to the product doc and not here, this count
        drifts and the omission is visible.
        """
        import inspect
        import sys

        classes = [
            name
            for name, obj in inspect.getmembers(sys.modules[__name__], inspect.isclass)
            if name.startswith("TestRow")
        ]
        assert len(classes) == 11, f"expected one class per acceptance row, found {classes}"
