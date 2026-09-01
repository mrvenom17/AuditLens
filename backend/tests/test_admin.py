"""Admin user-management and client-profile-document tests (ADR-012).

These close the drift found during TASK-022: both endpoint groups were
documented in 04_API_CONTRACT.md but never implemented.

05_SECURITY.md §10.3 makes the admin routes the single most security-relevant
surface after finalization — they are the only path by which `role` is ever set.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.models.enums import Role
from app.models.user import Session, User
from tests import filefixtures as ff

PASSWORD = "correct-horse-battery-staple"
NEW_PASSWORD = "a-sufficiently-long-password"


def login(client: TestClient, user: Any) -> None:
    assert (
        client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD}).status_code
        == 200
    )


@pytest.fixture
def admin(make_user: Any) -> User:
    return make_user(Role.admin, password=PASSWORD, name="Firm Admin")


class TestAdminRoleGate:
    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("GET", "/api/admin/users", None),
            (
                "POST",
                "/api/admin/users",
                {
                    "email": "new@testfirm.example",
                    "name": "New",
                    "role": "auditor",
                    "password": NEW_PASSWORD,
                },
            ),
            ("PATCH", f"/api/admin/users/{uuid.uuid4()}", {"is_active": False}),
        ],
    )
    @pytest.mark.parametrize("role", [Role.auditor, Role.reviewer])
    def test_non_admins_are_refused(
        self,
        api_client: TestClient,
        make_user: Any,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        role: Role,
    ) -> None:
        """A Reviewer has the most authority in the audit workflow and is
        still not an account administrator — the two are separate powers."""
        user = make_user(role, password=PASSWORD)
        login(api_client, user)

        response = api_client.request(method, path, json=body)

        assert response.status_code == 403

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/admin/users"),
            ("POST", "/api/admin/users"),
            ("PATCH", f"/api/admin/users/{uuid.uuid4()}"),
        ],
    )
    def test_unauthenticated_access_is_refused(
        self, api_client: TestClient, method: str, path: str
    ) -> None:
        assert api_client.request(method, path, json={}).status_code == 401


class TestCreateUser:
    def test_admin_creates_an_account_that_can_log_in(
        self, api_client: TestClient, admin: User
    ) -> None:
        """01_REQUIREMENTS.md: accounts are provisioned by an Admin only. The
        account is only really created if it can then be used."""
        login(api_client, admin)

        response = api_client.post(
            "/api/admin/users",
            json={
                "email": "casey@testfirm.example",
                "name": "Casey Auditor",
                "role": "auditor",
                "password": NEW_PASSWORD,
            },
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["email"] == "casey@testfirm.example"
        assert body["role"] == "auditor"
        assert body["is_active"] is True

        api_client.post("/api/auth/logout")
        signed_in = api_client.post(
            "/api/auth/login",
            json={"email": "casey@testfirm.example", "password": NEW_PASSWORD},
        )
        assert signed_in.status_code == 200

    def test_the_response_carries_no_credential_material(
        self, api_client: TestClient, admin: User
    ) -> None:
        """03_DATA_MODEL.md §8.4 classifies `password_hash` Secret."""
        login(api_client, admin)

        response = api_client.post(
            "/api/admin/users",
            json={
                "email": "casey@testfirm.example",
                "name": "Casey",
                "role": "auditor",
                "password": NEW_PASSWORD,
            },
        )

        assert "password" not in response.text.lower()
        assert "argon2" not in response.text
        assert NEW_PASSWORD not in response.text

    @pytest.mark.parametrize("role", ["auditor", "reviewer", "admin"])
    def test_every_documented_role_can_be_assigned(
        self, api_client: TestClient, admin: User, role: str
    ) -> None:
        login(api_client, admin)

        response = api_client.post(
            "/api/admin/users",
            json={
                "email": f"{role}-user@testfirm.example",
                "name": "Staff",
                "role": role,
                "password": NEW_PASSWORD,
            },
        )

        assert response.status_code == 201
        assert response.json()["role"] == role

    def test_duplicate_email_returns_409(
        self, api_client: TestClient, admin: User, make_user: Any
    ) -> None:
        existing = make_user(Role.auditor, password=PASSWORD)
        login(api_client, admin)

        response = api_client.post(
            "/api/admin/users",
            json={
                "email": existing.email,
                "name": "Impostor",
                "role": "admin",
                "password": NEW_PASSWORD,
            },
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "EMAIL_ALREADY_EXISTS"

    def test_duplicate_email_differing_only_in_case_is_refused(
        self, api_client: TestClient, admin: User, make_user: Any
    ) -> None:
        """Emails are normalised at the model layer, so a differently-cased
        duplicate must be caught here rather than raising an integrity error."""
        existing = make_user(Role.auditor, password=PASSWORD, email="casey@testfirm.example")
        login(api_client, admin)

        response = api_client.post(
            "/api/admin/users",
            json={
                "email": "CASEY@TESTFIRM.EXAMPLE",
                "name": "Impostor",
                "role": "admin",
                "password": NEW_PASSWORD,
            },
        )

        assert response.status_code == 409
        assert existing.role == Role.auditor

    @pytest.mark.parametrize(
        "bad_payload",
        [
            {"email": "not-an-email", "name": "X", "role": "auditor", "password": NEW_PASSWORD},
            {"email": "a@b.example", "name": "", "role": "auditor", "password": NEW_PASSWORD},
            {"email": "a@b.example", "name": "X", "role": "superuser", "password": NEW_PASSWORD},
            {"email": "a@b.example", "name": "X", "role": "auditor", "password": "short"},
            {"email": "a@b.example", "name": "X", "role": "auditor"},
        ],
    )
    def test_invalid_input_is_rejected(
        self, api_client: TestClient, db: DBSession, admin: User, bad_payload: dict[str, Any]
    ) -> None:
        login(api_client, admin)
        before = db.scalar(select(func.count()).select_from(User))

        response = api_client.post("/api/admin/users", json=bad_payload)

        assert response.status_code == 400
        assert db.scalar(select(func.count()).select_from(User)) == before

    def test_password_minimum_matches_the_standard_being_audited(
        self, api_client: TestClient, admin: User
    ) -> None:
        """PCI DSS v4.0.1 requirement 8.3.6 sets a 12-character minimum. A tool
        that assessed clients against it while holding its own staff to less
        would be difficult to defend."""
        login(api_client, admin)

        eleven = api_client.post(
            "/api/admin/users",
            json={
                "email": "a@testfirm.example",
                "name": "X",
                "role": "auditor",
                "password": "x" * 11,
            },
        )
        twelve = api_client.post(
            "/api/admin/users",
            json={
                "email": "b@testfirm.example",
                "name": "X",
                "role": "auditor",
                "password": "x" * 12,
            },
        )

        assert eleven.status_code == 400
        assert twelve.status_code == 201


class TestUpdateUser:
    def test_deactivating_a_user_revokes_their_live_sessions(
        self, api_client: TestClient, db: DBSession, admin: User, make_user: Any
    ) -> None:
        """A deactivated user whose cookie keeps working has not been
        deactivated — and the reason for deactivating is often that the account
        is suspected compromised."""
        target = make_user(Role.auditor, password=PASSWORD)
        login(api_client, target)
        assert api_client.get("/api/auth/me").status_code == 200
        target_cookies = dict(api_client.cookies)

        login(api_client, admin)
        response = api_client.patch(f"/api/admin/users/{target.id}", json={"is_active": False})
        assert response.status_code == 200
        assert response.json()["is_active"] is False

        api_client.cookies.clear()
        for name, value in target_cookies.items():
            api_client.cookies.set(name, value)
        assert api_client.get("/api/auth/me").status_code == 401

        revoked = db.scalars(select(Session).where(Session.user_id == target.id)).all()
        assert all(s.revoked_at is not None for s in revoked)

    def test_deactivated_users_are_not_deleted(
        self, api_client: TestClient, db: DBSession, admin: User, make_user: Any
    ) -> None:
        """03_DATA_MODEL.md → User lifecycle: soft delete only, so past actions
        stay attributable."""
        target = make_user(Role.auditor, password=PASSWORD)
        login(api_client, admin)

        api_client.patch(f"/api/admin/users/{target.id}", json={"is_active": False})

        assert db.get(User, target.id) is not None

    def test_deactivated_user_cannot_log_in(
        self, api_client: TestClient, admin: User, make_user: Any
    ) -> None:
        target = make_user(Role.auditor, password=PASSWORD)
        login(api_client, admin)
        api_client.patch(f"/api/admin/users/{target.id}", json={"is_active": False})
        api_client.post("/api/auth/logout")

        response = api_client.post(
            "/api/auth/login", json={"email": target.email, "password": PASSWORD}
        )

        # Same generic error as a wrong password — a distinct "disabled" message
        # would confirm the address exists (01_REQUIREMENTS.md).
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"

    def test_reactivating_restores_access(
        self, api_client: TestClient, admin: User, make_user: Any
    ) -> None:
        target = make_user(Role.auditor, password=PASSWORD)
        login(api_client, admin)
        api_client.patch(f"/api/admin/users/{target.id}", json={"is_active": False})
        api_client.patch(f"/api/admin/users/{target.id}", json={"is_active": True})
        api_client.post("/api/auth/logout")

        assert (
            api_client.post(
                "/api/auth/login", json={"email": target.email, "password": PASSWORD}
            ).status_code
            == 200
        )

    def test_role_change_takes_effect_on_the_next_request(
        self, api_client: TestClient, admin: User, make_user: Any, make_audit: Any
    ) -> None:
        """Role is re-read per request, so a promotion does not require the user
        to log out and back in."""
        target = make_user(Role.auditor, password=PASSWORD)
        audit = make_audit(target)
        login(api_client, target)
        assert api_client.post(f"/api/audits/{audit.id}/finalize").status_code == 403
        target_cookies = dict(api_client.cookies)

        login(api_client, admin)
        promoted = api_client.patch(f"/api/admin/users/{target.id}", json={"role": "reviewer"})
        assert promoted.status_code == 200
        assert promoted.json()["role"] == "reviewer"

        api_client.cookies.clear()
        for name, value in target_cookies.items():
            api_client.cookies.set(name, value)
        # No longer a 403 for role reasons — it now fails on audit state,
        # which is the next check in line.
        assert api_client.post(f"/api/audits/{audit.id}/finalize").status_code != 403

    def test_an_admin_cannot_deactivate_themselves(
        self, api_client: TestClient, admin: User
    ) -> None:
        """Recovering from self-lockout needs shell access to run the seed
        script. Refusing costs nothing, and a second Admin can always do it if
        it was genuinely intended."""
        login(api_client, admin)

        response = api_client.patch(f"/api/admin/users/{admin.id}", json={"is_active": False})

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CANNOT_MODIFY_SELF"

    def test_an_admin_cannot_demote_themselves(self, api_client: TestClient, admin: User) -> None:
        login(api_client, admin)

        response = api_client.patch(f"/api/admin/users/{admin.id}", json={"role": "auditor"})

        assert response.status_code == 409

    def test_another_admin_can_deactivate_them(
        self, api_client: TestClient, admin: User, make_user: Any
    ) -> None:
        """The self-modification guard is a foot-gun guard, not a privilege
        boundary — it must not make an Admin unremovable."""
        second_admin = make_user(Role.admin, password=PASSWORD)
        login(api_client, second_admin)

        response = api_client.patch(f"/api/admin/users/{admin.id}", json={"is_active": False})

        assert response.status_code == 200

    def test_unknown_user_returns_404(self, api_client: TestClient, admin: User) -> None:
        login(api_client, admin)

        response = api_client.patch(f"/api/admin/users/{uuid.uuid4()}", json={"is_active": False})

        assert response.status_code == 404

    def test_an_empty_patch_is_rejected(self, api_client: TestClient, admin: User) -> None:
        login(api_client, admin)
        assert api_client.patch(f"/api/admin/users/{admin.id}", json={}).status_code == 400

    def test_password_cannot_be_changed_through_the_api(
        self, api_client: TestClient, db: DBSession, admin: User, make_user: Any
    ) -> None:
        """An Admin resetting passwords through the API would let anyone who
        compromises an Admin session take over every account silently. Resets go
        through the seed script, which requires server access
        (05_SECURITY.md §10.2)."""
        target = make_user(Role.auditor, password=PASSWORD)
        original_hash = target.password_hash
        login(api_client, admin)

        api_client.patch(
            f"/api/admin/users/{target.id}",
            json={"is_active": True, "password": "attacker-chosen-password"},
        )

        db.refresh(target)
        assert target.password_hash == original_hash

    def test_listing_includes_deactivated_accounts(
        self, api_client: TestClient, admin: User, make_user: Any
    ) -> None:
        target = make_user(Role.auditor, password=PASSWORD)
        login(api_client, admin)
        api_client.patch(f"/api/admin/users/{target.id}", json={"is_active": False})

        listed = api_client.get("/api/admin/users").json()

        by_id = {u["id"]: u for u in listed}
        assert str(target.id) in by_id
        assert by_id[str(target.id)]["is_active"] is False

    def test_listing_exposes_no_credential_material(
        self, api_client: TestClient, admin: User, make_user: Any
    ) -> None:
        make_user(Role.reviewer, password=PASSWORD)
        login(api_client, admin)

        response = api_client.get("/api/admin/users")

        assert "password_hash" not in response.text
        assert "argon2" not in response.text


class TestClientProfileDocuments:
    def test_upload_returns_a_referencable_document(
        self, api_client: TestClient, make_user: Any
    ) -> None:
        auditor = make_user(Role.auditor, password=PASSWORD)
        login(api_client, auditor)

        response = api_client.post(
            "/api/client-profile-documents",
            files={"file": ("client-file.pdf", ff.valid_pdf(), "application/pdf")},
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["original_filename"] == "client-file.pdf"
        assert len(body["content_hash"]) == 64
        assert body["uploaded_by"] == str(auditor.id)

    def test_uploaded_document_can_seed_an_audit(
        self, api_client: TestClient, make_user: Any
    ) -> None:
        """This is the gap that made `source_document_ids` a dead parameter:
        the field validated against a table nothing could write to."""
        auditor = make_user(Role.auditor, password=PASSWORD)
        login(api_client, auditor)
        document = api_client.post(
            "/api/client-profile-documents",
            files={"file": ("client-file.pdf", ff.valid_pdf(), "application/pdf")},
        ).json()

        response = api_client.post(
            "/api/audits",
            json={
                "client_name": "Northwind Retail",
                "entity_type": "merchant",
                "merchant_level": "3",
                "source_document_ids": [document["id"]],
            },
        )

        assert response.status_code == 201, response.text

    def test_storage_path_is_never_returned(self, api_client: TestClient, make_user: Any) -> None:
        auditor = make_user(Role.auditor, password=PASSWORD)
        login(api_client, auditor)

        response = api_client.post(
            "/api/client-profile-documents",
            files={"file": ("client-file.pdf", ff.valid_pdf(), "application/pdf")},
        )

        assert "storage_path" not in response.json()

    def test_the_same_upload_validation_applies(
        self, api_client: TestClient, make_user: Any
    ) -> None:
        """The threat is identical to evidence upload — an untrusted file from
        outside the firm — so it must not get a second, weaker path."""
        auditor = make_user(Role.auditor, password=PASSWORD)
        login(api_client, auditor)

        disguised = api_client.post(
            "/api/client-profile-documents",
            files={"file": ("report.pdf", ff.disguised_executable(), "application/pdf")},
        )
        oversized = api_client.post(
            "/api/client-profile-documents",
            files={"file": ("big.pdf", ff.oversized(26), "application/pdf")},
        )
        zipped = api_client.post(
            "/api/client-profile-documents",
            files={"file": ("policy.docx", ff.plain_zip(), "application/zip")},
        )

        assert disguised.status_code == 400
        assert oversized.status_code == 413
        assert zipped.status_code == 400

    def test_profile_documents_are_stored_apart_from_evidence(
        self, api_client: TestClient, make_user: Any, isolated_file_storage: Any
    ) -> None:
        """Keeping the evidentiary record a clean, separately-backed-up set
        matters when the backup policy for audit records differs from the one
        for the firm's own working files (09_DEPLOYMENT.md)."""
        auditor = make_user(Role.auditor, password=PASSWORD)
        login(api_client, auditor)

        api_client.post(
            "/api/client-profile-documents",
            files={"file": ("client-file.pdf", ff.valid_pdf(), "application/pdf")},
        )

        written = [p for p in isolated_file_storage.rglob("*") if p.is_file()]
        assert written
        assert all("profile" in p.parts for p in written)

    def test_unauthenticated_upload_is_refused(self, api_client: TestClient) -> None:
        response = api_client.post(
            "/api/client-profile-documents",
            files={"file": ("client-file.pdf", ff.valid_pdf(), "application/pdf")},
        )
        assert response.status_code == 401

    @pytest.mark.parametrize("role", [Role.auditor, Role.reviewer, Role.admin])
    def test_any_authenticated_staff_member_may_upload(
        self, api_client: TestClient, make_user: Any, role: Role
    ) -> None:
        """03_DATA_MODEL.md → ClientProfileDocument: firm-wide, not
        audit-owned, so there is no assignment to check against."""
        user = make_user(role, password=PASSWORD)
        login(api_client, user)

        response = api_client.post(
            "/api/client-profile-documents",
            files={"file": ("client-file.pdf", ff.valid_pdf(), "application/pdf")},
        )

        assert response.status_code == 201
