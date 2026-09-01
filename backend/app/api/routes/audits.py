"""Audit routes (04_API_CONTRACT.md → /api/audits).

Thin: authenticate via the dependency, authorize via the role gate and the
service's scoped lookups, call the service, shape the response. No query is
constructed here and no business rule is decided here (02_ARCHITECTURE.md §7.4).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session as DBSession

from app.api.deps import CurrentActor, RequireAuditorOrReviewer
from app.db.session import get_db
from app.models.audit import Audit
from app.models.enums import AuditStatus
from app.repositories.audit import AuditRepository
from app.schemas.audit import (
    AssignmentCreate,
    AssignmentResponse,
    AuditCounts,
    AuditCreate,
    AuditDetail,
    AuditProfileUpdate,
    AuditSummary,
)
from app.schemas.common import ErrorResponse, Page
from app.services.audit import AuditService

router = APIRouter(prefix="/api/audits", tags=["audits"])


def get_service(db: Annotated[DBSession, Depends(get_db)]) -> AuditService:
    return AuditService(db)


Service = Annotated[AuditService, Depends(get_service)]


def _detail(audit: Audit, service: AuditService, db: DBSession) -> AuditDetail:
    return AuditDetail(
        **{
            field: getattr(audit, field)
            for field in AuditDetail.model_fields
            if field not in ("counts", "assigned_user_ids")
        },
        counts=AuditCounts(**AuditRepository(db).counts(audit.id)),
        assigned_user_ids=service.assigned_user_ids(audit.id),
    )


@router.post(
    "",
    response_model=AuditDetail,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": ErrorResponse, "description": "VALIDATION_ERROR"}},
)
def create_audit(
    payload: AuditCreate,
    actor: RequireAuditorOrReviewer,
    service: Service,
    db: Annotated[DBSession, Depends(get_db)],
) -> AuditDetail:
    """04_API_CONTRACT.md: role auditor or reviewer. Admin is excluded — an
    Admin manages accounts and the corpus, not audits (00_PRODUCT.md §5.3).
    """
    audit = service.create(payload, actor)
    db.commit()
    db.refresh(audit)
    return _detail(audit, service, db)


@router.get("", response_model=Page[AuditSummary])
def list_audits(
    actor: CurrentActor,
    service: Service,
    status_filter: Annotated[AuditStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[AuditSummary]:
    items, total = service.list_visible(actor, status=status_filter, limit=limit, offset=offset)
    return Page(items=[AuditSummary.model_validate(e) for e in items], total=total)


@router.get(
    "/{audit_id}",
    response_model=AuditDetail,
    responses={
        403: {"model": ErrorResponse, "description": "FORBIDDEN — exists, no access"},
        404: {"model": ErrorResponse, "description": "NOT_FOUND — no such audit"},
    },
)
def get_audit(
    audit_id: uuid.UUID,
    actor: CurrentActor,
    service: Service,
    db: Annotated[DBSession, Depends(get_db)],
) -> AuditDetail:
    audit = service.get(audit_id, actor)
    db.commit()  # persists the Admin-access audit log's session touch
    return _detail(audit, service, db)


@router.patch(
    "/{audit_id}",
    response_model=AuditDetail,
    responses={
        403: {"model": ErrorResponse, "description": "FORBIDDEN — exists, no access"},
        404: {"model": ErrorResponse, "description": "NOT_FOUND — no such audit"},
        409: {"model": ErrorResponse, "description": "AUDIT_FINALIZED"},
    },
)
def update_audit_profile(
    audit_id: uuid.UUID,
    payload: AuditProfileUpdate,
    actor: CurrentActor,
    service: Service,
    db: Annotated[DBSession, Depends(get_db)],
) -> AuditDetail:
    """Correct the company profile.

    Only the profile is editable. `client_name`, `entity_type` and the rest are
    not accepted here — changing what company an audit is *about* halfway through
    is not an edit, it is a different audit, and the evidence already gathered
    would no longer belong to it.

    Re-running scope suggestion afterwards is a deliberate second step: the
    auditor decides when a corrected profile should re-open scope, because doing
    it automatically could withdraw controls they had already confirmed.
    """
    audit = service.update_profile(audit_id, payload, actor)
    db.commit()
    return _detail(audit, service, db)


@router.post(
    "/{audit_id}/assignments",
    response_model=AssignmentResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        403: {"model": ErrorResponse, "description": "FORBIDDEN — reviewer/admin only"},
        409: {"model": ErrorResponse, "description": "ALREADY_ASSIGNED"},
    },
)
def create_assignment(
    audit_id: uuid.UUID,
    payload: AssignmentCreate,
    actor: CurrentActor,
    service: Service,
    db: Annotated[DBSession, Depends(get_db)],
) -> AssignmentResponse:
    """Reviewer or Admin only (ADR-012).

    The role check lives in the service rather than in a route gate because this
    grants access to another organisation's audit data — it must hold for any
    caller, not only for callers that arrived through this route.
    """
    assignment = service.assign_user(audit_id, payload.user_id, actor)
    db.commit()
    return AssignmentResponse.model_validate(assignment)


@router.delete("/{audit_id}/assignments/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_assignment(
    audit_id: uuid.UUID,
    user_id: uuid.UUID,
    actor: CurrentActor,
    service: Service,
    db: Annotated[DBSession, Depends(get_db)],
) -> None:
    service.unassign_user(audit_id, user_id, actor)
    db.commit()
