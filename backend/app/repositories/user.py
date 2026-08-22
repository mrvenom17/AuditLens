"""User, Session and LoginAttempt data access.

02_ARCHITECTURE.md §7.4: this is the only layer that touches the ORM session
directly, and it contains no business logic beyond data-shape concerns. The
lockout *decision* lives in the auth service; what lives here is the query that
counts recent failures.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.models.enums import Role
from app.models.user import LoginAttempt, Session, User


class UserRepository:
    def __init__(self, db: DBSession) -> None:
        self._db = db

    def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return self._db.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        """Look up by normalised email. Returns inactive users too — the caller
        decides how to respond, so that a deactivated account is indistinguishable
        from a wrong password (01_REQUIREMENTS.md § Failure Cases)."""
        return self._db.scalar(select(User).where(User.email == email.strip().lower()))

    def list_users(self, *, include_inactive: bool = True) -> list[User]:
        stmt = select(User).order_by(User.name)
        if not include_inactive:
            stmt = stmt.where(User.is_active.is_(True))
        return list(self._db.scalars(stmt).all())

    def create(self, *, email: str, password_hash: str, name: str, role: Role) -> User:
        user = User(
            email=email.strip().lower(),
            password_hash=password_hash,
            name=name,
            role=role,
            is_active=True,
        )
        self._db.add(user)
        self._db.flush()
        return user


class SessionRepository:
    def __init__(self, db: DBSession) -> None:
        self._db = db

    def create(
        self, *, user_id: uuid.UUID, token_hash: str, absolute_expires_at: datetime
    ) -> Session:
        session = Session(
            user_id=user_id,
            token_hash=token_hash,
            absolute_expires_at=absolute_expires_at,
        )
        self._db.add(session)
        self._db.flush()
        return session

    def get_valid(self, token_hash: str, *, idle_timeout: timedelta) -> Session | None:
        """Fetch a session only if it is currently valid.

        Every validity condition is expressed in the query rather than checked
        afterwards, so there is no window in which an expired session object
        exists in application memory and could be used by mistake.
        """
        now = datetime.now(UTC)
        return self._db.scalar(
            select(Session).where(
                Session.token_hash == token_hash,
                Session.revoked_at.is_(None),
                Session.absolute_expires_at > now,
                Session.last_seen_at > now - idle_timeout,
            )
        )

    def touch(self, session: Session) -> None:
        """Refresh the idle timer."""
        session.last_seen_at = datetime.now(UTC)
        self._db.flush()

    def revoke(self, session: Session) -> None:
        session.revoked_at = datetime.now(UTC)
        self._db.flush()

    def revoke_all_for_user(self, user_id: uuid.UUID) -> int:
        """Used when an account is deactivated — an existing cookie must stop
        working immediately, not at the next idle timeout."""
        sessions = self._db.scalars(
            select(Session).where(Session.user_id == user_id, Session.revoked_at.is_(None))
        ).all()
        now = datetime.now(UTC)
        for session in sessions:
            session.revoked_at = now
        self._db.flush()
        return len(sessions)


class LoginAttemptRepository:
    def __init__(self, db: DBSession) -> None:
        self._db = db

    def record(self, *, email: str, succeeded: bool) -> None:
        self._db.add(LoginAttempt(email=email.strip().lower(), succeeded=succeeded))
        self._db.flush()

    def count_recent_failures(self, *, email: str, window: timedelta) -> int:
        """Count consecutive-window failures since the last success.

        Counting only since the last successful login is what makes the lockout
        reset on success, which is the behaviour 01_REQUIREMENTS.md describes
        ("5 *consecutive* failed attempts").
        """
        normalised = email.strip().lower()
        since = datetime.now(UTC) - window

        last_success = self._db.scalar(
            select(func.max(LoginAttempt.created_at)).where(
                LoginAttempt.email == normalised,
                LoginAttempt.succeeded.is_(True),
                LoginAttempt.created_at >= since,
            )
        )
        cutoff = max(since, last_success) if last_success else since

        count = self._db.scalar(
            select(func.count())
            .select_from(LoginAttempt)
            .where(
                LoginAttempt.email == normalised,
                LoginAttempt.succeeded.is_(False),
                LoginAttempt.created_at > cutoff,
            )
        )
        return int(count or 0)

    def oldest_failure_in_window(self, *, email: str, window: timedelta) -> datetime | None:
        """When the current lockout window started, so `retry_after` can be
        computed rather than guessed."""
        since = datetime.now(UTC) - window
        return self._db.scalar(
            select(func.min(LoginAttempt.created_at)).where(
                LoginAttempt.email == email.strip().lower(),
                LoginAttempt.succeeded.is_(False),
                LoginAttempt.created_at >= since,
            )
        )
