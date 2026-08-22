"""Authentication business logic.

02_ARCHITECTURE.md §7.4: this layer knows nothing about HTTP. It returns a
session token and a user; setting the cookie is the route's job. That split is
what lets the seed script and the tests exercise the same login rules the API
uses, rather than a parallel implementation.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from sqlalchemy.orm import Session as DBSession

from app.auth.password import (
    hash_password,
    hash_session_token,
    needs_rehash,
    verify_password,
    waste_time_like_a_verification,
)
from app.config.settings import settings
from app.errors import InvalidCredentialsError, TooManyAttemptsError
from app.logging_setup import log_auth_attempt
from app.models.user import Session, User
from app.repositories.user import LoginAttemptRepository, SessionRepository, UserRepository

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "auditlens_session"


def _email_fingerprint(email: str) -> str:
    """A stable, non-reversible handle for logging.

    The audit log must record which account a login attempt targeted
    (02_ARCHITECTURE.md §7.8) without turning the log file into a list of the
    firm's email addresses.
    """
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


class AuthService:
    def __init__(self, db: DBSession) -> None:
        self._db = db
        self._users = UserRepository(db)
        self._sessions = SessionRepository(db)
        self._attempts = LoginAttemptRepository(db)

    # --- Login ---------------------------------------------------------------

    def login(self, email: str, password: str) -> tuple[User, str]:
        """Authenticate and open a session. Returns (user, raw session token).

        Ordering matters and is deliberate:
          1. Lockout is checked first, so a locked account cannot be probed with
             more guesses regardless of whether the password is right.
          2. An unknown email still pays the cost of a hash verification, so
             response timing does not reveal which emails exist.
          3. A deactivated account fails identically to a wrong password.
        """
        normalised = email.strip().lower()
        window = timedelta(minutes=settings.LOGIN_LOCKOUT_WINDOW_MINUTES)

        failures = self._attempts.count_recent_failures(email=normalised, window=window)
        if failures >= settings.LOGIN_MAX_ATTEMPTS:
            retry_after = self._seconds_until_unlock(normalised, window)
            log_auth_attempt(
                email_hash=_email_fingerprint(normalised), success=False, reason="locked_out"
            )
            raise TooManyAttemptsError(retry_after)

        user = self._users.get_by_email(normalised)

        if user is None:
            waste_time_like_a_verification()
            self._fail(normalised, "unknown_email")

        if not verify_password(password, user.password_hash):
            self._fail(normalised, "bad_password")

        if not user.is_active:
            # Same error as a wrong password: revealing that an account exists
            # but is deactivated is still user enumeration.
            self._fail(normalised, "inactive_account")

        # Transparently upgrade a hash stored under weaker parameters.
        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
            self._db.flush()

        self._attempts.record(email=normalised, succeeded=True)
        log_auth_attempt(email_hash=_email_fingerprint(normalised), success=True, reason="ok")

        token = secrets.token_urlsafe(32)
        self._sessions.create(
            user_id=user.id,
            token_hash=hash_session_token(token),
            absolute_expires_at=datetime.now(UTC)
            + timedelta(hours=settings.SESSION_ABSOLUTE_TIMEOUT_HOURS),
        )
        return user, token

    def _fail(self, email: str, reason: str) -> NoReturn:
        """Record the failure and raise the one generic credential error."""
        self._attempts.record(email=email, succeeded=False)
        log_auth_attempt(email_hash=_email_fingerprint(email), success=False, reason=reason)
        raise InvalidCredentialsError("Invalid email or password.")

    def _seconds_until_unlock(self, email: str, window: timedelta) -> int:
        oldest = self._attempts.oldest_failure_in_window(email=email, window=window)
        if oldest is None:
            return int(window.total_seconds())
        unlock_at = oldest + window
        remaining = (unlock_at - datetime.now(UTC)).total_seconds()
        return max(1, int(remaining))

    # --- Session resolution --------------------------------------------------

    def resolve_session(self, token: str) -> tuple[User, Session] | None:
        """Resolve a raw cookie token to its user, or None.

        The user's role and active flag are re-read from the User row on every
        request rather than cached in the session, so an Admin deactivating an
        account or changing a role takes effect on the next request instead of
        at the next login (03_DATA_MODEL.md → Session).
        """
        if not token:
            return None

        session = self._sessions.get_valid(
            hash_session_token(token),
            idle_timeout=timedelta(hours=settings.SESSION_IDLE_TIMEOUT_HOURS),
        )
        if session is None:
            return None

        user = self._users.get_by_id(session.user_id)
        if user is None or not user.is_active:
            # A deactivated user's live session is worthless from this point on.
            self._sessions.revoke(session)
            return None

        self._sessions.touch(session)
        return user, session

    def logout(self, session: Session) -> None:
        self._sessions.revoke(session)

    def deactivate_user_sessions(self, user_id: uuid.UUID) -> int:
        return self._sessions.revoke_all_for_user(user_id)
