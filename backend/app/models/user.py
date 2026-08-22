"""User, Session and LoginAttempt (03_DATA_MODEL.md; Session/LoginAttempt per ADR-011)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, created_at_column, uuid_pk
from app.models.enums import Role


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    # Sensitivity: Secret (03_DATA_MODEL.md §8.4). Never serialised into any
    # Pydantic response schema and never logged.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[Role] = mapped_column(Enum(Role, name="user_role"), nullable=False)
    # Deactivated, never hard-deleted — past actions must stay attributable.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    sessions: Mapped[list[Session]] = relationship(back_populates="user")


class Session(Base):
    """A server-side session. The cookie holds an opaque token; only its
    SHA-256 hash is stored, so database read access cannot mint a valid session.
    """

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        # RESTRICT, not CASCADE: users are deactivated rather than deleted
        # (03_DATA_MODEL.md §8.3), so a cascade path here should never fire.
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = created_at_column()
    last_seen_at: Mapped[datetime] = created_at_column()
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")


class LoginAttempt(Base):
    """Backs the 5-per-15-minutes lockout (01_REQUIREMENTS.md § User Authentication).

    Rows are written for unknown emails too. Recording only real accounts would
    make the table itself an enumeration oracle for anyone who could read it.
    """

    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_attempts_email_created", "email", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = created_at_column()
