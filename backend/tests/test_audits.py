"""Engagement endpoint tests (TASK-011, TASK-012).

Acceptance criteria come from 01_REQUIREMENTS.md § Engagement Creation, and the
authorization cases from 04_API_CONTRACT.md and 08_TESTING.md § Security Tests.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.models.engagement import Engagement
from app.models.enums import EngagementStatus, Role

PASSWORD = "correct-horse-battery-staple"


def login(client: TestClient, user: Any) -> None:
    response = client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD})
    assert response.status_code == 200, response.text


VALID_PAYLOAD = {
    "client_name": "Northwind Retail Ltd",
    "entity_type": "merchant",
    "merchant_level": "3",
    "annual_transaction_volume": 450_000,
    "existing_saq_type": "D",
    "tech_stack_summary": "Magento on AWS, Stripe for card capture.",
}


class TestCreateEngagement:
    def test_valid_payload_creates_an_intake_engagement_and_assigns_the_creator(
        self, api_client: TestClient, make_user: Any
    ) -> None:
        """01_REQUIREMENTS.md acceptance criterion: status `intake`, and the
        creating user is in its assigned-auditors list."""
        auditor = make_user(Role.auditor, password=PASSWORD)
        login(api_client, auditor)

        response = api_client.post("/api/engagements", json=VALID_PAYLOAD)

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "intake"
        assert body["client_name"] == "Northwind Retail Ltd"
        assert body["created_by"] == str(auditor.id)
        assert str(auditor.id) in body["assigned_user_ids"]

    def test_merchant_without_level_is_rejected_and_creates_nothing(
        self, api_client: TestClient, db: DBSession, make_user: Any
    ) -> None:
        """01_REQUIREMENTS.md acceptance criterion: 400, and no Engagement row."""
        auditor = make_user(Role.auditor, password=PASSWORD)
        login(api_client, auditor)
        payload = {**VALID_PAYLOAD}
        del payload["merchant_level"]

        response = api_client.post("/api/engagements", json=payload)

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
        assert db.scalar(select(func.count()).select_from(Engagement)) == 0

    def test_service_provider_does_not_take_a_merchant_level(
        self, api_client: TestClient, make_user: Any
    ) -> None:
        auditor = make_user(Role.auditor, password=PASSWORD)
        login(api_client, auditor)

        ok = api_client.post(
            "/api/engagements",
            json={"client_name": "Acme Processing", "entity_type": "service_provider"},
        )
        assert ok.status_code == 201
        assert ok.json()["merchant_level"] is None

        contradictory = api_client.post(
            "/api/engagements",
            json={
                "client_name": "Acme Processing",
                "entity_type": "service_provider",
                "merchant_level": "1",
            },
        )
        assert contradictory.status_code == 400

    @pytest.mark.parametrize(
        "bad_payload",
        [
            {"client_name": "", "entity_type": "merchant", "merchant_level": "1"},
            {"client_name": "   ", "entity_type": "merchant", "merchant_level": "1"},
            {"client_name": "x" * 201, "entity_type": "merchant", "merchant_level": "1"},
            {"client_name": "Ok", "entity_type": "not_a_real_type"},
            {"client_name": "Ok", "entity_type": "merchant", "merchant_level": "9"},
            {
                "client_name": "Ok",
                "entity_type": "merchant",
                "merchant_level": "1",
                "annual_transaction_volume": -5,
            },
            {
                "client_name": "Ok",
                "entity_type": "merchant",
                "merchant_level": "1",
                "tech_stack_summary": "x" * 5001,
            },
        ],
    )
    def test_invalid_input_is_rejected_server_side(
        self, api_client: TestClient, make_user: Any, bad_payload: dict[str, Any]
    ) -> None:
        """06_ENGINEERING_RULES.md § Validation: validated at the server boundary
        regardless of what the frontend already checked."""
        auditor = make_user(Role.auditor, password=PASSWORD)
        login(api_client, auditor)

        response = api_client.post("/api/engagements", json=bad_payload)

        assert response.status_code == 400

    def test_client_supplied_status_and_ownership_are_ignored(
        self, api_client: TestClient, make_user: Any
    ) -> None:
        """05_SECURITY.md §10.3: client-provided ownership is never trusted.

        The schema has no such fields, so extra keys are dropped rather than
        honoured — this test exists to catch a future edit that adds them.
        """
        auditor = make_user(Role.auditor, password=PASSWORD)
        other = make_user(Role.auditor, password=PASSWORD)
        login(api_client, auditor)

        response = api_client.post(
            "/api/engagements",
            json={
                **VALID_PAYLOAD,
                "status": "finalized",
                "created_by": str(other.id),
                "finalized_by": str(other.id),
            },
        )

        assert response.status_code == 201
        assert response.json()["status"] == "intake"
        assert response.json()["created_by"] == str(auditor.id)
        assert response.json()["finalized_by"] is None

    def test_unknown_source_document_ids_are_rejected(
        self, api_client: TestClient, make_user: Any
    ) -> None:
        auditor = make_user(Role.auditor, password=PASSWORD)
        login(api_client, auditor)

        response = api_client.post(
            "/api/engagements",
            json={**VALID_PAYLOAD, "source_document_ids": [str(uuid.uuid4())]},
        )

        assert response.status_code == 400
        assert "source_document_ids" in response.json()["error"]["message"]

    def test_duplicate_client_names_are_allowed(
        self, api_client: TestClient, make_user: Any
    ) -> None:
        """01_REQUIREMENTS.md Edge Cases: the same client may have several
        engagements over time, e.g. an annual re-assessment."""
        auditor = make_user(Role.auditor, password=PASSWORD)
        login(api_client, auditor)

        first = api_client.post("/api/engagements", json=VALID_PAYLOAD)
        second = api_client.post("/api/engagements", json=VALID_PAYLOAD)

        assert first.status_code == second.status_code == 201
        assert first.json()["id"] != second.json()["id"]

    def test_unauthenticated_creation_is_rejected(self, api_client: TestClient) -> None:
        response = api_client.post("/api/engagements", json=VALID_PAYLOAD)
        assert response.status_code == 401

    def test_admin_cannot_create_an_engagement(
        self, api_client: TestClient, make_user: Any
    ) -> None:
        """04_API_CONTRACT.md restricts this to auditor or reviewer. An Admin
        manages accounts and the corpus (00_PRODUCT.md §5.3), not casework."""
        admin = make_user(Role.admin, password=PASSWORD)
        login(api_client, admin)

        response = api_client.post("/api/engagements", json=VALID_PAYLOAD)

        assert response.status_code == 403


class TestReadEngagement:
    def test_assigned_auditor_can_read_it(
        self, api_client: TestClient, make_user: Any, make_engagement: Any
    ) -> None:
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor)
        login(api_client, auditor)

        response = api_client.get(f"/api/engagements/{engagement.id}")

        assert response.status_code == 200
        assert response.json()["id"] == str(engagement.id)
        assert response.json()["counts"]["findings_draft"] == 0

    def test_unassigned_auditor_gets_403_not_the_data(
        self, api_client: TestClient, make_user: Any, make_engagement: Any
    ) -> None:
        """08_TESTING.md § Security Tests — the core IDOR case."""
        owner = make_user(Role.auditor, password=PASSWORD)
        intruder = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(owner, client_name="Confidential Client")
        login(api_client, intruder)

        response = api_client.get(f"/api/engagements/{engagement.id}")

        assert response.status_code == 403
        assert "Confidential Client" not in response.text

    def test_nonexistent_engagement_gives_404(self, api_client: TestClient, make_user: Any) -> None:
        """04_API_CONTRACT.md distinguishes 403 (exists, no access) from 404."""
        auditor = make_user(Role.auditor, password=PASSWORD)
        login(api_client, auditor)

        response = api_client.get(f"/api/engagements/{uuid.uuid4()}")

        assert response.status_code == 404

    def test_reviewer_can_read_any_engagement(
        self, api_client: TestClient, make_user: Any, make_engagement: Any
    ) -> None:
        owner = make_user(Role.auditor, password=PASSWORD)
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        engagement = make_engagement(owner)
        login(api_client, reviewer)

        assert api_client.get(f"/api/engagements/{engagement.id}").status_code == 200

    def test_unauthenticated_read_is_rejected(
        self, api_client: TestClient, make_user: Any, make_engagement: Any
    ) -> None:
        owner = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(owner)

        response = api_client.get(f"/api/engagements/{engagement.id}")

        assert response.status_code == 401

    def test_malformed_id_is_a_validation_error_not_a_crash(
        self, api_client: TestClient, make_user: Any
    ) -> None:
        auditor = make_user(Role.auditor, password=PASSWORD)
        login(api_client, auditor)

        response = api_client.get("/api/engagements/not-a-uuid")

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


class TestListEngagements:
    def test_auditor_sees_only_their_own(
        self, api_client: TestClient, make_user: Any, make_engagement: Any
    ) -> None:
        mine = make_user(Role.auditor, password=PASSWORD)
        theirs = make_user(Role.auditor, password=PASSWORD)
        make_engagement(mine, client_name="Mine")
        make_engagement(theirs, client_name="Theirs")
        login(api_client, mine)

        body = api_client.get("/api/engagements").json()

        assert body["total"] == 1
        assert [e["client_name"] for e in body["items"]] == ["Mine"]

    def test_list_omits_the_sensitive_tech_stack_summary(
        self, api_client: TestClient, make_user: Any, make_engagement: Any
    ) -> None:
        """03_DATA_MODEL.md §8.4 classifies it Sensitive; a list view has no
        need for it, so it is not in the summary schema at all."""
        auditor = make_user(Role.auditor, password=PASSWORD)
        make_engagement(auditor)
        login(api_client, auditor)

        body = api_client.get("/api/engagements").json()

        assert "tech_stack_summary" not in body["items"][0]

    def test_reviewer_sees_all(
        self, api_client: TestClient, make_user: Any, make_engagement: Any
    ) -> None:
        a = make_user(Role.auditor, password=PASSWORD)
        b = make_user(Role.auditor, password=PASSWORD)
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        make_engagement(a)
        make_engagement(b)
        login(api_client, reviewer)

        assert api_client.get("/api/engagements").json()["total"] == 2

    def test_status_filter_applies(
        self, api_client: TestClient, make_user: Any, make_engagement: Any
    ) -> None:
        auditor = make_user(Role.auditor, password=PASSWORD)
        make_engagement(auditor, status=EngagementStatus.intake)
        make_engagement(auditor, status=EngagementStatus.in_progress)
        login(api_client, auditor)

        body = api_client.get("/api/engagements?status=in_progress").json()

        assert body["total"] == 1
        assert body["items"][0]["status"] == "in_progress"

    def test_limit_is_capped(self, api_client: TestClient, make_user: Any) -> None:
        auditor = make_user(Role.auditor, password=PASSWORD)
        login(api_client, auditor)

        assert api_client.get("/api/engagements?limit=10000").status_code == 400


class TestAssignments:
    def test_reviewer_can_assign_a_user(
        self, api_client: TestClient, make_user: Any, make_engagement: Any
    ) -> None:
        owner = make_user(Role.auditor, password=PASSWORD)
        colleague = make_user(Role.auditor, password=PASSWORD)
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        engagement = make_engagement(owner)
        login(api_client, reviewer)

        response = api_client.post(
            f"/api/engagements/{engagement.id}/assignments",
            json={"user_id": str(colleague.id)},
        )

        assert response.status_code == 201

    def test_auditor_cannot_assign_themselves_to_another_engagement(
        self, api_client: TestClient, make_user: Any, make_engagement: Any
    ) -> None:
        """The obvious privilege-escalation path: if an Auditor could create
        their own assignment, the entire ownership boundary would be optional."""
        owner = make_user(Role.auditor, password=PASSWORD)
        intruder = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(owner)
        login(api_client, intruder)

        response = api_client.post(
            f"/api/engagements/{engagement.id}/assignments",
            json={"user_id": str(intruder.id)},
        )

        assert response.status_code == 403
        assert api_client.get(f"/api/engagements/{engagement.id}").status_code == 403

    def test_duplicate_assignment_returns_409(
        self, api_client: TestClient, make_user: Any, make_engagement: Any
    ) -> None:
        owner = make_user(Role.auditor, password=PASSWORD)
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        engagement = make_engagement(owner)
        login(api_client, reviewer)

        response = api_client.post(
            f"/api/engagements/{engagement.id}/assignments",
            json={"user_id": str(owner.id)},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ALREADY_ASSIGNED"

    def test_assigning_an_unknown_user_returns_404(
        self, api_client: TestClient, make_user: Any, make_engagement: Any
    ) -> None:
        owner = make_user(Role.auditor, password=PASSWORD)
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        engagement = make_engagement(owner)
        login(api_client, reviewer)

        response = api_client.post(
            f"/api/engagements/{engagement.id}/assignments",
            json={"user_id": str(uuid.uuid4())},
        )

        assert response.status_code == 404

    def test_unassigning_revokes_access(
        self, api_client: TestClient, make_user: Any, make_engagement: Any
    ) -> None:
        owner = make_user(Role.auditor, password=PASSWORD)
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        engagement = make_engagement(owner)
        login(api_client, reviewer)

        removed = api_client.delete(f"/api/engagements/{engagement.id}/assignments/{owner.id}")
        assert removed.status_code == 204

        login(api_client, owner)
        assert api_client.get(f"/api/engagements/{engagement.id}").status_code == 403

    def test_cannot_assign_to_a_finalized_engagement(
        self, api_client: TestClient, make_user: Any, make_engagement: Any
    ) -> None:
        owner = make_user(Role.auditor, password=PASSWORD)
        colleague = make_user(Role.auditor, password=PASSWORD)
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        engagement = make_engagement(owner, status=EngagementStatus.finalized)
        login(api_client, reviewer)

        response = api_client.post(
            f"/api/engagements/{engagement.id}/assignments",
            json={"user_id": str(colleague.id)},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ENGAGEMENT_FINALIZED"
