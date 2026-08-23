"""Finding review tests (TASK-020).

TASK-020 requires tests for the accept/edit/reject paths and the
Reviewer-overrides-Auditor case with history verification.

08_TESTING.md § Security Tests requires one of these specifically be attempted
"via direct service-layer call, not just via the API, to catch any bypass path":
a Finding must not reach `status=approved` without `reviewed_by` set. That is
`TestApprovalRequiresAReviewer`.
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
from app.models.engagement import EngagementAssignment
from app.models.enums import ComplianceStatus, EngagementStatus, FindingAction, FindingStatus, Role
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
def engagement_with_draft(
    db: DBSession, make_user: Any, make_engagement: Any, make_finding: Any
) -> dict[str, Any]:
    """One draft Finding on an engagement an Auditor and a Reviewer both work."""
    auditor = make_user(Role.auditor, password=PASSWORD, name="Junior Auditor")
    reviewer = make_user(Role.reviewer, password=PASSWORD, name="Engagement Lead")
    engagement = make_engagement(auditor, status=EngagementStatus.in_progress)
    db.add(EngagementAssignment(engagement_id=engagement.id, user_id=reviewer.id))
    db.flush()
    finding = make_finding(engagement, status=FindingStatus.draft)
    return {
        "auditor": auditor,
        "reviewer": reviewer,
        "engagement": engagement,
        "finding": finding,
    }


class TestAcceptPath:
    def test_accept_approves_with_the_ai_suggested_status(
        self, api_client: TestClient, db: DBSession, engagement_with_draft: dict[str, Any]
    ) -> None:
        """01_REQUIREMENTS.md § Processing Rules: accept sets status=approved,
        final_status=ai_suggestion, and the reviewer fields."""
        setup = engagement_with_draft
        login(api_client, setup["auditor"])

        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review", json={"action": "accept"}
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "approved"
        assert body["final_status"] == "satisfied"
        assert body["reviewed_by"] == str(setup["auditor"].id)
        assert body["reviewed_at"] is not None
        assert body["is_ai_draft"] is False

    def test_accepting_clears_the_manual_review_flag(
        self, api_client: TestClient, make_user: Any, make_engagement: Any, make_finding: Any
    ) -> None:
        """The flag means "a human still needs to look at this". Once one has,
        leaving it set would keep the item in the attention queue forever."""
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor, status=EngagementStatus.in_progress)
        finding = make_finding(engagement, needs_manual_review=True, ai_confidence=0.4)
        login(api_client, auditor)

        response = api_client.patch(f"/api/findings/{finding.id}/review", json={"action": "accept"})

        assert response.status_code == 200
        assert response.json()["needs_manual_review"] is False

    def test_accept_is_refused_when_there_is_no_ai_suggestion(
        self, api_client: TestClient, make_user: Any, make_engagement: Any, make_finding: Any
    ) -> None:
        """When the LLM failed, the Finding exists with a null suggestion.
        "Accepting" nothing is not a determination — the human must supply the
        status via `edit`, which is what makes the approval meaningful."""
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor, status=EngagementStatus.in_progress)
        finding = make_finding(
            engagement,
            ai_suggested_status=None,
            ai_confidence=None,
            needs_manual_review=True,
        )
        login(api_client, auditor)

        response = api_client.patch(f"/api/findings/{finding.id}/review", json={"action": "accept"})

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "NO_AI_SUGGESTION"


class TestEditPath:
    def test_edit_approves_with_the_human_value(
        self, api_client: TestClient, engagement_with_draft: dict[str, Any]
    ) -> None:
        """01_REQUIREMENTS.md: "final_status = edited_status (human value
        overrides AI value)"."""
        setup = engagement_with_draft
        login(api_client, setup["auditor"])

        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "edit", "edited_status": "partial", "note": "Only covers scope A."},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "approved"
        assert body["final_status"] == "partial"
        assert body["review_note"] == "Only covers scope A."

    def test_edit_retains_the_original_ai_suggestion(
        self, api_client: TestClient, db: DBSession, engagement_with_draft: dict[str, Any]
    ) -> None:
        """01_REQUIREMENTS.md: "original AI suggestion retained for audit trail
        (never overwritten)". This is what makes measuring AI quality over time
        possible at all."""
        setup = engagement_with_draft
        login(api_client, setup["auditor"])

        api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "edit", "edited_status": "not_satisfied"},
        )

        db.refresh(setup["finding"])
        assert setup["finding"].ai_suggested_status == ComplianceStatus.satisfied
        assert setup["finding"].final_status == ComplianceStatus.not_satisfied
        assert setup["finding"].ai_confidence == 0.85

    def test_edit_without_edited_status_is_rejected(
        self, api_client: TestClient, db: DBSession, engagement_with_draft: dict[str, Any]
    ) -> None:
        """01_REQUIREMENTS.md Failure Cases: edit without edited_status → 400."""
        setup = engagement_with_draft
        login(api_client, setup["auditor"])

        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review", json={"action": "edit"}
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

        db.refresh(setup["finding"])
        assert setup["finding"].status == FindingStatus.draft

    def test_edited_status_must_be_a_known_value(
        self, api_client: TestClient, engagement_with_draft: dict[str, Any]
    ) -> None:
        """01_REQUIREMENTS.md Validation Rules: edited_status must be one of the
        same enum values the AI could suggest."""
        setup = engagement_with_draft
        login(api_client, setup["auditor"])

        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "edit", "edited_status": "definitely_compliant"},
        )

        assert response.status_code == 400


class TestRejectPath:
    def test_reject_sets_rejected_and_records_the_note(
        self, api_client: TestClient, engagement_with_draft: dict[str, Any]
    ) -> None:
        setup = engagement_with_draft
        login(api_client, setup["auditor"])

        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "reject", "note": "Evidence is for a different system."},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "rejected"
        assert body["final_status"] is None
        assert body["review_note"] == "Evidence is for a different system."

    def test_reject_without_a_note_is_rejected_and_leaves_the_finding_draft(
        self, api_client: TestClient, db: DBSession, engagement_with_draft: dict[str, Any]
    ) -> None:
        """01_REQUIREMENTS.md acceptance criterion: "Given a draft Finding, when
        a Reviewer rejects it without a note, the response is 400 and the
        Finding remains draft." A rejection must be explainable."""
        setup = engagement_with_draft
        login(api_client, setup["reviewer"])

        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review", json={"action": "reject"}
        )

        assert response.status_code == 400
        db.refresh(setup["finding"])
        assert setup["finding"].status == FindingStatus.draft

    def test_a_whitespace_only_note_does_not_count(
        self, api_client: TestClient, engagement_with_draft: dict[str, Any]
    ) -> None:
        setup = engagement_with_draft
        login(api_client, setup["reviewer"])

        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "reject", "note": "   \n  "},
        )

        assert response.status_code == 400

    def test_rejected_findings_are_never_deleted(
        self, api_client: TestClient, db: DBSession, engagement_with_draft: dict[str, Any]
    ) -> None:
        """03_DATA_MODEL.md Deletion Strategy: never deleted, including rejected
        Findings — the record of what the AI proposed and why a human
        disagreed."""
        setup = engagement_with_draft
        login(api_client, setup["auditor"])
        api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "reject", "note": "Not applicable to this environment."},
        )

        db.refresh(setup["finding"])
        assert setup["finding"].id is not None
        assert setup["finding"].ai_rationale is not None


class TestReviewerOverride:
    def test_reviewer_overrides_an_auditor_accept_and_both_appear_in_history(
        self, api_client: TestClient, db: DBSession, engagement_with_draft: dict[str, Any]
    ) -> None:
        """01_REQUIREMENTS.md acceptance criterion: "Given an Auditor accepts a
        Finding, when a Reviewer later edits the same Finding, the final state
        reflects the Reviewer's edit and both actions appear in the Finding's
        history"."""
        setup = engagement_with_draft
        finding_id = setup["finding"].id

        login(api_client, setup["auditor"])
        accepted = api_client.patch(f"/api/findings/{finding_id}/review", json={"action": "accept"})
        assert accepted.status_code == 200
        assert accepted.json()["final_status"] == "satisfied"

        login(api_client, setup["reviewer"])
        overridden = api_client.patch(
            f"/api/findings/{finding_id}/review",
            json={
                "action": "edit",
                "edited_status": "not_satisfied",
                "note": "The config shown predates the assessment period.",
            },
        )

        assert overridden.status_code == 200
        assert overridden.json()["final_status"] == "not_satisfied"
        assert overridden.json()["reviewed_by"] == str(setup["reviewer"].id)

        history = db.scalars(
            select(FindingHistory)
            .where(FindingHistory.finding_id == finding_id)
            .order_by(FindingHistory.created_at)
        ).all()

        assert len(history) == 2, "both decisions must be retained, not overwritten"

        first, second = history
        assert first.actor_id == setup["auditor"].id
        assert first.action == FindingAction.accept
        assert first.previous_status == FindingStatus.draft
        assert first.new_status == FindingStatus.approved
        assert first.new_final_status == ComplianceStatus.satisfied

        # The second entry is logged as an override, not as a plain edit, and it
        # records what it replaced — otherwise the history would say a change
        # happened without saying what changed.
        assert second.actor_id == setup["reviewer"].id
        assert second.action == FindingAction.override
        assert second.previous_status == FindingStatus.approved
        assert second.previous_final_status == ComplianceStatus.satisfied
        assert second.new_final_status == ComplianceStatus.not_satisfied
        assert second.note == "The config shown predates the assessment period."

    def test_history_is_exposed_through_the_api(
        self, api_client: TestClient, engagement_with_draft: dict[str, Any]
    ) -> None:
        setup = engagement_with_draft
        login(api_client, setup["auditor"])
        api_client.patch(f"/api/findings/{setup['finding'].id}/review", json={"action": "accept"})
        login(api_client, setup["reviewer"])
        api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "reject", "note": "Superseded."},
        )

        history = api_client.get(f"/api/findings/{setup['finding'].id}/history").json()

        assert [h["action"] for h in history] == ["accept", "override"]

    def test_an_auditor_cannot_override_an_already_reviewed_finding(
        self,
        api_client: TestClient,
        db: DBSession,
        make_user: Any,
        engagement_with_draft: dict[str, Any],
    ) -> None:
        """01_REQUIREMENTS.md § Authorization Rules gives override authority to
        Reviewers only. Without this, two Auditors could flip a determination
        back and forth with no senior involvement, and the last write would
        silently win."""
        setup = engagement_with_draft
        second_auditor = make_user(Role.auditor, password=PASSWORD)
        db.add(
            EngagementAssignment(engagement_id=setup["engagement"].id, user_id=second_auditor.id)
        )
        db.flush()

        login(api_client, setup["auditor"])
        api_client.patch(f"/api/findings/{setup['finding'].id}/review", json={"action": "accept"})

        login(api_client, second_auditor)
        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "edit", "edited_status": "not_satisfied"},
        )

        assert response.status_code == 403
        db.refresh(setup["finding"])
        assert setup["finding"].final_status == ComplianceStatus.satisfied
        assert setup["finding"].reviewed_by == setup["auditor"].id

    def test_an_auditor_cannot_reopen_their_own_prior_decision(
        self, api_client: TestClient, engagement_with_draft: dict[str, Any]
    ) -> None:
        """Same rule, applied to the author of the original decision — a
        self-revision is still a change to a determination that has been made."""
        setup = engagement_with_draft
        login(api_client, setup["auditor"])
        api_client.patch(f"/api/findings/{setup['finding'].id}/review", json={"action": "accept"})

        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "reject", "note": "Changed my mind."},
        )

        assert response.status_code == 403

    def test_reviewer_can_override_a_rejection_back_to_approved(
        self, api_client: TestClient, db: DBSession, engagement_with_draft: dict[str, Any]
    ) -> None:
        setup = engagement_with_draft
        login(api_client, setup["auditor"])
        api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "reject", "note": "Insufficient."},
        )

        login(api_client, setup["reviewer"])
        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "edit", "edited_status": "partial", "note": "Partially covers it."},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "approved"
        history = db.scalars(
            select(FindingHistory).where(FindingHistory.finding_id == setup["finding"].id)
        ).all()
        assert len(history) == 2


class TestApprovalRequiresAReviewer:
    """ADR-003, and 08_TESTING.md's requirement-to-test map: "No Finding
    approved without reviewed_by — unit test on the service layer directly +
    integration test via API"."""

    def test_service_layer_always_sets_reviewed_by_from_the_actor(
        self, db: DBSession, engagement_with_draft: dict[str, Any]
    ) -> None:
        """Called directly, bypassing the route entirely."""
        setup = engagement_with_draft
        service = FindingService(db)

        finding = service.review(
            setup["finding"].id, actor_for(setup["auditor"]), action=FindingAction.accept
        )

        assert finding.status == FindingStatus.approved
        assert finding.reviewed_by == setup["auditor"].id
        assert finding.final_status is not None

    def test_reviewed_by_is_never_taken_from_the_request_body(
        self,
        api_client: TestClient,
        db: DBSession,
        make_user: Any,
        engagement_with_draft: dict[str, Any],
    ) -> None:
        """04_API_CONTRACT.md Security Notes: "reviewed_by is always set
        server-side from the authenticated session — never accepted from the
        request body". The schema has no such field, so a supplied one is
        dropped; this test exists to catch a future edit that adds it."""
        setup = engagement_with_draft
        someone_else = make_user(Role.reviewer, password=PASSWORD)
        login(api_client, setup["auditor"])

        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={
                "action": "accept",
                "reviewed_by": str(someone_else.id),
                "status": "approved",
                "final_status": "not_applicable",
            },
        )

        assert response.status_code == 200
        db.refresh(setup["finding"])
        assert setup["finding"].reviewed_by == setup["auditor"].id
        assert setup["finding"].final_status == ComplianceStatus.satisfied

    def test_override_is_not_an_accepted_client_action(
        self, api_client: TestClient, engagement_with_draft: dict[str, Any]
    ) -> None:
        """`override` is derived server-side from the finding's prior state.
        Accepting it as input would let a caller mislabel its own action in the
        audit trail — recording an override where none happened, or a plain
        edit where one did."""
        setup = engagement_with_draft
        login(api_client, setup["reviewer"])

        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review",
            json={"action": "override", "edited_status": "satisfied"},
        )

        assert response.status_code == 400

    def test_direct_service_call_with_override_action_is_refused(
        self, db: DBSession, engagement_with_draft: dict[str, Any]
    ) -> None:
        setup = engagement_with_draft
        service = FindingService(db)

        with pytest.raises(ValidationError):
            service.review(
                setup["finding"].id,
                actor_for(setup["reviewer"]),
                action=FindingAction.override,
                edited_status=ComplianceStatus.satisfied,
            )

    def test_history_row_is_written_with_every_approval(
        self, db: DBSession, engagement_with_draft: dict[str, Any]
    ) -> None:
        """03_DATA_MODEL.md §8.3: the Finding transition and its history row are
        written in a single transaction — never one without the other."""
        setup = engagement_with_draft
        service = FindingService(db)

        service.review(
            setup["finding"].id, actor_for(setup["reviewer"]), action=FindingAction.accept
        )

        history = db.scalars(
            select(FindingHistory).where(FindingHistory.finding_id == setup["finding"].id)
        ).all()
        assert len(history) == 1
        assert history[0].actor_id == setup["reviewer"].id


class TestReviewAuthorization:
    def test_unassigned_auditor_cannot_review(
        self, api_client: TestClient, make_user: Any, engagement_with_draft: dict[str, Any]
    ) -> None:
        """01_REQUIREMENTS.md: "Auditors can only act on Findings within
        engagements they're assigned to"."""
        setup = engagement_with_draft
        intruder = make_user(Role.auditor, password=PASSWORD)
        login(api_client, intruder)

        response = api_client.patch(
            f"/api/findings/{setup['finding'].id}/review", json={"action": "accept"}
        )

        assert response.status_code == 403

    def test_unassigned_auditor_cannot_read_the_finding_or_its_history(
        self, api_client: TestClient, make_user: Any, engagement_with_draft: dict[str, Any]
    ) -> None:
        setup = engagement_with_draft
        intruder = make_user(Role.auditor, password=PASSWORD)
        login(api_client, intruder)

        assert api_client.get(f"/api/findings/{setup['finding'].id}").status_code == 403
        assert api_client.get(f"/api/findings/{setup['finding'].id}/history").status_code == 403
        assert (
            api_client.get(f"/api/engagements/{setup['engagement'].id}/findings").status_code == 403
        )

    def test_unauthenticated_review_is_rejected(
        self, api_client: TestClient, engagement_with_draft: dict[str, Any]
    ) -> None:
        response = api_client.patch(
            f"/api/findings/{engagement_with_draft['finding'].id}/review",
            json={"action": "accept"},
        )
        assert response.status_code == 401

    def test_unknown_finding_returns_404(self, api_client: TestClient, make_user: Any) -> None:
        auditor = make_user(Role.auditor, password=PASSWORD)
        login(api_client, auditor)

        response = api_client.patch(
            f"/api/findings/{uuid.uuid4()}/review", json={"action": "accept"}
        )

        assert response.status_code == 404

    def test_reviewer_may_act_on_any_engagement(
        self, api_client: TestClient, make_user: Any, make_engagement: Any, make_finding: Any
    ) -> None:
        """Reviewers can act on any Finding at the firm, assigned or not."""
        auditor = make_user(Role.auditor, password=PASSWORD)
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        engagement = make_engagement(auditor, status=EngagementStatus.in_progress)
        finding = make_finding(engagement)
        login(api_client, reviewer)

        response = api_client.patch(f"/api/findings/{finding.id}/review", json={"action": "accept"})

        assert response.status_code == 200


class TestReviewQueue:
    def test_queue_distinguishes_drafts_from_determinations(
        self, api_client: TestClient, make_user: Any, make_engagement: Any, make_finding: Any
    ) -> None:
        """04_API_CONTRACT.md Security Notes: "the API response schema itself
        (not just the UI) should make it impossible to mistake a draft AI
        suggestion for a final determination"."""
        auditor = make_user(Role.auditor, password=PASSWORD)
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        engagement = make_engagement(auditor, status=EngagementStatus.in_progress)
        make_finding(engagement, status=FindingStatus.draft)
        make_finding(
            engagement,
            status=FindingStatus.approved,
            reviewed_by=reviewer,
            final_status=ComplianceStatus.partial,
        )
        login(api_client, auditor)

        items = api_client.get(f"/api/engagements/{engagement.id}/findings").json()

        drafts = [f for f in items if f["is_ai_draft"]]
        determined = [f for f in items if not f["is_ai_draft"]]
        assert len(drafts) == 1
        assert len(determined) == 1
        # A draft carries an AI suggestion but no determination and no reviewer.
        assert drafts[0]["ai_suggested_status"] is not None
        assert drafts[0]["final_status"] is None
        assert drafts[0]["reviewed_by"] is None
        # A determination carries both, in separate fields that are never merged.
        assert determined[0]["final_status"] == "partial"
        assert determined[0]["reviewed_by"] is not None

    def test_status_filter_applies(
        self, api_client: TestClient, make_user: Any, make_engagement: Any, make_finding: Any
    ) -> None:
        auditor = make_user(Role.auditor, password=PASSWORD)
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        engagement = make_engagement(auditor, status=EngagementStatus.in_progress)
        make_finding(engagement, status=FindingStatus.draft)
        make_finding(
            engagement,
            status=FindingStatus.approved,
            reviewed_by=reviewer,
            final_status=ComplianceStatus.satisfied,
        )
        login(api_client, auditor)

        drafts = api_client.get(f"/api/engagements/{engagement.id}/findings?status=draft").json()

        assert len(drafts) == 1
        assert drafts[0]["status"] == "draft"

    def test_needs_manual_review_filter_applies(
        self, api_client: TestClient, make_user: Any, make_engagement: Any, make_finding: Any
    ) -> None:
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor, status=EngagementStatus.in_progress)
        make_finding(engagement, needs_manual_review=True, ai_confidence=0.4)
        make_finding(engagement, needs_manual_review=False, ai_confidence=0.9)
        login(api_client, auditor)

        flagged = api_client.get(
            f"/api/engagements/{engagement.id}/findings?needs_manual_review=true"
        ).json()

        assert len(flagged) == 1
        assert flagged[0]["ai_confidence"] == 0.4
