"""Finding review tests (TASK-020, TASK-106, TASK-114).

The review path changed shape in this revision. A Finding no longer wraps an AI
suggestion the human accepts or edits; it wraps a `ControlEvaluation` the machine
produced, and the human records a *separate* `auditor_decision` beside it.

Two invariants carry the product, and both are tested here:

* **`system_result` is never overwritten.** Approving, rejecting, or overriding
  all leave `ControlEvaluation.result` byte-identical. This is what keeps "how
  often did the human disagree with the machine" answerable later
  (01_REQUIREMENTS.md § Finding Review, Explicitly Forbidden Behavior).
* **No Finding reaches `approved` without `reviewed_by`** (ADR-003). 08_TESTING.md
  requires this be attempted "via direct service-layer call, not just via the
  API, to catch any bypass path" — that is `TestApprovalRequiresAReviewer`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.api.deps import Actor
from app.errors import ValidationError
from app.models.audit import AuditAssignment
from app.models.enums import (
    AuditStatus,
    EvaluationResult,
    FindingAction,
    FindingStatus,
    GateStatus,
    Role,
)
from app.models.finding import FindingHistory
from app.services.finding import FindingService

PASSWORD = "correct-horse-battery-staple"


def login(client: TestClient, user: Any) -> None:
    assert (
        client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD}).status_code
        == 200
    )


def actor_for(user: Any) -> Actor:
    return Actor(id=user.id, role=user.role, name=user.name, email=user.email)


@pytest.fixture
def audit_with_draft(
    db: DBSession, make_user: Any, make_audit: Any, make_finding: Any
) -> dict[str, Any]:
    """One pending Finding on an audit an Auditor and a Reviewer both work."""
    auditor = make_user(Role.auditor, password=PASSWORD, name="Junior Auditor")
    reviewer = make_user(Role.reviewer, password=PASSWORD, name="Audit Lead")
    audit = make_audit(auditor, status=AuditStatus.in_progress)
    db.add(AuditAssignment(audit_id=audit.id, user_id=reviewer.id))
    db.flush()
    finding = make_finding(
        audit, status=FindingStatus.pending_review, system_result=EvaluationResult.PASS
    )
    return {
        "auditor": auditor,
        "reviewer": reviewer,
        "audit": audit,
        "finding": finding,
    }


class TestApprovePath:
    def test_approving_without_a_decision_adopts_the_system_result(
        self, db: DBSession, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        """Omitting `auditor_decision` means "I agree with the machine". The
        value is *copied* into the human's column, not aliased, so both remain
        independently readable afterwards."""
        setup = audit_with_draft
        login(api_client, setup["auditor"])

        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review", json={"action": "approve"}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "approved"
        assert body["auditor_decision"] == "PASS"
        assert body["system_result"] == "PASS"
        assert body["is_override"] is False

    def test_the_system_result_is_untouched_by_approval(
        self, db: DBSession, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        setup = audit_with_draft
        before = setup["finding"].evaluation.result
        login(api_client, setup["auditor"])

        api_client.patch(f"/api/findings/{setup['finding'].id}/review", json={"action": "approve"})

        db.expire_all()
        assert setup["finding"].evaluation.result == before

    def test_reviewed_by_and_at_are_recorded(
        self, db: DBSession, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        setup = audit_with_draft
        login(api_client, setup["auditor"])

        body = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review", json={"action": "approve"}
        ).json()

        assert body["reviewed_by"] == str(setup["auditor"].id)
        assert body["reviewed_at"] is not None


class TestOverride:
    """An auditor disagreeing with the machine is always permitted, always
    logged, and never destructive (01_REQUIREMENTS.md § Finding Review)."""

    def test_a_differing_decision_is_allowed_and_flagged(
        self, db: DBSession, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        setup = audit_with_draft
        login(api_client, setup["auditor"])

        body = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={
                "action": "approve",
                "auditor_decision": "FAIL",
                "note": "The export predates the change that introduced the gap.",
            },
        ).json()

        assert body["auditor_decision"] == "FAIL"
        assert body["system_result"] == "PASS"
        assert body["is_override"] is True

    def test_an_override_still_leaves_the_evaluation_intact(
        self, db: DBSession, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        """The whole point of two columns: the machine's answer survives the
        human's disagreement, so the disagreement stays measurable."""
        setup = audit_with_draft
        evaluation_id = setup["finding"].control_evaluation_id
        login(api_client, setup["auditor"])

        api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "approve", "auditor_decision": "FAIL", "note": "Disagree."},
        )

        db.expire_all()
        assert setup["finding"].control_evaluation_id == evaluation_id
        assert setup["finding"].evaluation.result == EvaluationResult.PASS

    def test_an_override_requires_a_note(
        self, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        """Departing from the mechanical result has to be explainable, or the
        override is not reviewable later."""
        setup = audit_with_draft
        login(api_client, setup["auditor"])

        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "approve", "auditor_decision": "FAIL"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_agreeing_needs_no_note(
        self, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        setup = audit_with_draft
        login(api_client, setup["auditor"])
        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "approve", "auditor_decision": "PASS"},
        )
        assert response.status_code == 200

    def test_the_override_is_recorded_in_history_with_both_values(
        self, db: DBSession, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        setup = audit_with_draft
        login(api_client, setup["auditor"])
        api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "approve", "auditor_decision": "FAIL", "note": "Disagree."},
        )

        entry = db.scalars(
            select(FindingHistory).where(FindingHistory.finding_id == setup["finding"].id)
        ).one()

        assert entry.new_decision == EvaluationResult.FAIL
        # What the machine said at the moment of the decision, copied onto the
        # history row so the disagreement is legible without a re-join.
        assert entry.system_result == EvaluationResult.PASS


class TestRejectAndRequestMoreEvidence:
    def test_reject_records_the_note(
        self, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        setup = audit_with_draft
        login(api_client, setup["auditor"])

        body = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "reject", "note": "The cited export is for the wrong environment."},
        ).json()

        assert body["status"] == "rejected"
        assert body["review_note"] == "The cited export is for the wrong environment."

    def test_reject_without_a_note_is_refused(
        self, db: DBSession, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        setup = audit_with_draft
        login(api_client, setup["auditor"])

        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review", json={"action": "reject"}
        )

        assert response.status_code == 400
        db.expire_all()
        assert setup["finding"].status == FindingStatus.pending_review

    def test_a_whitespace_only_note_does_not_count(
        self, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        setup = audit_with_draft
        login(api_client, setup["auditor"])
        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "reject", "note": "   "},
        )
        assert response.status_code == 400

    def test_request_more_evidence_sets_its_own_status(
        self, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        """01_REQUIREMENTS.md: this routes back into the evidence pipeline
        rather than closing the item."""
        setup = audit_with_draft
        login(api_client, setup["auditor"])

        body = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "request_more_evidence", "note": "Need the production export."},
        ).json()

        assert body["status"] == "needs_more_evidence"

    def test_request_more_evidence_requires_a_note(
        self, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        setup = audit_with_draft
        login(api_client, setup["auditor"])
        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "request_more_evidence"},
        )
        assert response.status_code == 400

    def test_rejected_findings_are_never_deleted(
        self, db: DBSession, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        setup = audit_with_draft
        login(api_client, setup["auditor"])
        api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "reject", "note": "Wrong environment."},
        )

        db.expire_all()
        assert setup["finding"].id is not None
        assert setup["finding"].status == FindingStatus.rejected


class TestReviewerOverridesAuditor:
    def test_reviewer_may_revisit_an_auditors_decision(
        self, db: DBSession, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        setup = audit_with_draft

        login(api_client, setup["auditor"])
        api_client.patch(f"/api/findings/{setup['finding'].id}/review", json={"action": "approve"})

        login(api_client, setup["reviewer"])
        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={
                "action": "approve",
                "auditor_decision": "FAIL",
                "note": "Reviewed with the client; the control is not in place.",
            },
        )

        assert response.status_code == 200
        assert response.json()["auditor_decision"] == "FAIL"

        entries = db.scalars(
            select(FindingHistory)
            .where(FindingHistory.finding_id == setup["finding"].id)
            .order_by(FindingHistory.created_at)
        ).all()
        assert [e.action.value for e in entries] == ["approve", "override"]

    def test_an_auditor_cannot_override_an_already_reviewed_finding(
        self, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        setup = audit_with_draft
        login(api_client, setup["auditor"])
        api_client.patch(f"/api/findings/{setup['finding'].id}/review", json={"action": "approve"})

        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "reject", "note": "Changed my mind."},
        )

        assert response.status_code == 403

    def test_reviewer_can_move_a_rejection_back_to_approved(
        self, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        setup = audit_with_draft
        login(api_client, setup["auditor"])
        api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "reject", "note": "Looked wrong."},
        )

        login(api_client, setup["reviewer"])
        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "approve", "note": "Re-examined; the evidence does support it."},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "approved"

    def test_history_is_exposed_through_the_api(
        self, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        setup = audit_with_draft
        login(api_client, setup["auditor"])
        api_client.patch(f"/api/findings/{setup['finding'].id}/review", json={"action": "approve"})

        history = api_client.get(f"/api/findings/{setup['finding'].id}/history").json()

        assert len(history) == 1
        assert history[0]["new_status"] == "approved"
        assert history[0]["system_result"] == "PASS"


class TestApprovalRequiresAReviewer:
    """08_TESTING.md § Security Tests: attempted via a direct service-layer
    call, not only through the API, to catch any bypass path."""

    def test_service_layer_always_sets_reviewed_by_from_the_actor(
        self, db: DBSession, audit_with_draft: dict[str, Any]
    ) -> None:
        setup = audit_with_draft
        service = FindingService(db)

        finding = service.review(
            setup["finding"].id, actor_for(setup["auditor"]), action=FindingAction.approve
        )

        assert finding.reviewed_by == setup["auditor"].id
        assert finding.status == FindingStatus.approved

    def test_reviewed_by_is_never_taken_from_the_request_body(
        self, db: DBSession, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        setup = audit_with_draft
        login(api_client, setup["auditor"])

        api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "approve", "reviewed_by": str(setup["reviewer"].id)},
        )

        db.expire_all()
        assert setup["finding"].reviewed_by == setup["auditor"].id

    def test_a_request_body_cannot_set_the_system_result(
        self, db: DBSession, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        """05_SECURITY.md §10.3: `ControlEvaluation.result` has no API write
        path under any role. The field name is not in the review schema at all,
        so an attempt to send it is silently ignored rather than honoured."""
        setup = audit_with_draft
        login(api_client, setup["auditor"])

        api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "approve", "system_result": "FAIL", "result": "FAIL"},
        )

        db.expire_all()
        assert setup["finding"].evaluation.result == EvaluationResult.PASS

    def test_override_is_not_an_accepted_client_action(
        self, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        """`override` is derived server-side from prior state. Accepting it as
        input would let a caller mislabel its own action in the audit trail."""
        setup = audit_with_draft
        login(api_client, setup["auditor"])

        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "override", "auditor_decision": "FAIL", "note": "x"},
        )

        assert response.status_code == 400

    def test_direct_service_call_with_override_action_is_refused(
        self, db: DBSession, audit_with_draft: dict[str, Any]
    ) -> None:
        setup = audit_with_draft
        with pytest.raises(ValidationError):
            FindingService(db).review(
                setup["finding"].id,
                actor_for(setup["reviewer"]),
                action=FindingAction.override,
                note="x",
            )

    def test_history_row_is_written_with_every_approval(
        self, db: DBSession, audit_with_draft: dict[str, Any]
    ) -> None:
        setup = audit_with_draft
        FindingService(db).review(
            setup["finding"].id, actor_for(setup["auditor"]), action=FindingAction.approve
        )

        entries = db.scalars(
            select(FindingHistory).where(FindingHistory.finding_id == setup["finding"].id)
        ).all()
        assert len(entries) == 1
        assert entries[0].actor_id == setup["auditor"].id


class TestReviewAuthorization:
    def test_unassigned_auditor_cannot_review(
        self, api_client: TestClient, make_user: Any, audit_with_draft: dict[str, Any]
    ) -> None:
        outsider = make_user(Role.auditor, password=PASSWORD)
        login(api_client, outsider)

        response = api_client.patch(
            f"/api/findings/{audit_with_draft['finding'].id}/review", json={"action": "approve"}
        )

        assert response.status_code == 403

    def test_unassigned_auditor_cannot_read_the_finding_or_its_history(
        self, api_client: TestClient, make_user: Any, audit_with_draft: dict[str, Any]
    ) -> None:
        outsider = make_user(Role.auditor, password=PASSWORD)
        login(api_client, outsider)
        finding_id = audit_with_draft["finding"].id

        assert api_client.get(f"/api/findings/{finding_id}").status_code == 403
        assert api_client.get(f"/api/findings/{finding_id}/history").status_code == 403

    def test_unauthenticated_review_is_rejected(
        self, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        response = api_client.patch(
            f"/api/findings/{audit_with_draft['finding'].id}/review", json={"action": "approve"}
        )
        assert response.status_code == 401

    def test_unknown_finding_returns_404(self, api_client: TestClient, make_user: Any) -> None:
        login(api_client, make_user(Role.auditor, password=PASSWORD))
        response = api_client.get(f"/api/findings/{uuid.uuid4()}")
        assert response.status_code == 404

    def test_reviewer_may_act_on_any_audit(
        self, api_client: TestClient, make_user: Any, audit_with_draft: dict[str, Any]
    ) -> None:
        unassigned_reviewer = make_user(Role.reviewer, password=PASSWORD)
        login(api_client, unassigned_reviewer)

        response = api_client.patch(
            f"/api/findings/{audit_with_draft['finding'].id}/review", json={"action": "approve"}
        )

        assert response.status_code == 200


class TestReviewQueue:
    def test_the_queue_separates_machine_and_human_fields(
        self, api_client: TestClient, audit_with_draft: dict[str, Any]
    ) -> None:
        """04_API_CONTRACT.md, Security Notes: the separation is a contract
        guarantee, so a client cannot receive them pre-merged."""
        setup = audit_with_draft
        login(api_client, setup["auditor"])

        row = api_client.get(f"/api/audits/{setup['audit'].id}/findings").json()[0]

        assert row["system_result"] == "PASS"
        assert row["auditor_decision"] is None
        assert row["awaiting_review"] is True
        assert row["llm_involved"] is False

    def test_a_gate_rejected_finding_is_explicitly_flagged(
        self,
        db: DBSession,
        api_client: TestClient,
        make_user: Any,
        make_audit: Any,
        make_finding: Any,
    ) -> None:
        """01_REQUIREMENTS.md § Finding Review, Edge Cases: a result the gate
        could not verify must never look the same as one it could."""
        auditor = make_user(Role.auditor, password=PASSWORD)
        audit = make_audit(auditor, status=AuditStatus.in_progress)
        make_finding(audit, gate_status=GateStatus.REJECTED)
        login(api_client, auditor)

        row = api_client.get(f"/api/audits/{audit.id}/findings").json()[0]

        assert row["gate_status"] == "REJECTED"
        assert row["unverified_by_gate"] is True

    def test_status_filter_applies(
        self,
        db: DBSession,
        api_client: TestClient,
        make_user: Any,
        make_audit: Any,
        make_finding: Any,
    ) -> None:
        auditor = make_user(Role.auditor, password=PASSWORD)
        audit = make_audit(auditor, status=AuditStatus.in_progress)
        make_finding(audit, status=FindingStatus.pending_review)
        make_finding(
            audit,
            status=FindingStatus.approved,
            auditor_decision=EvaluationResult.PASS,
            reviewed_by=auditor,
        )
        login(api_client, auditor)

        pending = api_client.get(
            f"/api/audits/{audit.id}/findings", params={"status": "pending_review"}
        ).json()

        assert len(pending) == 1
        assert pending[0]["status"] == "pending_review"
