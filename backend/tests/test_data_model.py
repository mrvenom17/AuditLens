"""Data-layer constraint tests (TASK-005, TASK-007).

08_TESTING.md ranks data integrity fourth in coverage priority, behind the
security boundaries. What is verified here is specifically the set of
constraints that exist to protect the audit trail: uniqueness on assignment,
deletion restriction on anything that would orphan an attribution, and the
database-level backstop on the human-sign-off invariant.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditAssignment
from app.models.enums import EvaluationResult, FindingStatus, Role


class TestAuditAssignment:
    def test_unique_constraint_on_audit_and_user(
        self, db: Session, make_user: Any, make_audit: Any
    ) -> None:
        """TASK-005: the (audit_id, user_id) uniqueness is what stops a
        duplicate assignment row from quietly doubling an audit trail entry."""
        auditor = make_user(Role.auditor)
        audit = make_audit(auditor)  # already assigns the creator

        db.add(AuditAssignment(audit_id=audit.id, user_id=auditor.id))
        with pytest.raises(IntegrityError):
            db.flush()

    def test_distinct_users_may_share_an_audit(
        self, db: Session, make_user: Any, make_audit: Any
    ) -> None:
        first = make_user(Role.auditor)
        second = make_user(Role.auditor)
        audit = make_audit(first)

        db.add(AuditAssignment(audit_id=audit.id, user_id=second.id))
        db.flush()

        count = db.scalar(
            text("SELECT count(*) FROM audit_assignments WHERE audit_id = :eid"),
            {"eid": audit.id},
        )
        assert count == 2


class TestDeletionRestriction:
    """03_DATA_MODEL.md §8.3: ON DELETE RESTRICT for anything that would orphan
    an audit trail. Users are deactivated, never deleted."""

    def test_cannot_delete_user_with_reviewed_findings(
        self, db: Session, make_user: Any, make_audit: Any, make_finding: Any
    ) -> None:
        """TASK-007's named case: deleting a User holding `reviewed_by` records
        must fail rather than silently detach the review."""
        reviewer = make_user(Role.reviewer)
        audit = make_audit(reviewer)
        make_finding(
            audit,
            status=FindingStatus.approved,
            reviewed_by=reviewer,
            auditor_decision=EvaluationResult.PASS,
        )
        db.flush()

        with pytest.raises(IntegrityError):
            db.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": reviewer.id})

    def test_cannot_delete_audit_with_findings(
        self, db: Session, make_user: Any, make_audit: Any, make_finding: Any
    ) -> None:
        """ "Nothing client-related is ever hard-deleted once an audit
        leaves intake" — enforced by the database, not only by the absence of a
        delete endpoint."""
        auditor = make_user(Role.auditor)
        audit = make_audit(auditor)
        make_finding(audit)
        db.flush()

        with pytest.raises(IntegrityError):
            db.execute(text("DELETE FROM audits WHERE id = :eid"), {"eid": audit.id})

    def test_cannot_delete_evidence_document_referenced_by_a_chunk(
        self, db: Session, make_user: Any, make_audit: Any
    ) -> None:
        from app.models.evidence import EvidenceChunk, EvidenceDocument

        auditor = make_user(Role.auditor)
        audit = make_audit(auditor)
        document = EvidenceDocument(
            audit_id=audit.id,
            original_filename="firewall.pdf",
            content_hash="a" * 64,
            storage_path="/data/evidence/aa/" + "a" * 64,
            mime_type="application/pdf",
            size_bytes=1024,
            uploaded_by=auditor.id,
        )
        db.add(document)
        db.flush()
        db.add(
            EvidenceChunk(
                evidence_document_id=document.id,
                chunk_index=0,
                content="chunk text",
                location="page 1",
            )
        )
        db.flush()

        with pytest.raises(IntegrityError):
            db.execute(text("DELETE FROM evidence_documents WHERE id = :did"), {"did": document.id})


class TestFindingApprovalConstraint:
    """The database-level half of ADR-003. The service layer is the primary
    enforcement point; this constraint is what catches a future code path that
    bypasses it — including a direct SQL write during an incident."""

    def test_approved_finding_without_reviewer_is_rejected_by_the_database(
        self,
        db: Session,
        make_user: Any,
        make_audit: Any,
        make_scoped_requirement: Any,
        make_evaluation: Any,
    ) -> None:
        """Attempted as raw SQL, bypassing the service layer entirely — the
        database is the second half of ADR-003's defence, and a constraint that
        only holds when the application remembers to check is not a constraint."""
        auditor = make_user(Role.auditor)
        audit = make_audit(auditor)
        scoped = make_scoped_requirement(audit)
        evaluation = make_evaluation(audit, scoped.control)

        with pytest.raises(IntegrityError, match="ck_approved_requires_reviewer"):
            db.execute(
                text(
                    "INSERT INTO findings "
                    "(id, audit_id, scoped_control_id, control_evaluation_id, "
                    " auditor_decision, status, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :eid, :sid, :ceid, "
                    " 'PASS'::evaluation_result, 'approved', now(), now())"
                ),
                {"eid": audit.id, "sid": scoped.id, "ceid": evaluation.id},
            )

    def test_approved_finding_without_an_auditor_decision_is_rejected(
        self, db: Session, make_user: Any, make_audit: Any, make_finding: Any
    ) -> None:
        """An approved Finding with no `auditor_decision` would appear in a
        report as a determination with no determination in it."""
        reviewer = make_user(Role.reviewer)
        audit = make_audit(reviewer)
        finding = make_finding(audit)

        finding.status = FindingStatus.approved
        finding.reviewed_by = reviewer.id
        finding.auditor_decision = None
        with pytest.raises(IntegrityError, match="ck_approved_requires_reviewer"):
            db.flush()

    def test_draft_and_rejected_findings_need_no_reviewer(
        self, db: Session, make_user: Any, make_audit: Any, make_finding: Any
    ) -> None:
        """The constraint must not over-reach: a rejected Finding is retained
        for the audit trail and a draft has not been reviewed yet."""
        auditor = make_user(Role.auditor)
        audit = make_audit(auditor)

        make_finding(audit, status=FindingStatus.pending_review)
        make_finding(audit, status=FindingStatus.rejected)
        db.flush()  # must not raise


class TestScopedRequirementConstraints:
    def test_one_scoped_row_per_clause_per_audit(
        self, db: Session, make_user: Any, make_audit: Any, make_requirement: Any
    ) -> None:
        from app.models.enums import ScopeSource
        from app.models.scoping import ScopedControl

        auditor = make_user(Role.auditor)
        audit = make_audit(auditor)
        requirement = make_requirement()

        for _ in range(2):
            db.add(
                ScopedControl(
                    audit_id=audit.id,
                    control_definition_id=requirement.id,
                    source=ScopeSource.manual,
                    confirmed=False,
                )
            )
        with pytest.raises(IntegrityError):
            db.flush()

    def test_gap_fields_default_to_not_acknowledged(
        self, db: Session, make_user: Any, make_audit: Any, make_scoped_requirement: Any
    ) -> None:
        """ADR-011 item 3 — a gap is never acknowledged implicitly."""
        auditor = make_user(Role.auditor)
        scoped = make_scoped_requirement(make_audit(auditor))
        assert scoped.gap_acknowledged is False
        assert scoped.gap_note is None


class TestCorpusVersioning:
    def test_same_clause_may_exist_under_two_corpus_versions(
        self, db: Session, make_requirement: Any
    ) -> None:
        """03_DATA_MODEL.md → ControlDefinition lifecycle: a corpus update inserts
        new rows rather than mutating existing ones, so a past audit still
        cites the text that was in effect when it ran."""
        from app.models.corpus import ControlDefinition

        make_requirement(control_id="1.2.1")
        db.add(
            ControlDefinition(
                control_id="1.2.1",
                requirement_family=1,
                name="Same clause, newer corpus",
                requirement_text="Revised text.",
                corpus_version="v4.0.2-test",
            )
        )
        db.flush()  # must not raise — uniqueness is per (control_id, corpus_version)

    def test_duplicate_clause_within_one_version_is_rejected(
        self, db: Session, make_requirement: Any
    ) -> None:
        from app.models.corpus import ControlDefinition

        make_requirement(control_id="1.2.1")
        db.add(
            ControlDefinition(
                control_id="1.2.1",
                requirement_family=1,
                name="Duplicate",
                requirement_text="Duplicate text.",
                corpus_version="v4.0.1-test",
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()


class TestReportImmutabilityShape:
    def test_one_report_per_audit(self, db: Session, make_user: Any, make_audit: Any) -> None:
        """Finalize is terminal; a second call returns 409 rather than creating
        a second Report. The unique constraint is the backstop."""
        from app.models.finding import Report

        reviewer = make_user(Role.reviewer)
        audit = make_audit(reviewer)

        for _ in range(2):
            db.add(
                Report(
                    audit_id=audit.id,
                    snapshot_data={"findings": []},
                    generated_by=reviewer.id,
                )
            )
        with pytest.raises(IntegrityError):
            db.flush()
