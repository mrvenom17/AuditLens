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

from app.models.engagement import EngagementAssignment
from app.models.enums import ComplianceStatus, FindingStatus, Role


class TestEngagementAssignment:
    def test_unique_constraint_on_engagement_and_user(
        self, db: Session, make_user: Any, make_engagement: Any
    ) -> None:
        """TASK-005: the (engagement_id, user_id) uniqueness is what stops a
        duplicate assignment row from quietly doubling an audit trail entry."""
        auditor = make_user(Role.auditor)
        engagement = make_engagement(auditor)  # already assigns the creator

        db.add(EngagementAssignment(engagement_id=engagement.id, user_id=auditor.id))
        with pytest.raises(IntegrityError):
            db.flush()

    def test_distinct_users_may_share_an_engagement(
        self, db: Session, make_user: Any, make_engagement: Any
    ) -> None:
        first = make_user(Role.auditor)
        second = make_user(Role.auditor)
        engagement = make_engagement(first)

        db.add(EngagementAssignment(engagement_id=engagement.id, user_id=second.id))
        db.flush()

        count = db.scalar(
            text("SELECT count(*) FROM engagement_assignments WHERE engagement_id = :eid"),
            {"eid": engagement.id},
        )
        assert count == 2


class TestDeletionRestriction:
    """03_DATA_MODEL.md §8.3: ON DELETE RESTRICT for anything that would orphan
    an audit trail. Users are deactivated, never deleted."""

    def test_cannot_delete_user_with_reviewed_findings(
        self, db: Session, make_user: Any, make_engagement: Any, make_finding: Any
    ) -> None:
        """TASK-007's named case: deleting a User holding `reviewed_by` records
        must fail rather than silently detach the review."""
        reviewer = make_user(Role.reviewer)
        engagement = make_engagement(reviewer)
        make_finding(
            engagement,
            status=FindingStatus.approved,
            reviewed_by=reviewer,
            final_status=ComplianceStatus.satisfied,
        )
        db.flush()

        with pytest.raises(IntegrityError):
            db.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": reviewer.id})

    def test_cannot_delete_engagement_with_findings(
        self, db: Session, make_user: Any, make_engagement: Any, make_finding: Any
    ) -> None:
        """ "Nothing client-related is ever hard-deleted once an engagement
        leaves intake" — enforced by the database, not only by the absence of a
        delete endpoint."""
        auditor = make_user(Role.auditor)
        engagement = make_engagement(auditor)
        make_finding(engagement)
        db.flush()

        with pytest.raises(IntegrityError):
            db.execute(text("DELETE FROM engagements WHERE id = :eid"), {"eid": engagement.id})

    def test_cannot_delete_evidence_document_referenced_by_a_chunk(
        self, db: Session, make_user: Any, make_engagement: Any
    ) -> None:
        from app.models.evidence import EvidenceChunk, EvidenceDocument

        auditor = make_user(Role.auditor)
        engagement = make_engagement(auditor)
        document = EvidenceDocument(
            engagement_id=engagement.id,
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
        self, db: Session, make_user: Any, make_engagement: Any, make_scoped_requirement: Any
    ) -> None:
        auditor = make_user(Role.auditor)
        engagement = make_engagement(auditor)
        scoped = make_scoped_requirement(engagement)

        with pytest.raises(IntegrityError, match="ck_approved_requires_reviewer"):
            db.execute(
                text(
                    "INSERT INTO findings "
                    "(id, engagement_id, scoped_requirement_id, citations, evidence_document_ids, "
                    " needs_manual_review, status, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :eid, :sid, '[]'::jsonb, '{}', false, 'approved', "
                    " now(), now())"
                ),
                {"eid": engagement.id, "sid": scoped.id},
            )

    def test_approved_finding_without_final_status_is_rejected(
        self, db: Session, make_user: Any, make_engagement: Any, make_finding: Any
    ) -> None:
        """An approved Finding with no `final_status` would appear in a report
        as a determination with no determination in it."""
        reviewer = make_user(Role.reviewer)
        engagement = make_engagement(reviewer)
        finding = make_finding(engagement)

        finding.status = FindingStatus.approved
        finding.reviewed_by = reviewer.id
        finding.final_status = None
        with pytest.raises(IntegrityError, match="ck_approved_requires_reviewer"):
            db.flush()

    def test_draft_and_rejected_findings_need_no_reviewer(
        self, db: Session, make_user: Any, make_engagement: Any, make_finding: Any
    ) -> None:
        """The constraint must not over-reach: a rejected Finding is retained
        for the audit trail and a draft has not been reviewed yet."""
        auditor = make_user(Role.auditor)
        engagement = make_engagement(auditor)

        make_finding(engagement, status=FindingStatus.draft)
        make_finding(engagement, status=FindingStatus.rejected)
        db.flush()  # must not raise


class TestScopedRequirementConstraints:
    def test_one_scoped_row_per_clause_per_engagement(
        self, db: Session, make_user: Any, make_engagement: Any, make_requirement: Any
    ) -> None:
        from app.models.enums import ScopeSource
        from app.models.scoping import ScopedRequirement

        auditor = make_user(Role.auditor)
        engagement = make_engagement(auditor)
        requirement = make_requirement()

        for _ in range(2):
            db.add(
                ScopedRequirement(
                    engagement_id=engagement.id,
                    pci_requirement_id=requirement.id,
                    source=ScopeSource.manual,
                    confirmed=False,
                )
            )
        with pytest.raises(IntegrityError):
            db.flush()

    def test_gap_fields_default_to_not_acknowledged(
        self, db: Session, make_user: Any, make_engagement: Any, make_scoped_requirement: Any
    ) -> None:
        """ADR-011 item 3 — a gap is never acknowledged implicitly."""
        auditor = make_user(Role.auditor)
        scoped = make_scoped_requirement(make_engagement(auditor))
        assert scoped.gap_acknowledged is False
        assert scoped.gap_note is None


class TestCorpusVersioning:
    def test_same_clause_may_exist_under_two_corpus_versions(
        self, db: Session, make_requirement: Any
    ) -> None:
        """03_DATA_MODEL.md → PCIRequirement lifecycle: a corpus update inserts
        new rows rather than mutating existing ones, so a past engagement still
        cites the text that was in effect when it ran."""
        from app.models.corpus import PCIRequirement

        make_requirement(clause_id="1.2.1")
        db.add(
            PCIRequirement(
                clause_id="1.2.1",
                requirement_family=1,
                title="Same clause, newer corpus",
                full_text="Revised text.",
                corpus_version="v4.0.2-test",
            )
        )
        db.flush()  # must not raise — uniqueness is per (clause_id, corpus_version)

    def test_duplicate_clause_within_one_version_is_rejected(
        self, db: Session, make_requirement: Any
    ) -> None:
        from app.models.corpus import PCIRequirement

        make_requirement(clause_id="1.2.1")
        db.add(
            PCIRequirement(
                clause_id="1.2.1",
                requirement_family=1,
                title="Duplicate",
                full_text="Duplicate text.",
                corpus_version="v4.0.1-test",
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()


class TestReportImmutabilityShape:
    def test_one_report_per_engagement(
        self, db: Session, make_user: Any, make_engagement: Any
    ) -> None:
        """Finalize is terminal; a second call returns 409 rather than creating
        a second Report. The unique constraint is the backstop."""
        from app.models.finding import Report

        reviewer = make_user(Role.reviewer)
        engagement = make_engagement(reviewer)

        for _ in range(2):
            db.add(
                Report(
                    engagement_id=engagement.id,
                    snapshot_data={"findings": []},
                    generated_by=reviewer.id,
                )
            )
        with pytest.raises(IntegrityError):
            db.flush()
