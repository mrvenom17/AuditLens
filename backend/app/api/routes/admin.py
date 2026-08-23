"""Admin routes (04_API_CONTRACT.md → Admin user management, ADR-012).

Every route here sits behind `RequireAdmin`. This is the only place in the
application where `role` is accepted as input at all (05_SECURITY.md §10.3).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session as DBSession

from app.api.deps import RequireAdmin
from app.db.session import get_db
from app.schemas.admin import AdminUserCreate, AdminUserUpdate
from app.schemas.auth import UserSummary
from app.schemas.common import ErrorResponse
from app.services.admin import AdminService

router = APIRouter(prefix="/api/admin", tags=["admin"])


def get_service(db: Annotated[DBSession, Depends(get_db)]) -> AdminService:
    return AdminService(db)


Service = Annotated[AdminService, Depends(get_service)]


@router.get(
    "/users",
    response_model=list[UserSummary],
    responses={403: {"model": ErrorResponse, "description": "FORBIDDEN — admin only"}},
)
def list_users(actor: RequireAdmin, service: Service) -> list[UserSummary]:
    """Includes deactivated accounts: an Admin managing access needs to see who
    has been disabled, not only who is currently active."""
    return [UserSummary.model_validate(u) for u in service.list_users()]


@router.post(
    "/users",
    response_model=UserSummary,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ErrorResponse, "description": "VALIDATION_ERROR"},
        403: {"model": ErrorResponse, "description": "FORBIDDEN — admin only"},
        409: {"model": ErrorResponse, "description": "EMAIL_ALREADY_EXISTS"},
    },
)
def create_user(
    payload: AdminUserCreate,
    actor: RequireAdmin,
    service: Service,
    db: Annotated[DBSession, Depends(get_db)],
) -> UserSummary:
    """The only API path by which an account, and therefore a role, comes into
    existence. There is no self-registration anywhere (01_REQUIREMENTS.md)."""
    user = service.create_user(payload, actor)
    db.commit()
    # The response carries no credential material — not the password that was
    # just set, and not its hash.
    return UserSummary.model_validate(user)


@router.patch(
    "/users/{user_id}",
    response_model=UserSummary,
    responses={
        403: {"model": ErrorResponse, "description": "FORBIDDEN — admin only"},
        404: {"model": ErrorResponse, "description": "NOT_FOUND"},
        409: {"model": ErrorResponse, "description": "CANNOT_MODIFY_SELF"},
    },
)
def update_user(
    user_id: uuid.UUID,
    payload: AdminUserUpdate,
    actor: RequireAdmin,
    service: Service,
    db: Annotated[DBSession, Depends(get_db)],
) -> UserSummary:
    """Change a user's role or active state.

    Deactivation revokes the user's live sessions in the same transaction, so
    access ends immediately rather than at the next idle timeout.
    """
    user = service.update_user(user_id, payload, actor)
    db.commit()
    return UserSummary.model_validate(user)
