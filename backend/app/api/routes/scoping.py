"""Scoping routes (04_API_CONTRACT.md → scope-suggestion, scoped-requirements)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session as DBSession

from app.api.deps import CurrentActor
from app.db.session import get_db
from app.schemas.common import ErrorResponse
from app.schemas.scoping import (
    ManualScopeCreate,
    ScopedRequirementGapUpdate,
    ScopedRequirementResponse,
    ScopedRequirementUpdate,
    ScopeSuggestionResponse,
)
from app.services.scoping import ScopingService

router = APIRouter(tags=["scoping"])


def get_service(db: Annotated[DBSession, Depends(get_db)]) -> ScopingService:
    return ScopingService(db)


Service = Annotated[ScopingService, Depends(get_service)]


@router.post(
    "/api/audits/{audit_id}/scope-suggestion",
    response_model=ScopeSuggestionResponse,
    responses={
        403: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "MISSING_PROFILE_FIELDS | RATE_LIMITED"},
    },
)
def suggest_scope(
    audit_id: uuid.UUID,
    actor: CurrentActor,
    service: Service,
    db: Annotated[DBSession, Depends(get_db)],
) -> ScopeSuggestionResponse:
    """Always 200 when the LLM is merely unavailable.

    04_API_CONTRACT.md is explicit: on LLM failure or timeout this returns an
    empty proposal with `manual_scoping_required: true`, never a 500. The
    service returns that state rather than raising, so there is no exception
    path here that could turn a degraded call into an error.
    """
    result = service.suggest_scope(audit_id, actor)
    db.commit()
    return ScopeSuggestionResponse(
        proposed_requirements=[ScopedRequirementResponse.of(s) for s in result.proposed],
        manual_scoping_required=result.manual_scoping_required,
        saq_type=result.saq_type,
        ambiguous_entity_type=result.ambiguous_entity_type,
    )


@router.get(
    "/api/audits/{audit_id}/scoped-requirements",
    response_model=list[ScopedRequirementResponse],
)
def list_scoped_requirements(
    audit_id: uuid.UUID,
    actor: CurrentActor,
    service: Service,
    confirmed_only: bool = False,
) -> list[ScopedRequirementResponse]:
    rows = service.list_scope(audit_id, actor, confirmed_only=confirmed_only)
    return [ScopedRequirementResponse.of(r) for r in rows]


@router.post(
    "/api/audits/{audit_id}/scoped-requirements",
    response_model=ScopedRequirementResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_manual_scoped_requirement(
    audit_id: uuid.UUID,
    payload: ManualScopeCreate,
    actor: CurrentActor,
    service: Service,
    db: Annotated[DBSession, Depends(get_db)],
) -> ScopedRequirementResponse:
    """Add a clause the AI did not propose. Created unconfirmed — adding a
    requirement to scope is not the same act as confirming it."""
    scoped = service.add_manual(audit_id, payload.control_id, actor, payload.rationale)
    db.commit()
    return ScopedRequirementResponse.of(scoped)


@router.patch(
    "/api/scoped-requirements/{scoped_id}",
    response_model=ScopedRequirementResponse,
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def update_scoped_requirement(
    scoped_id: uuid.UUID,
    payload: ScopedRequirementUpdate,
    actor: CurrentActor,
    service: Service,
    db: Annotated[DBSession, Depends(get_db)],
) -> ScopedRequirementResponse:
    """The human confirmation gate (04_API_CONTRACT.md → PATCH).

    One row per call by design. A bulk UI action is fine, but it must issue one
    request per row so each confirmation is individually addressable in the
    audit trail.
    """
    scoped = service.confirm(scoped_id, actor, confirmed=payload.confirmed)
    db.commit()
    return ScopedRequirementResponse.of(scoped)


@router.patch(
    "/api/scoped-requirements/{scoped_id}/gap",
    response_model=ScopedRequirementResponse,
    responses={403: {"model": ErrorResponse, "description": "FORBIDDEN — reviewer only"}},
)
def acknowledge_gap(
    scoped_id: uuid.UUID,
    payload: ScopedRequirementGapUpdate,
    actor: CurrentActor,
    service: Service,
    db: Annotated[DBSession, Depends(get_db)],
) -> ScopedRequirementResponse:
    """Reviewer only (ADR-012) — this flag is what allows finalizing without an
    approved Finding, so it carries finalization-level authority."""
    scoped = service.acknowledge_gap(
        scoped_id, actor, acknowledged=payload.gap_acknowledged, note=payload.gap_note
    )
    db.commit()
    return ScopedRequirementResponse.of(scoped)
