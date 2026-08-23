"""Client profile document upload (ADR-011 item 6, ADR-012).

These are the firm's own client-file documents, uploaded so they can be
referenced by `source_document_ids` at engagement creation. Distinct from
evidence: they are not attached to an engagement, and they never enter the
extraction/matching pipeline.

Validation is identical to evidence upload and reuses the same module — the
threat is the same (an untrusted file from outside the firm's systems), so it
gets the same magic-byte inspection, size cap, and content-addressed storage
rather than a second, weaker path (05_SECURITY.md §10.4).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy.orm import Session as DBSession

from app.api.deps import CurrentActor
from app.db.session import get_db
from app.repositories.engagement import ClientProfileDocumentRepository
from app.schemas.common import ErrorResponse
from app.schemas.engagement import ClientProfileDocumentResponse
from app.services import file_storage

router = APIRouter(prefix="/api/client-profile-documents", tags=["client-documents"])


@router.post(
    "",
    response_model=ClientProfileDocumentResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ErrorResponse, "description": "UNSUPPORTED_FILE_TYPE"},
        413: {"model": ErrorResponse, "description": "FILE_TOO_LARGE"},
    },
)
def upload_client_profile_document(
    actor: CurrentActor,
    db: Annotated[DBSession, Depends(get_db)],
    file: Annotated[UploadFile, File()],
) -> ClientProfileDocumentResponse:
    """Upload a firm-held client-file document.

    Available to any authenticated staff member: these are the firm's own
    records in a single-tenant deployment, not engagement-owned data, so there
    is no assignment to check against (03_DATA_MODEL.md → ClientProfileDocument).

    Stored under a separate `profile` subdirectory rather than alongside
    evidence, so the evidentiary record stays a clean, separately-backed-up set.
    """
    content, mime_type, safe_filename = file_storage.read_and_validate(
        file.file, file.filename or "unnamed"
    )
    content_hash, storage_path = file_storage.store(content, subdirectory="profile")

    document = ClientProfileDocumentRepository(db).create(
        original_filename=safe_filename,
        content_hash=content_hash,
        storage_path=storage_path,
        mime_type=mime_type,
        uploaded_by=actor.id,
    )
    db.commit()
    # `storage_path` is Sensitive and is absent from the response schema.
    return ClientProfileDocumentResponse.model_validate(document)
