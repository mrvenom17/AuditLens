"""Control corpus, fact and evaluation routes (04_API_CONTRACT.md).

The security-relevant property of this module is what it does not expose. There
is no endpoint, and no request schema referenced by any endpoint here, that can
set `ControlEvaluation.result`. `POST /api/audits/{id}/evaluate` takes no body at
all: its entire contract is mechanical inputs already in the database, mechanical
output. It accepts no confidence threshold, no "let the model decide ties", no
LLM parameter of any kind (05_SECURITY.md §10.3).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session as DBSession

from app.api.deps import CurrentActor, RequireAdmin
from app.db.session import get_db
from app.models.enums import EvaluationMode, VerificationStatus
from app.repositories.evaluation import ControlEvaluationRepository, EvidenceFactRepository
from app.schemas.common import ErrorResponse
from app.schemas.evaluation import (
    ControlDefinitionCreate,
    ControlDefinitionResponse,
    ControlEvaluationResponse,
    EvaluateResponse,
    EvidenceFactResponse,
)
from app.services.audit import AuditService
from app.services.control_corpus import ControlCorpusService
from app.services.evaluation import EvaluationService

router = APIRouter(tags=["evaluation"])


def get_corpus_service(db: Annotated[DBSession, Depends(get_db)]) -> ControlCorpusService:
    return ControlCorpusService(db)


def get_evaluation_service(db: Annotated[DBSession, Depends(get_db)]) -> EvaluationService:
    return EvaluationService(db)


Corpus = Annotated[ControlCorpusService, Depends(get_corpus_service)]
Evaluations = Annotated[EvaluationService, Depends(get_evaluation_service)]


# --- Control corpus ----------------------------------------------------------


@router.get("/api/control-definitions", response_model=list[ControlDefinitionResponse])
def list_control_definitions(
    actor: CurrentActor,
    service: Corpus,
    evaluation_mode: Annotated[EvaluationMode | None, Query()] = None,
    requirement_family: Annotated[int | None, Query(ge=1, le=12)] = None,
    corpus_version: Annotated[str | None, Query(max_length=40)] = None,
) -> list[ControlDefinitionResponse]:
    """The machine-readable corpus, including `facts` and `rules`.

    Readable by any authenticated user: these are the published standard's
    mechanics, not client data (04_API_CONTRACT.md).
    """
    controls = service.list_controls(
        evaluation_mode=evaluation_mode,
        requirement_family=requirement_family,
        corpus_version=corpus_version,
    )
    return [ControlDefinitionResponse.model_validate(c) for c in controls]


@router.post(
    "/api/control-definitions",
    response_model=ControlDefinitionResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {
            "model": ErrorResponse,
            "description": "VALIDATION_ERROR | DETERMINISTIC_CONTROL_MISSING_RULES",
        },
        403: {"model": ErrorResponse, "description": "FORBIDDEN — Admin only"},
    },
)
def create_control_definition(
    actor: RequireAdmin,
    payload: ControlDefinitionCreate,
    service: Corpus,
    db: Annotated[DBSession, Depends(get_db)],
) -> ControlDefinitionResponse:
    """Author a control definition. Admin only.

    This is the one place in the whole API where a human directly authors what
    the rule engine will later treat as ground truth. There is deliberately no
    AI-assisted "suggest rules" variant of this endpoint — 01_REQUIREMENTS.md
    forbids an LLM populating `rules` under any circumstance.

    The gate is `RequireAdmin`, a route dependency, so a non-Admin is refused
    before the body is even parsed. A role check inside the service alone would
    let a malformed payload from a non-Admin return 400 — telling them their
    JSON was wrong rather than that they had no business here. The service keeps
    its own check as well, for callers that are not HTTP requests.
    """
    control = service.create(payload, actor)
    db.commit()
    return ControlDefinitionResponse.model_validate(control)


# --- Facts -------------------------------------------------------------------


@router.get("/api/audits/{audit_id}/facts", response_model=list[EvidenceFactResponse])
def list_facts(
    audit_id: uuid.UUID,
    actor: CurrentActor,
    db: Annotated[DBSession, Depends(get_db)],
    control_definition_id: Annotated[uuid.UUID | None, Query()] = None,
    verification_status: Annotated[VerificationStatus | None, Query()] = None,
) -> list[EvidenceFactResponse]:
    """Extracted facts with full provenance.

    `source_hash` is returned so a caller can compare it against the document's
    current hash — evidence tampering is a checkable condition from outside the
    system, not just an internal one (04_API_CONTRACT.md, Security Notes).
    """
    AuditService(db).get(audit_id, actor)
    facts = EvidenceFactRepository(db).list_for_audit(
        audit_id,
        actor,
        control_definition_id=control_definition_id,
        verification_status=verification_status,
    )
    return [EvidenceFactResponse.model_validate(f) for f in facts]


# --- Evaluation --------------------------------------------------------------


@router.post("/api/audits/{audit_id}/evaluate", response_model=EvaluateResponse)
def evaluate_audit(
    audit_id: uuid.UUID,
    actor: CurrentActor,
    service: Evaluations,
    db: Annotated[DBSession, Depends(get_db)],
) -> EvaluateResponse:
    """Run (or re-run) the rule engine and Evidence Gate for this audit.

    Takes no request body. A control with no facts yet simply evaluates to
    INSUFFICIENT_EVIDENCE, which is a valid result rather than an error
    (04_API_CONTRACT.md, Error Responses: "None specific").

    Re-running never edits an existing evaluation — it appends new rows, so the
    history of what the engine said at each point in time survives intact.
    """
    summaries = service.evaluate_audit(audit_id, actor)
    db.commit()
    return EvaluateResponse(
        evaluations=[ControlEvaluationResponse.model_validate(s.evaluation) for s in summaries]
    )


@router.get("/api/audits/{audit_id}/evaluations", response_model=list[ControlEvaluationResponse])
def list_evaluations(
    audit_id: uuid.UUID,
    actor: CurrentActor,
    db: Annotated[DBSession, Depends(get_db)],
) -> list[ControlEvaluationResponse]:
    """Every evaluation ever produced for this audit, oldest first.

    The full history rather than only the latest per control, because "what did
    the engine conclude, and when" is exactly the question an audit trail exists
    to answer.
    """
    AuditService(db).get(audit_id, actor)
    evaluations = ControlEvaluationRepository(db).list_for_audit(audit_id, actor)
    return [ControlEvaluationResponse.model_validate(e) for e in evaluations]
