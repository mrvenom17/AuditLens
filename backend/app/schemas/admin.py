"""Admin user-management schemas (ADR-012).

05_SECURITY.md §10.3: "role is set only by Admin action on the User record,
never accepted as a client-supplied field on any other endpoint." These are the
only schemas in the application that carry a `role` field, and they are reachable
only behind the admin role gate.
"""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.enums import Role

# 04_API_CONTRACT.md → Admin user management: password minimum 12 characters.
# This matches PCI DSS v4.0.1 requirement 8.3.6, which the firm using this tool
# is itself assessed against — a tool that held its own staff to a weaker
# standard than it audits clients for would be hard to defend.
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024


class AdminUserCreate(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=200)
    role: Role
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name must not be blank")
        return cleaned


class AdminUserUpdate(BaseModel):
    """04_API_CONTRACT.md: "PATCH supports `is_active` and `role` only."

    Notably absent: `password`. An Admin resetting another user's password
    through the API would let anyone who compromises an Admin session take over
    every account silently. Resets go through the documented seed-script
    procedure instead (05_SECURITY.md §10.2), which requires server access.
    """

    is_active: bool | None = None
    role: Role | None = None
