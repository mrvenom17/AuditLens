"""Admin account management (ADR-012).

00_PRODUCT.md §5.3 grants the Admin role "create/deactivate user accounts", and
01_REQUIREMENTS.md states accounts are "provisioned by an Admin only — no public
self-registration". This service is that capability.

Two invariants beyond the obvious role gate:

* **Users are deactivated, never deleted** (03_DATA_MODEL.md → User lifecycle),
  because their past actions must stay attributable.
* **Deactivation revokes live sessions.** A deactivated user whose cookie keeps
  working until it happens to expire has not really been deactivated — and the
  reason for deactivating is often that the account is suspected compromised.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session as DBSession

from app.auth.password import hash_password
from app.errors import CODE_EMAIL_ALREADY_EXISTS, ConflictError, NotFoundError, ValidationError
from app.models.enums import Role
from app.models.user import User
from app.repositories.user import SessionRepository, UserRepository
from app.schemas.admin import AdminUserCreate, AdminUserUpdate

if TYPE_CHECKING:
    from app.api.deps import Actor

logger = logging.getLogger(__name__)

_audit = logging.getLogger("auditlens.audit")


class AdminService:
    def __init__(self, db: DBSession) -> None:
        self._db = db
        self._users = UserRepository(db)
        self._sessions = SessionRepository(db)

    def list_users(self) -> list[User]:
        return self._users.list_users()

    def create_user(self, payload: AdminUserCreate, actor: Actor) -> User:
        if self._users.get_by_email(payload.email) is not None:
            # 409 rather than a silent no-op: an Admin who thinks they created
            # an account that already existed would hand out a password that
            # does not work, and never find out why.
            raise ConflictError(
                "An account already exists for that email address.",
                code=CODE_EMAIL_ALREADY_EXISTS,
            )

        user = self._users.create(
            email=payload.email,
            password_hash=hash_password(payload.password),
            name=payload.name,
            role=payload.role,
        )
        # Granting a role is the single most security-relevant action in this
        # system after finalization, so it is logged with both parties.
        _audit.warning(
            "admin.user_created actor=%s user=%s role=%s",
            actor.id,
            user.id,
            user.role.value,
        )
        return user

    def update_user(self, user_id: uuid.UUID, payload: AdminUserUpdate, actor: Actor) -> User:
        user = self._users.get_by_id(user_id)
        if user is None:
            raise NotFoundError("User not found.")

        if payload.is_active is None and payload.role is None:
            raise ValidationError("Provide at least one of is_active or role.")

        # An Admin removing their own access — by deactivating themselves or
        # demoting away from admin — is almost always a mistake, and recovering
        # from it requires shell access to the server to run the seed script.
        # Refusing it costs nothing; a second Admin can always perform the
        # change if it was genuinely intended.
        if user.id == actor.id:
            if payload.is_active is False:
                raise ConflictError(
                    "You cannot deactivate your own account.",
                    code="CANNOT_MODIFY_SELF",
                )
            if payload.role is not None and payload.role != Role.admin:
                raise ConflictError(
                    "You cannot remove your own admin role.",
                    code="CANNOT_MODIFY_SELF",
                )

        if payload.role is not None and payload.role != user.role:
            previous = user.role
            user.role = payload.role
            _audit.warning(
                "admin.role_changed actor=%s user=%s %s->%s",
                actor.id,
                user.id,
                previous.value,
                payload.role.value,
            )

        if payload.is_active is not None and payload.is_active != user.is_active:
            user.is_active = payload.is_active
            _audit.warning(
                "admin.user_%s actor=%s user=%s",
                "activated" if payload.is_active else "deactivated",
                actor.id,
                user.id,
            )
            if not payload.is_active:
                revoked = self._sessions.revoke_all_for_user(user.id)
                logger.info("Revoked %d session(s) for deactivated user %s", revoked, user.id)

        self._db.flush()
        return user
