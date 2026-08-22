"""Authentication schemas (04_API_CONTRACT.md → POST /api/auth/login)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.enums import Role
from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    # Not EmailStr: rejecting a malformed email with a distinct 400 before the
    # credential check would tell an attacker which submissions were even
    # candidates, and it changes response timing. Anything non-empty is accepted
    # and fails the same way a wrong password does.
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("email")
    @classmethod
    def normalise_email(cls, v: str) -> str:
        return v.strip().lower()


class LoginResponse(BaseModel):
    """04_API_CONTRACT.md specifies exactly these three fields.

    Notably absent: anything about the password, the session token, or other
    users. The cookie is the credential; the body is only what the UI needs to
    render a role-appropriate view.
    """

    user_id: uuid.UUID
    role: Role
    name: str


class CurrentUserResponse(ORMModel):
    user_id: uuid.UUID
    email: str
    name: str
    role: Role


class UserSummary(ORMModel):
    """A user as seen by another user — no credential material of any kind."""

    id: uuid.UUID
    email: EmailStr
    name: str
    role: Role
    is_active: bool
