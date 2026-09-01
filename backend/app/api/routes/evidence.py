"""Evidence routes (04_API_CONTRACT.md → evidence-requests, evidence-documents)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile, status
from sqlalchemy.orm import Session as DBSession

from app.api.deps import CurrentActor
from app.db.session import get_db
from app.schemas.common import ErrorResponse
from app.schemas.evidence import EvidenceDocumentDetail, EvidenceDocumentSummary
from app.schemas.scoping import (
    EvidenceRequestGenerateResponse,
    EvidenceRequestResponse,
    EvidenceRequestUpdate,
)
from app.services.evidence import EvidenceService
from app.services.evidence_request import EvidenceRequestService

router = APIRouter(tags=["evidence"])


def get_request_service(db: Annotated[DBSession, Depends(get_db)]) -> EvidenceRequestService:
    return EvidenceRequestService(db)


def get_evidence_service(db: Annotated[DBSession, Depends(get_db)]) -> EvidenceService:
    return EvidenceService(db)


RequestService = Annotated[EvidenceRequestService, Depends(get_request_service)]
DocumentService = Annotated[EvidenceService, Depends(get_evidence_service)]


# --- Evidence requests -------------------------------------------------------


@router.post(
    "/api/audits/{audit_id}/evidence-requests/generate",
    response_model=EvidenceRequestGenerateResponse,
    responses={409: {"model": ErrorResponse, "description": "NO_CONFIRMED_SCOPE"}},
)
def generate_evidence_requests(
    audit_id: uuid.UUID,
    actor: CurrentActor,
    service: RequestService,
    db: Annotated[DBSession, Depends(get_db)],
) -> EvidenceRequestGenerateResponse:
    """Create the draft checklist.

    04_API_CONTRACT.md, Side Effects: "Creates EvidenceRequest rows,
    status=draft. Never sends anything externally." (ADR-004.)
    """
    result = service.generate(audit_id, actor)
    db.commit()
    clause_ids = service.clause_ids_for(result.created)
    return EvidenceRequestGenerateResponse(
        created=[
            EvidenceRequestResponse.of(r, clause_ids[r.scoped_control_id]) for r in result.created
        ],
        skipped_already_requested=result.skipped_already_requested,
        llm_available=result.llm_available,
    )


@router.get(
    "/api/audits/{audit_id}/evidence-requests",
    response_model=list[EvidenceRequestResponse],
)
def list_evidence_requests(
    audit_id: uuid.UUID, actor: CurrentActor, service: RequestService
) -> list[EvidenceRequestResponse]:
    requests = service.list_for_audit(audit_id, actor)
    clause_ids = service.clause_ids_for(requests)
    return [EvidenceRequestResponse.of(r, clause_ids[r.scoped_control_id]) for r in requests]


@router.patch("/api/evidence-requests/{request_id}", response_model=EvidenceRequestResponse)
def update_evidence_request(
    request_id: uuid.UUID,
    payload: EvidenceRequestUpdate,
    actor: CurrentActor,
    service: RequestService,
    db: Annotated[DBSession, Depends(get_db)],
) -> EvidenceRequestResponse:
    updated = service.update(
        request_id, actor, description=payload.description, status=payload.status
    )
    db.commit()
    return EvidenceRequestResponse.of(
        updated, service.clause_ids_for([updated])[updated.scoped_control_id]
    )


# --- Evidence documents ------------------------------------------------------


@router.post(
    "/api/audits/{audit_id}/evidence-documents",
    response_model=EvidenceDocumentSummary,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ErrorResponse, "description": "UNSUPPORTED_FILE_TYPE"},
        413: {"model": ErrorResponse, "description": "FILE_TOO_LARGE"},
    },
)
def upload_evidence_document(
    audit_id: uuid.UUID,
    actor: CurrentActor,
    service: DocumentService,
    db: Annotated[DBSession, Depends(get_db)],
    file: Annotated[UploadFile, File()],
    evidence_request_id: Annotated[uuid.UUID | None, Form()] = None,
) -> EvidenceDocumentSummary:
    """Returns immediately with `extraction_status: processing`.

    The extraction, embedding and matching pipeline runs in the worker
    (02_ARCHITECTURE.md §7.5). Nothing in this handler parses the file beyond
    reading its magic bytes to identify it.
    """
    document = service.upload(
        audit_id,
        actor,
        upload=file.file,
        filename=file.filename or "unnamed",
        evidence_request_id=evidence_request_id,
    )
    db.commit()
    return EvidenceDocumentSummary.model_validate(document)


@router.get(
    "/api/audits/{audit_id}/evidence-documents",
    response_model=list[EvidenceDocumentSummary],
)
def list_evidence_documents(
    audit_id: uuid.UUID, actor: CurrentActor, service: DocumentService
) -> list[EvidenceDocumentSummary]:
    return [
        EvidenceDocumentSummary.model_validate(d) for d in service.list_for_audit(audit_id, actor)
    ]


@router.get("/api/evidence-documents/{document_id}", response_model=EvidenceDocumentDetail)
def get_evidence_document(
    document_id: uuid.UUID, actor: CurrentActor, service: DocumentService
) -> EvidenceDocumentDetail:
    return EvidenceDocumentDetail.model_validate(service.get(document_id, actor))


@router.get("/api/evidence-documents/{document_id}/download")
def download_evidence_document(
    document_id: uuid.UUID, actor: CurrentActor, service: DocumentService
) -> Response:
    """Always an attachment, never inline.

    An uploaded file rendered inline would execute in the app's own origin —
    a stored-XSS vector from a source that is, by design, external and
    untrusted. `Content-Type` is the type detected at upload from the file's own
    bytes, not anything the client claimed.
    """
    content, document = service.read_file(document_id, actor)
    return Response(
        content=content,
        media_type=document.mime_type,
        headers={
            "Content-Disposition": f'attachment; filename="{document.original_filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
