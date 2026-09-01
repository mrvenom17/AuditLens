"""Engagement routes (04_API_CONTRACT.md → /api/engagements).

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
from app.models.engagement import Engagement
from app.models.enums import EngagementStatus
from app.repositories.engagement import EngagementRepository
from app.schemas.common import ErrorResponse, Page
from app.schemas.engagement import (
    AssignmentCreate,
    AssignmentResponse,
    EngagementCounts,
    EngagementCreate,
    EngagementDetail,
    EngagementSummary,
)
from app.services.engagement import EngagementService

router = APIRouter(prefix="/api/engagements", tags=["engagements"])


def get_service(db: Annotated[DBSession, Depends(get_db)]) -> EngagementService:
    return EngagementService(db)


Service = Annotated[EngagementService, Depends(get_service)]


def _detail(engagement: Engagement, service: EngagementService, db: DBSession) -> EngagementDetail:
    return EngagementDetail(
        **{
            field: getattr(engagement, field)
            for field in EngagementDetail.model_fields
            if field not in ("counts", "assigned_user_ids")
        },
        counts=EngagementCounts(**EngagementRepository(db).counts(engagement.id)),
        assigned_user_ids=service.assigned_user_ids(engagement.id),
    )


@router.post(
    "",
    response_model=EngagementDetail,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": ErrorResponse, "description": "VALIDATION_ERROR"}},
)
def create_engagement(
    payload: EngagementCreate,
    actor: RequireAuditorOrReviewer,
    service: Service,
    db: Annotated[DBSession, Depends(get_db)],
) -> EngagementDetail:
    """04_API_CONTRACT.md: role auditor or reviewer. Admin is excluded — an
    Admin manages accounts and the corpus, not engagements (00_PRODUCT.md §5.3).
    """
    engagement = service.create(payload, actor)
    db.commit()
    db.refresh(engagement)
    return _detail(engagement, service, db)


@router.get("", response_model=Page[EngagementSummary])
def list_engagements(
    actor: CurrentActor,
    service: Service,
    status_filter: Annotated[EngagementStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[EngagementSummary]:
    items, total = service.list_visible(actor, status=status_filter, limit=limit, offset=offset)
    return Page(items=[EngagementSummary.model_validate(e) for e in items], total=total)


@router.get(
    "/{engagement_id}",
    response_model=EngagementDetail,
    responses={
        403: {"model": ErrorResponse, "description": "FORBIDDEN — exists, no access"},
        404: {"model": ErrorResponse, "description": "NOT_FOUND — no such engagement"},
    },
)
def get_engagement(
    engagement_id: uuid.UUID,
    actor: CurrentActor,
    service: Service,
    db: Annotated[DBSession, Depends(get_db)],
) -> EngagementDetail:
    engagement = service.get(engagement_id, actor)
    db.commit()  # persists the Admin-access audit log's session touch
    return _detail(engagement, service, db)


@router.post(
    "/{engagement_id}/assignments",
    response_model=AssignmentResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        403: {"model": ErrorResponse, "description": "FORBIDDEN — reviewer/admin only"},
        409: {"model": ErrorResponse, "description": "ALREADY_ASSIGNED"},
    },
)
def create_assignment(
    engagement_id: uuid.UUID,
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
    assignment = service.assign_user(engagement_id, payload.user_id, actor)
    db.commit()
    return AssignmentResponse.model_validate(assignment)


@router.delete("/{engagement_id}/assignments/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_assignment(
    engagement_id: uuid.UUID,
    user_id: uuid.UUID,
    actor: CurrentActor,
    service: Service,
    db: Annotated[DBSession, Depends(get_db)],
) -> None:
    service.unassign_user(engagement_id, user_id, actor)
    db.commit()
