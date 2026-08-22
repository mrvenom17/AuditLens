"""Authentication tests (TASK-008).

TASK-008 requires explicit tests for: valid login, invalid password, unknown
email (identical response), lockout after 5 attempts, and session cookie
attributes. 08_TESTING.md adds that lockout must not leak whether an email
exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.config.settings import settings
from app.errors import InvalidCredentialsError, TooManyAttemptsError
from app.models.enums import Role
from app.models.user import LoginAttempt, Session
from app.services.auth import SESSION_COOKIE_NAME, AuthService

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def auth(db: DBSession) -> AuthService:
    return AuthService(db)


class TestLoginSuccess:
    def test_valid_credentials_return_user_and_token(
        self, db: DBSession, auth: AuthService, make_user: Any
    ) -> None:
        user = make_user(Role.auditor, password=PASSWORD)

        logged_in, token = auth.login(user.email, PASSWORD)

        assert logged_in.id == user.id
        assert token

    def test_login_creates_a_server_side_session_row(
        self, db: DBSession, auth: AuthService, make_user: Any
    ) -> None:
        """05_SECURITY.md §10.2 specifies server-side sessions — the cookie is a
        handle, not a self-contained credential."""
        user = make_user(password=PASSWORD)
        _, token = auth.login(user.email, PASSWORD)

        session = db.scalar(select(Session).where(Session.user_id == user.id))
        assert session is not None
        assert session.revoked_at is None

        # The raw token is never stored — only its hash.
        assert token not in (session.token_hash, "")
        stored = db.scalar(select(Session.token_hash).where(Session.id == session.id))
        assert stored != token

    def test_absolute_expiry_is_24_hours(
        self, db: DBSession, auth: AuthService, make_user: Any
    ) -> None:
        user = make_user(password=PASSWORD)
        auth.login(user.email, PASSWORD)

        session = db.scalar(select(Session).where(Session.user_id == user.id))
        assert session is not None
        expected = datetime.now(UTC) + timedelta(hours=settings.SESSION_ABSOLUTE_TIMEOUT_HOURS)
        assert abs((session.absolute_expires_at - expected).total_seconds()) < 60

    def test_email_matching_is_case_insensitive_and_trimmed(
        self, auth: AuthService, make_user: Any
    ) -> None:
        user = make_user(email="Casey.Auditor@testfirm.example", password=PASSWORD)
        logged_in, _ = auth.login("  CASEY.AUDITOR@TESTFIRM.EXAMPLE  ", PASSWORD)
        assert logged_in.id == user.id


class TestLoginFailure:
    def test_wrong_password_is_rejected(self, auth: AuthService, make_user: Any) -> None:
        user = make_user(password=PASSWORD)
        with pytest.raises(InvalidCredentialsError):
            auth.login(user.email, "not-the-password")

    def test_unknown_email_is_rejected(self, auth: AuthService) -> None:
        with pytest.raises(InvalidCredentialsError):
            auth.login("nobody@testfirm.example", PASSWORD)

    def test_unknown_email_and_wrong_password_are_indistinguishable(
        self, auth: AuthService, make_user: Any
    ) -> None:
        """01_REQUIREMENTS.md, Explicitly Forbidden Behavior: the API must never
        reveal whether the email exists."""
        user = make_user(password=PASSWORD)

        with pytest.raises(InvalidCredentialsError) as wrong_password:
            auth.login(user.email, "wrong")
        with pytest.raises(InvalidCredentialsError) as unknown_email:
            auth.login("nobody@testfirm.example", "wrong")

        assert str(wrong_password.value) == str(unknown_email.value)
        assert wrong_password.value.code == unknown_email.value.code
        assert wrong_password.value.status_code == unknown_email.value.status_code

    def test_deactivated_account_fails_like_a_wrong_password(
        self, auth: AuthService, make_user: Any
    ) -> None:
        """A distinct "account disabled" error would confirm the address exists."""
        user = make_user(password=PASSWORD, is_active=False)

        with pytest.raises(InvalidCredentialsError) as inactive:
            auth.login(user.email, PASSWORD)
        with pytest.raises(InvalidCredentialsError) as unknown:
            auth.login("nobody@testfirm.example", PASSWORD)

        assert str(inactive.value) == str(unknown.value)

    def test_failed_attempt_creates_no_session(
        self, db: DBSession, auth: AuthService, make_user: Any
    ) -> None:
        user = make_user(password=PASSWORD)
        with pytest.raises(InvalidCredentialsError):
            auth.login(user.email, "wrong")

        assert db.scalar(select(func.count()).select_from(Session)) == 0


class TestLockout:
    def test_locks_after_five_failures(self, auth: AuthService, make_user: Any) -> None:
        """01_REQUIREMENTS.md acceptance criterion: after 5 failed attempts, a
        6th attempt with the *correct* password still returns 429."""
        user = make_user(password=PASSWORD)

        for _ in range(settings.LOGIN_MAX_ATTEMPTS):
            with pytest.raises(InvalidCredentialsError):
                auth.login(user.email, "wrong")

        with pytest.raises(TooManyAttemptsError) as locked:
            auth.login(user.email, PASSWORD)
        assert locked.value.status_code == 429
        assert locked.value.retry_after_seconds > 0

    def test_four_failures_do_not_lock(self, auth: AuthService, make_user: Any) -> None:
        """The boundary matters in both directions — locking a user out one
        attempt early is a self-inflicted denial of service."""
        user = make_user(password=PASSWORD)
        for _ in range(settings.LOGIN_MAX_ATTEMPTS - 1):
            with pytest.raises(InvalidCredentialsError):
                auth.login(user.email, "wrong")

        logged_in, _ = auth.login(user.email, PASSWORD)
        assert logged_in.id == user.id

    def test_successful_login_resets_the_counter(self, auth: AuthService, make_user: Any) -> None:
        user = make_user(password=PASSWORD)
        for _ in range(4):
            with pytest.raises(InvalidCredentialsError):
                auth.login(user.email, "wrong")

        auth.login(user.email, PASSWORD)

        # Four more failures must not immediately re-lock: the counter is
        # consecutive failures since the last success, not a rolling total.
        for _ in range(4):
            with pytest.raises(InvalidCredentialsError):
                auth.login(user.email, "wrong")
        logged_in, _ = auth.login(user.email, PASSWORD)
        assert logged_in.id == user.id

    def test_lockout_expires_after_the_window(
        self, db: DBSession, auth: AuthService, make_user: Any
    ) -> None:
        user = make_user(password=PASSWORD)
        for _ in range(settings.LOGIN_MAX_ATTEMPTS):
            with pytest.raises(InvalidCredentialsError):
                auth.login(user.email, "wrong")

        # Age the recorded attempts past the window rather than sleeping.
        old = datetime.now(UTC) - timedelta(minutes=settings.LOGIN_LOCKOUT_WINDOW_MINUTES + 1)
        for attempt in db.scalars(select(LoginAttempt)).all():
            attempt.created_at = old
        db.flush()

        logged_in, _ = auth.login(user.email, PASSWORD)
        assert logged_in.id == user.id

    def test_lockout_applies_to_unknown_emails_too(self, auth: AuthService) -> None:
        """Locking only real accounts would make the 429-vs-401 difference an
        enumeration oracle — the exact leak the lockout is meant not to create."""
        email = "nobody@testfirm.example"
        for _ in range(settings.LOGIN_MAX_ATTEMPTS):
            with pytest.raises(InvalidCredentialsError):
                auth.login(email, "wrong")

        with pytest.raises(TooManyAttemptsError):
            auth.login(email, "wrong")

    def test_lockout_is_per_account_not_global(self, auth: AuthService, make_user: Any) -> None:
        """One user's failed attempts must not lock a colleague out."""
        victim = make_user(password=PASSWORD)
        bystander = make_user(password=PASSWORD)

        for _ in range(settings.LOGIN_MAX_ATTEMPTS):
            with pytest.raises(InvalidCredentialsError):
                auth.login(victim.email, "wrong")

        logged_in, _ = auth.login(bystander.email, PASSWORD)
        assert logged_in.id == bystander.id


class TestSessionResolution:
    def test_valid_token_resolves_to_the_user(self, auth: AuthService, make_user: Any) -> None:
        user = make_user(password=PASSWORD)
        _, token = auth.login(user.email, PASSWORD)

        resolved = auth.resolve_session(token)
        assert resolved is not None
        assert resolved[0].id == user.id

    def test_unknown_and_empty_tokens_resolve_to_nothing(self, auth: AuthService) -> None:
        assert auth.resolve_session("") is None
        assert auth.resolve_session("not-a-real-token") is None

    def test_revoked_session_stops_resolving(self, auth: AuthService, make_user: Any) -> None:
        user = make_user(password=PASSWORD)
        _, token = auth.login(user.email, PASSWORD)
        resolved = auth.resolve_session(token)
        assert resolved is not None

        auth.logout(resolved[1])
        assert auth.resolve_session(token) is None

    def test_session_past_absolute_expiry_stops_resolving(
        self, db: DBSession, auth: AuthService, make_user: Any
    ) -> None:
        """The 24-hour cap is absolute and is never extended by activity."""
        user = make_user(password=PASSWORD)
        _, token = auth.login(user.email, PASSWORD)

        session = db.scalar(select(Session).where(Session.user_id == user.id))
        assert session is not None
        session.absolute_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.flush()

        assert auth.resolve_session(token) is None

    def test_idle_session_stops_resolving(
        self, db: DBSession, auth: AuthService, make_user: Any
    ) -> None:
        user = make_user(password=PASSWORD)
        _, token = auth.login(user.email, PASSWORD)

        session = db.scalar(select(Session).where(Session.user_id == user.id))
        assert session is not None
        session.last_seen_at = datetime.now(UTC) - timedelta(
            hours=settings.SESSION_IDLE_TIMEOUT_HOURS + 1
        )
        db.flush()

        assert auth.resolve_session(token) is None

    def test_deactivating_a_user_invalidates_their_live_session(
        self, db: DBSession, auth: AuthService, make_user: Any
    ) -> None:
        """Role and active state are re-read per request, so an Admin's
        deactivation takes effect immediately rather than at next login."""
        user = make_user(password=PASSWORD)
        _, token = auth.login(user.email, PASSWORD)
        assert auth.resolve_session(token) is not None

        user.is_active = False
        db.flush()

        assert auth.resolve_session(token) is None

    def test_role_change_is_reflected_without_re_login(
        self, db: DBSession, auth: AuthService, make_user: Any
    ) -> None:
        user = make_user(Role.auditor, password=PASSWORD)
        _, token = auth.login(user.email, PASSWORD)

        user.role = Role.reviewer
        db.flush()

        resolved = auth.resolve_session(token)
        assert resolved is not None
        assert resolved[0].role == Role.reviewer


class TestPasswordHashing:
    def test_hashes_are_argon2id_and_salted(self) -> None:
        from app.auth.password import hash_password, verify_password

        first = hash_password(PASSWORD)
        second = hash_password(PASSWORD)

        assert first.startswith("$argon2id$")
        assert first != second, "identical passwords must not produce identical hashes"
        assert verify_password(PASSWORD, first)
        assert verify_password(PASSWORD, second)

    def test_verify_returns_false_rather_than_raising(self) -> None:
        from app.auth.password import verify_password

        assert verify_password("anything", "not-a-valid-hash") is False
        assert verify_password("wrong", hash_password_for_test()) is False

    def test_session_tokens_are_hashed_before_storage(self) -> None:
        from app.auth.password import hash_session_token

        digest = hash_session_token("some-token")
        assert len(digest) == 64
        assert digest != "some-token"
        assert hash_session_token("some-token") == digest


def hash_password_for_test() -> str:
    from app.auth.password import hash_password

    return hash_password(PASSWORD)


class TestLoginEndpoint:
    """04_API_CONTRACT.md → POST /api/auth/login, exercised through HTTP."""

    def test_successful_login_sets_a_hardened_cookie(
        self, api_client: TestClient, make_user: Any
    ) -> None:
        """TASK-008 names session cookie attributes as a required test."""
        user = make_user(Role.auditor, password=PASSWORD)

        response = api_client.post(
            "/api/auth/login", json={"email": user.email, "password": PASSWORD}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["user_id"] == str(user.id)
        assert body["role"] == "auditor"
        assert body["name"] == user.name
        assert "password" not in str(body).lower()

        cookie_header = response.headers["set-cookie"]
        assert "HttpOnly" in cookie_header
        assert "SameSite=strict" in cookie_header.replace("SameSite=Strict", "SameSite=strict")
        assert SESSION_COOKIE_NAME in cookie_header

    def test_invalid_password_returns_401_and_no_cookie(
        self, api_client: TestClient, make_user: Any
    ) -> None:
        user = make_user(password=PASSWORD)

        response = api_client.post(
            "/api/auth/login", json={"email": user.email, "password": "wrong"}
        )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"
        assert "set-cookie" not in response.headers

    def test_unknown_email_returns_an_identical_body(
        self, api_client: TestClient, make_user: Any
    ) -> None:
        user = make_user(password=PASSWORD)

        wrong_password = api_client.post(
            "/api/auth/login", json={"email": user.email, "password": "wrong"}
        )
        unknown_email = api_client.post(
            "/api/auth/login", json={"email": "nobody@testfirm.example", "password": "wrong"}
        )

        assert wrong_password.status_code == unknown_email.status_code == 401
        # request_id differs by design; everything else must match exactly.
        assert wrong_password.json()["error"]["code"] == unknown_email.json()["error"]["code"]
        assert wrong_password.json()["error"]["message"] == unknown_email.json()["error"]["message"]

    def test_lockout_returns_429_with_retry_after(
        self, api_client: TestClient, make_user: Any
    ) -> None:
        user = make_user(password=PASSWORD)
        for _ in range(settings.LOGIN_MAX_ATTEMPTS):
            api_client.post("/api/auth/login", json={"email": user.email, "password": "wrong"})

        response = api_client.post(
            "/api/auth/login", json={"email": user.email, "password": PASSWORD}
        )

        assert response.status_code == 429
        assert response.json()["error"]["code"] == "TOO_MANY_ATTEMPTS"
        assert response.json()["error"]["retry_after"] > 0
        assert "Retry-After" in response.headers

    def test_missing_fields_are_rejected_before_any_credential_work(
        self, api_client: TestClient
    ) -> None:
        response = api_client.post("/api/auth/login", json={"email": "", "password": ""})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_me_requires_authentication(self, api_client: TestClient) -> None:
        response = api_client.get("/api/auth/me")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"

    def test_authenticated_request_carries_the_session(
        self, api_client: TestClient, make_user: Any
    ) -> None:
        user = make_user(Role.reviewer, password=PASSWORD)
        api_client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD})

        response = api_client.get("/api/auth/me")

        assert response.status_code == 200
        assert response.json() == {
            "user_id": str(user.id),
            "email": user.email,
            "name": user.name,
            "role": "reviewer",
        }

    def test_logout_revokes_the_session_server_side(
        self, api_client: TestClient, make_user: Any
    ) -> None:
        user = make_user(password=PASSWORD)
        api_client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD})
        assert api_client.get("/api/auth/me").status_code == 200

        assert api_client.post("/api/auth/logout").status_code == 204

        # Even if the client kept the cookie, the server-side row is revoked.
        assert api_client.get("/api/auth/me").status_code == 401

    def test_a_forged_cookie_is_rejected(self, api_client: TestClient) -> None:
        """A resource ID or a guessed token is never proof of anything."""
        api_client.cookies.set(SESSION_COOKIE_NAME, "forged-token-value")
        response = api_client.get("/api/auth/me")
        assert response.status_code == 401
