"""Audit finalization tests (TASK-021).

07_TASKS.md names TASK-021 a high-risk task and requires tests for: unresolved
drafts blocking with the correct list, non-Reviewer receiving 403 regardless of
UI state, and an already-finalized audit returning 409 rather than a
duplicate Report.

05_SECURITY.md §10.11 and 04_API_CONTRACT.md both single out the 403 case on
this endpoint as requiring an explicit dedicated test before any change to this
path is considered complete. `TestFinalizeIsReviewerOnly` is that test.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.api.deps import Actor
from app.errors import ForbiddenError
from app.models.audit import AuditAssignment
from app.models.enums import AuditStatus, EvaluationResult, FindingStatus, Role
from app.models.finding import Report
from app.services.finalization import FinalizationService

PASSWORD = "correct-horse-battery-staple"


def login(client: TestClient, user: Any) -> None:
    assert (
        client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD}).status_code
        == 200
    )


def actor_for(user: Any) -> Actor:
    return Actor(id=user.id, role=user.role, name=user.name, email=user.email)


@pytest.fixture
def ready_audit(
    db: DBSession, make_user: Any, make_audit: Any, make_finding: Any
) -> dict[str, Any]:
    """An audit with everything approved, assigned to both an Auditor and
    a Reviewer, sitting in `in_progress` and ready to finalize."""
    auditor = make_user(Role.auditor, password=PASSWORD)
    reviewer = make_user(Role.reviewer, password=PASSWORD)
    audit = make_audit(auditor, status=AuditStatus.in_progress)
    db.add(AuditAssignment(audit_id=audit.id, user_id=reviewer.id))
    db.flush()

    finding = make_finding(
        audit,
        status=FindingStatus.approved,
        reviewed_by=reviewer,
        auditor_decision=EvaluationResult.PASS,
    )
    return {
        "auditor": auditor,
        "reviewer": reviewer,
        "audit": audit,
        "finding": finding,
    }


class TestFinalizeIsReviewerOnly:
    """05_SECURITY.md §10.11 / 04_API_CONTRACT.md: "403 FORBIDDEN if caller is
    not a reviewer — this must be checked even if an Auditor somehow gets a
    finalize button rendered client-side"."""

    def test_auditor_gets_403_even_when_everything_is_approved(
        self, api_client: TestClient, db: DBSession, ready_audit: dict[str, Any]
    ) -> None:
        """01_REQUIREMENTS.md acceptance criterion: "Given the same request made
        by an Auditor (non-Reviewer), the response is 403 regardless of Finding
        state." The audit here is fully ready — nothing but the role stops
        it."""
        setup = ready_audit
        login(api_client, setup["auditor"])

        response = api_client.post(f"/api/audits/{setup['audit'].id}/finalize")

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"

        db.refresh(setup["audit"])
        assert setup["audit"].status == AuditStatus.in_progress
        assert setup["audit"].finalized_by is None
        assert db.scalar(select(func.count()).select_from(Report)) == 0

    def test_auditor_gets_403_when_findings_are_unresolved(
        self, api_client: TestClient, make_user: Any, make_audit: Any, make_finding: Any
    ) -> None:
        """ "Regardless of Finding state" cuts both ways: an Auditor must get the
        same flat 403 whether the audit is ready or not. A 409 here would
        tell them how close the audit is to being signed off, which is a
        readiness disclosure to someone with no authority over it."""
        auditor = make_user(Role.auditor, password=PASSWORD)
        audit = make_audit(auditor, status=AuditStatus.in_progress)
        make_finding(audit, status=FindingStatus.pending_review)
        login(api_client, auditor)

        response = api_client.post(f"/api/audits/{audit.id}/finalize")

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"

    def test_admin_cannot_finalize(
        self,
        api_client: TestClient,
        db: DBSession,
        make_user: Any,
        ready_audit: dict[str, Any],
    ) -> None:
        """00_PRODUCT.md §5.3: an Admin "cannot finalize audits unless also
        a Reviewer — sign-off authority is a role property, not an escalation
        path." An Admin sees every audit, so this is the case where
        visibility must not be mistaken for authority."""
        setup = ready_audit
        admin = make_user(Role.admin, password=PASSWORD)
        login(api_client, admin)

        response = api_client.post(f"/api/audits/{setup['audit'].id}/finalize")

        assert response.status_code == 403
        assert db.scalar(select(func.count()).select_from(Report)) == 0

    def test_unauthenticated_finalize_is_rejected(
        self, api_client: TestClient, ready_audit: dict[str, Any]
    ) -> None:
        response = api_client.post(f"/api/audits/{ready_audit['audit'].id}/finalize")
        assert response.status_code == 401

    def test_service_layer_rejects_a_non_reviewer_directly(
        self, db: DBSession, ready_audit: dict[str, Any]
    ) -> None:
        """08_TESTING.md § Security Tests requires attempting the bypass "via
        direct service-layer call, not just via the API, to catch any bypass
        path". The role check lives in the service, so a caller that never
        touched a route is refused too."""
        setup = ready_audit
        service = FinalizationService(db)

        with pytest.raises(ForbiddenError):
            service.finalize(setup["audit"].id, actor_for(setup["auditor"]))

        assert db.scalar(select(func.count()).select_from(Report)) == 0

    def test_unassigned_reviewer_may_still_finalize(
        self,
        api_client: TestClient,
        db: DBSession,
        make_user: Any,
        make_audit: Any,
        make_finding: Any,
    ) -> None:
        """A Reviewer's authority is firm-wide (03_DATA_MODEL.md §8.2: Reviewers
        see all audits), so finalization must not additionally require an
        assignment row — that would make sign-off depend on bookkeeping."""
        auditor = make_user(Role.auditor, password=PASSWORD)
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        audit = make_audit(auditor, status=AuditStatus.in_progress)
        make_finding(
            audit,
            status=FindingStatus.approved,
            reviewed_by=reviewer,
            auditor_decision=EvaluationResult.PASS,
        )
        login(api_client, reviewer)

        response = api_client.post(f"/api/audits/{audit.id}/finalize")

        assert response.status_code == 200, response.text


class TestUnresolvedFindingsBlockFinalization:
    def test_two_unresolved_drafts_return_409_listing_both(
        self,
        api_client: TestClient,
        db: DBSession,
        make_user: Any,
        make_audit: Any,
        make_finding: Any,
        make_scoped_requirement: Any,
        make_requirement: Any,
    ) -> None:
        """01_REQUIREMENTS.md acceptance criterion: "Given 2 unresolved draft
        Findings, when finalize is attempted, the response is 409 listing those
        2 items, and Audit.status remains unchanged"."""
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        audit = make_audit(reviewer, status=AuditStatus.in_progress)

        blocked_clauses = []
        for control_id in ("1.2.1", "3.3.1"):
            requirement = make_requirement(control_id=control_id, family=int(control_id[0]))
            scoped = make_scoped_requirement(audit, requirement=requirement)
            make_finding(audit, status=FindingStatus.pending_review, scoped_control=scoped)
            blocked_clauses.append(control_id)

        login(api_client, reviewer)
        response = api_client.post(f"/api/audits/{audit.id}/finalize")

        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "UNRESOLVED_FINDINGS"
        assert len(error["blocking_requirements"]) == 2
        assert sorted(b["control_id"] for b in error["blocking_requirements"]) == blocked_clauses
        assert all("awaiting review" in b["reason"] for b in error["blocking_requirements"])

        db.refresh(audit)
        assert audit.status == AuditStatus.in_progress
        assert db.scalar(select(func.count()).select_from(Report)) == 0

    def test_confirmed_requirement_with_no_finding_blocks(
        self,
        api_client: TestClient,
        make_user: Any,
        make_audit: Any,
        make_scoped_requirement: Any,
    ) -> None:
        """A requirement with no evidence at all must block just as loudly as
        one with an unreviewed draft — otherwise the quiet gap is the dangerous
        one, since nothing on screen says it is missing."""
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        audit = make_audit(reviewer, status=AuditStatus.in_progress)
        make_scoped_requirement(audit, confirmed=True)
        login(api_client, reviewer)

        response = api_client.post(f"/api/audits/{audit.id}/finalize")

        assert response.status_code == 409
        blocking = response.json()["error"]["blocking_requirements"]
        assert len(blocking) == 1
        assert "no approved finding" in blocking[0]["reason"]

    def test_rejected_finding_alone_does_not_satisfy_a_requirement(
        self,
        api_client: TestClient,
        make_user: Any,
        make_audit: Any,
        make_finding: Any,
        make_scoped_requirement: Any,
    ) -> None:
        """A rejected Finding is excluded from the report (01_REQUIREMENTS.md
        § Finding Review), so a requirement covered only by rejections is still
        uncovered."""
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        audit = make_audit(reviewer, status=AuditStatus.in_progress)
        scoped = make_scoped_requirement(audit)
        make_finding(audit, status=FindingStatus.rejected, scoped_control=scoped)
        login(api_client, reviewer)

        response = api_client.post(f"/api/audits/{audit.id}/finalize")

        assert response.status_code == 409
        assert response.json()["error"]["blocking_requirements"][0]["reason"].startswith(
            "no approved finding"
        )

    def test_unconfirmed_requirements_do_not_block(
        self,
        api_client: TestClient,
        make_user: Any,
        make_audit: Any,
        make_finding: Any,
        make_scoped_requirement: Any,
    ) -> None:
        """Only *confirmed* scope counts. An AI suggestion the auditor never
        accepted is not part of the audit and must not hold it hostage."""
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        audit = make_audit(reviewer, status=AuditStatus.in_progress)
        approved_scope = make_scoped_requirement(audit, confirmed=True)
        make_finding(
            audit,
            status=FindingStatus.approved,
            reviewed_by=reviewer,
            auditor_decision=EvaluationResult.PASS,
            scoped_control=approved_scope,
        )
        make_scoped_requirement(audit, confirmed=False)
        login(api_client, reviewer)

        response = api_client.post(f"/api/audits/{audit.id}/finalize")

        assert response.status_code == 200, response.text

    def test_acknowledged_gap_permits_finalization(
        self,
        api_client: TestClient,
        db: DBSession,
        make_user: Any,
        make_audit: Any,
        make_scoped_requirement: Any,
    ) -> None:
        """01_REQUIREMENTS.md Edge Cases: finalizing with a known, acknowledged
        gap is "explicitly supported via gap_acknowledged, not a workaround"."""
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        audit = make_audit(reviewer, status=AuditStatus.in_progress)
        scoped = make_scoped_requirement(audit, confirmed=True)
        login(api_client, reviewer)

        acknowledged = api_client.patch(
            f"/api/scoped-requirements/{scoped.id}/gap",
            json={
                "gap_acknowledged": True,
                "gap_note": "Client could not produce the artifact before the deadline.",
            },
        )
        assert acknowledged.status_code == 200

        response = api_client.post(f"/api/audits/{audit.id}/finalize")

        assert response.status_code == 200, response.text
        report = db.scalar(select(Report).where(Report.audit_id == audit.id))
        assert report is not None
        assert len(report.snapshot_data["acknowledged_gaps"]) == 1
        assert (
            report.snapshot_data["acknowledged_gaps"][0]["gap_note"]
            == "Client could not produce the artifact before the deadline."
        )

    def test_an_acknowledged_gap_does_not_excuse_an_unreviewed_draft(
        self,
        api_client: TestClient,
        make_user: Any,
        make_audit: Any,
        make_finding: Any,
        make_scoped_requirement: Any,
    ) -> None:
        """01_REQUIREMENTS.md: the auditor "can finalize with known gaps, but
        never with unreviewed drafts". Acknowledging a gap must not become a way
        to skip reviewing a finding that already exists."""
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        audit = make_audit(reviewer, status=AuditStatus.in_progress)
        scoped = make_scoped_requirement(audit, confirmed=True, gap_acknowledged=True)
        scoped.gap_note = "Acknowledged."
        make_finding(audit, status=FindingStatus.pending_review, scoped_control=scoped)
        login(api_client, reviewer)

        response = api_client.post(f"/api/audits/{audit.id}/finalize")

        assert response.status_code == 409
        assert "awaiting review" in response.json()["error"]["blocking_requirements"][0]["reason"]

    def test_readiness_endpoint_reports_the_same_blockers(
        self,
        api_client: TestClient,
        make_user: Any,
        make_audit: Any,
        make_finding: Any,
    ) -> None:
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        audit = make_audit(reviewer, status=AuditStatus.in_progress)
        make_finding(audit, status=FindingStatus.pending_review)
        login(api_client, reviewer)

        readiness = api_client.get(f"/api/audits/{audit.id}/finalization-readiness").json()

        assert readiness["ready"] is False
        assert len(readiness["blocking_requirements"]) == 1


class TestSuccessfulFinalization:
    def test_finalization_creates_a_report_and_sets_status(
        self, api_client: TestClient, db: DBSession, ready_audit: dict[str, Any]
    ) -> None:
        """01_REQUIREMENTS.md acceptance criterion: "Given all Findings approved
        or gaps acknowledged, when finalize is called by a Reviewer,
        Audit.status becomes finalized and a Report is created"."""
        setup = ready_audit
        login(api_client, setup["reviewer"])

        response = api_client.post(f"/api/audits/{setup['audit'].id}/finalize")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["audit_status"] == "finalized"

        db.refresh(setup["audit"])
        assert setup["audit"].status == AuditStatus.finalized
        assert setup["audit"].finalized_by == setup["reviewer"].id
        assert setup["audit"].finalized_at is not None

        report = db.scalar(select(Report).where(Report.id == uuid.UUID(body["report_id"])))
        assert report is not None
        assert report.generated_by == setup["reviewer"].id

    def test_snapshot_records_who_signed_off_and_against_which_corpus(
        self, api_client: TestClient, db: DBSession, ready_audit: dict[str, Any]
    ) -> None:
        """The report must be self-describing: 03_DATA_MODEL.md makes it an
        immutable snapshot, so the reviewer's identity and the corpus version in
        force must be inside it rather than looked up later."""
        setup = ready_audit
        login(api_client, setup["reviewer"])
        api_client.post(f"/api/audits/{setup['audit'].id}/finalize")

        report = db.scalar(select(Report).where(Report.audit_id == setup["audit"].id))
        assert report is not None
        snapshot = report.snapshot_data
        assert snapshot["generated_by"]["id"] == str(setup["reviewer"].id)
        assert snapshot["generated_by"]["role"] == "reviewer"
        assert snapshot["framework"] == "PCI DSS v4.0.1"
        assert snapshot["corpus_versions"]
        assert snapshot["findings"][0]["auditor_decision"] == "PASS"
        assert snapshot["findings"][0]["reviewed_by"] == setup["reviewer"].name

    def test_snapshot_keeps_the_system_result_beside_the_human_decision(
        self, api_client: TestClient, db: DBSession, ready_audit: dict[str, Any]
    ) -> None:
        """03_DATA_MODEL.md → Report: both are preserved, in separate fields, so
        a later reader can see where the auditor agreed with the engine and
        where they did not. Merging them would destroy exactly that."""
        setup = ready_audit
        login(api_client, setup["reviewer"])
        api_client.post(f"/api/audits/{setup['audit'].id}/finalize")

        report = db.scalar(select(Report).where(Report.audit_id == setup["audit"].id))
        assert report is not None
        finding = report.snapshot_data["findings"][0]
        assert finding["system_result"] == "PASS"
        assert finding["auditor_decision"] == "PASS"
        assert finding["is_override"] is False
        # The mechanics that produced the result travel with it.
        assert "rules_used" in finding
        assert "evidence_locations" in finding
        assert "gate_status" in finding
        assert finding["engine_version"] is not None
        assert finding["llm_involved_in_result"] is False

    def test_snapshot_stamps_the_engine_and_corpus_versions(
        self, api_client: TestClient, db: DBSession, ready_audit: dict[str, Any]
    ) -> None:
        """01_REQUIREMENTS.md § Audit Finalization: a later policy or rule-engine
        change must never retroactively alter what a past report says."""
        setup = ready_audit
        login(api_client, setup["reviewer"])
        api_client.post(f"/api/audits/{setup['audit'].id}/finalize")

        report = db.scalar(select(Report).where(Report.audit_id == setup["audit"].id))
        assert report is not None
        assert report.snapshot_data["engine_versions"]
        assert report.snapshot_data["corpus_versions"]
        assert report.corpus_version is not None
        assert report.engine_version is not None

    def test_snapshot_carries_evidence_hashes(
        self, api_client: TestClient, db: DBSession, ready_audit: dict[str, Any]
    ) -> None:
        """00_PRODUCT.md §5.6 specifies "evidence hashes and locations".

        The SHA-256 existed in the database, was checked by the gate and was even
        returned by the live facts API — but the one place it is evidentially
        load-bearing, the frozen report, dropped it. A citation whose integrity a
        future reader cannot confirm is only half a citation.
        """
        setup = ready_audit
        login(api_client, setup["reviewer"])
        api_client.post(f"/api/audits/{setup['audit'].id}/finalize")

        report = db.scalar(select(Report).where(Report.audit_id == setup["audit"].id))
        assert report is not None
        finding = report.snapshot_data["findings"][0]
        # The key is present on every citation the snapshot carries.
        for citation in finding["evidence_locations"]:
            assert "source_hash" in citation

    def test_snapshot_carries_the_requirement_and_its_procedure(
        self, api_client: TestClient, db: DBSession, ready_audit: dict[str, Any]
    ) -> None:
        """A report a reader cannot interpret without the corpus to hand is not
        the self-contained record 03_DATA_MODEL.md asks for."""
        setup = ready_audit
        login(api_client, setup["reviewer"])
        api_client.post(f"/api/audits/{setup['audit'].id}/finalize")

        report = db.scalar(select(Report).where(Report.audit_id == setup["audit"].id))
        assert report is not None
        finding = report.snapshot_data["findings"][0]
        assert "assessment_procedures" in finding
        assert "applicability_conditions" in finding
        assert finding["evidence_strength"] is not None

    def test_snapshot_discloses_ai_assistance(
        self, api_client: TestClient, db: DBSession, ready_audit: dict[str, Any]
    ) -> None:
        """The report says where GenAI was involved and in what role, rather
        than leaving a reader to guess."""
        setup = ready_audit
        login(api_client, setup["reviewer"])
        api_client.post(f"/api/audits/{setup['audit'].id}/finalize")

        report = db.scalar(select(Report).where(Report.audit_id == setup["audit"].id))
        assert report is not None
        disclosure = report.snapshot_data["ai_disclosure"]
        assert disclosure["authoritative"] is False
        assert disclosure["role"] == "explanation_drafting_only"

    def test_draft_and_rejected_findings_are_excluded_from_the_report(
        self,
        api_client: TestClient,
        db: DBSession,
        make_user: Any,
        make_audit: Any,
        make_finding: Any,
        make_scoped_requirement: Any,
    ) -> None:
        """01_REQUIREMENTS.md § Finalization: the report is assembled from
        approved Findings; rejected ones are retained but excluded."""
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        audit = make_audit(reviewer, status=AuditStatus.in_progress)

        approved_scope = make_scoped_requirement(audit)
        make_finding(
            audit,
            status=FindingStatus.approved,
            reviewed_by=reviewer,
            auditor_decision=EvaluationResult.PASS,
            scoped_control=approved_scope,
        )
        # A rejected finding on a scope row that is *not* confirmed, so it does
        # not itself block finalization.
        rejected_scope = make_scoped_requirement(audit, confirmed=False)
        make_finding(audit, status=FindingStatus.rejected, scoped_control=rejected_scope)

        login(api_client, reviewer)
        assert api_client.post(f"/api/audits/{audit.id}/finalize").status_code == 200

        report = db.scalar(select(Report).where(Report.audit_id == audit.id))
        assert report is not None
        assert len(report.snapshot_data["findings"]) == 1
        assert report.snapshot_data["rejected_finding_count"] == 1

    def test_rejected_findings_are_retained_in_the_database(
        self,
        api_client: TestClient,
        db: DBSession,
        make_user: Any,
        make_audit: Any,
        make_finding: Any,
        make_scoped_requirement: Any,
    ) -> None:
        """03_DATA_MODEL.md: Findings are never deleted, including rejected
        ones — they are the record of AI quality over time."""
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        audit = make_audit(reviewer, status=AuditStatus.in_progress)
        rejected = make_finding(
            audit,
            status=FindingStatus.rejected,
            scoped_control=make_scoped_requirement(audit, confirmed=False),
        )
        make_finding(
            audit,
            status=FindingStatus.approved,
            reviewed_by=reviewer,
            auditor_decision=EvaluationResult.PASS,
        )
        login(api_client, reviewer)
        api_client.post(f"/api/audits/{audit.id}/finalize")

        db.refresh(rejected)
        assert rejected.status == FindingStatus.rejected


class TestFinalizationIsTerminal:
    def test_second_finalize_returns_409_and_no_duplicate_report(
        self, api_client: TestClient, db: DBSession, ready_audit: dict[str, Any]
    ) -> None:
        """04_API_CONTRACT.md Idempotency: "Calling finalize on an already-
        finalized audit returns 409 ALREADY_FINALIZED rather than creating
        a duplicate Report"."""
        setup = ready_audit
        login(api_client, setup["reviewer"])
        first = api_client.post(f"/api/audits/{setup['audit'].id}/finalize")
        assert first.status_code == 200

        second = api_client.post(f"/api/audits/{setup['audit'].id}/finalize")

        assert second.status_code == 409
        assert second.json()["error"]["code"] == "ALREADY_FINALIZED"
        assert db.scalar(select(func.count()).select_from(Report)) == 1

    def test_findings_become_read_only_after_finalization(
        self, api_client: TestClient, ready_audit: dict[str, Any]
    ) -> None:
        """01_REQUIREMENTS.md § Finalization, Business Rules: "Once finalized,
        an audit's Findings become read-only. Any correction requires a
        new, explicitly-labelled addendum — never a silent edit"."""
        setup = ready_audit
        login(api_client, setup["reviewer"])
        api_client.post(f"/api/audits/{setup['audit'].id}/finalize")

        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={
                "action": "approve",
                "auditor_decision": "FAIL",
                "note": "Attempting a silent correction after sign-off.",
            },
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "AUDIT_FINALIZED"

    def test_evidence_cannot_be_uploaded_after_finalization(
        self, api_client: TestClient, ready_audit: dict[str, Any]
    ) -> None:
        setup = ready_audit
        login(api_client, setup["reviewer"])
        api_client.post(f"/api/audits/{setup['audit'].id}/finalize")

        response = api_client.post(
            f"/api/audits/{setup['audit'].id}/evidence-documents",
            files={"file": ("x.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "AUDIT_FINALIZED"


class TestReportAccess:
    def test_report_is_readable_by_an_assigned_auditor(
        self, api_client: TestClient, ready_audit: dict[str, Any]
    ) -> None:
        setup = ready_audit
        login(api_client, setup["reviewer"])
        api_client.post(f"/api/audits/{setup['audit'].id}/finalize")

        login(api_client, setup["auditor"])
        response = api_client.get(f"/api/audits/{setup['audit'].id}/report")

        assert response.status_code == 200
        assert response.json()["snapshot_data"]["framework"] == "PCI DSS v4.0.1"

    def test_report_is_not_readable_by_an_unassigned_auditor(
        self, api_client: TestClient, make_user: Any, ready_audit: dict[str, Any]
    ) -> None:
        """A finalized report is the most sensitive artifact in the system — it
        states another organisation's compliance gaps in full."""
        setup = ready_audit
        login(api_client, setup["reviewer"])
        api_client.post(f"/api/audits/{setup['audit'].id}/finalize")

        intruder = make_user(Role.auditor, password=PASSWORD)
        login(api_client, intruder)
        response = api_client.get(f"/api/audits/{setup['audit'].id}/report")

        assert response.status_code == 403

    def test_report_is_404_before_finalization(
        self, api_client: TestClient, ready_audit: dict[str, Any]
    ) -> None:
        setup = ready_audit
        login(api_client, setup["reviewer"])

        response = api_client.get(f"/api/audits/{setup['audit'].id}/report")

        assert response.status_code == 404

    def test_pdf_export_renders_and_is_served_as_an_attachment(
        self, api_client: TestClient, ready_audit: dict[str, Any]
    ) -> None:
        """01_REQUIREMENTS.md § Finalization processing rule 2 requires a PDF
        export generated from the snapshot."""
        setup = ready_audit
        login(api_client, setup["reviewer"])
        api_client.post(f"/api/audits/{setup['audit'].id}/finalize")

        response = api_client.get(f"/api/audits/{setup['audit'].id}/report?format=pdf")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.content.startswith(b"%PDF")
        assert len(response.content) > 1000

    def test_pdf_export_escapes_client_supplied_text(
        self,
        api_client: TestClient,
        db: DBSession,
        make_user: Any,
        make_audit: Any,
        make_finding: Any,
    ) -> None:
        """The renderer receives LLM-generated rationale and client-derived
        names. Unescaped markup would either corrupt the document or be
        interpreted — so the escaping is exercised rather than assumed."""
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        audit = make_audit(
            reviewer,
            status=AuditStatus.in_progress,
            client_name="Acme <b>& Co</b>",
        )
        finding = make_finding(
            audit,
            status=FindingStatus.approved,
            reviewed_by=reviewer,
            auditor_decision=EvaluationResult.PASS,
        )
        finding.ai_rationale = "Contains <script>alert(1)</script> & an ampersand"
        finding.review_note = "Note with <tags> & symbols"
        db.flush()

        login(api_client, reviewer)
        assert api_client.post(f"/api/audits/{audit.id}/finalize").status_code == 200

        response = api_client.get(f"/api/audits/{audit.id}/report?format=pdf")

        assert response.status_code == 200
        assert response.content.startswith(b"%PDF")


class TestFinalizationIsNeverAutomatic:
    def test_no_scheduler_or_worker_path_reaches_finalize(self) -> None:
        """01_REQUIREMENTS.md, Explicitly Forbidden Behavior: "The system must
        never auto-finalize an audit on any schedule, timeout, or batch
        process."

        Asserted structurally rather than behaviourally: the worker module must
        not import or reference the finalization service at all, so no future
        edit can quietly add a batch path without this failing.
        """
        from pathlib import Path

        worker_source = (
            Path(__file__).resolve().parent.parent / "app" / "pipelines" / "worker.py"
        ).read_text()

        assert "FinalizationService" not in worker_source
        assert "finalize" not in worker_source

    def test_finalize_is_reachable_from_exactly_one_route(self) -> None:
        """A second finalize endpoint would be a second place for the Reviewer
        check to be forgotten."""
        from app.main import app

        finalize_paths = [path for path in app.openapi()["paths"] if path.endswith("/finalize")]
        assert finalize_paths == ["/api/audits/{audit_id}/finalize"]
