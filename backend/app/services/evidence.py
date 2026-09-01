"""Evidence document ingestion (TASK-016).

01_REQUIREMENTS.md § Evidence Document Ingestion. The upload path is
deliberately short: validate, store, record, enqueue, return. Extraction runs in
the worker so a 60-document batch never blocks a request
(02_ARCHITECTURE.md §7.9).

Immutability is enforced by omission — this service has no update or delete
method, because the uploaded file is the evidentiary record and 00_PRODUCT.md
§5.3 states even a Reviewer cannot delete evidence.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, BinaryIO

from sqlalchemy.orm import Session as DBSession

from app.errors import ConflictError, ForbiddenError, NotFoundError
from app.models.enums import EvidenceRequestStatus
from app.models.evidence import EvidenceDocument
from app.repositories.evidence import EvidenceDocumentRepository
from app.repositories.scoping import EvidenceRequestRepository
from app.services import file_storage
from app.services.audit import AuditService

if TYPE_CHECKING:
    from app.api.deps import Actor

logger = logging.getLogger(__name__)


class EvidenceService:
    def __init__(self, db: DBSession) -> None:
        self._db = db
        self._documents = EvidenceDocumentRepository(db)
        self._requests = EvidenceRequestRepository(db)
        self._audits = AuditService(db)

    def upload(
        self,
        audit_id: uuid.UUID,
        actor: Actor,
        *,
        upload: BinaryIO,
        filename: str,
        evidence_request_id: uuid.UUID | None = None,
    ) -> EvidenceDocument:
        audit = self._audits.get(audit_id, actor)
        self._audits.ensure_not_finalized(audit)

        # Validation happens before anything is written to disk, so a rejected
        # upload leaves no trace on the filesystem.
        content, mime_type, safe_filename = file_storage.read_and_validate(upload, filename)

        if evidence_request_id is not None:
            request = self._requests.get_scoped(evidence_request_id, actor)
            if request is None or request.audit_id != audit_id:
                # Checked against *this* audit specifically: a valid request
                # id belonging to a different audit must not link across.
                raise NotFoundError("Evidence request not found on this audit.")

        content_hash, storage_path = file_storage.store(content)

        duplicate = self._documents.find_duplicate(audit_id, content_hash)
        if duplicate is not None:
            raise ConflictError(
                "This exact file has already been uploaded to this audit.",
                code="DUPLICATE_EVIDENCE",
                existing_document_id=str(duplicate.id),
            )

        document = self._documents.create(
            audit_id=audit_id,
            evidence_request_id=evidence_request_id,
            original_filename=safe_filename,
            content_hash=content_hash,
            storage_path=storage_path,
            mime_type=mime_type,
            size_bytes=len(content),
            uploaded_by=actor.id,
        )

        if evidence_request_id is not None:
            request = self._requests.get_scoped(evidence_request_id, actor)
            if request is not None:
                request.status = EvidenceRequestStatus.received
                self._db.flush()

        logger.info(
            "evidence.uploaded document=%s audit=%s bytes=%d type=%s",
            document.id,
            audit_id,
            len(content),
            mime_type,
        )
        return document

    def list_for_audit(self, audit_id: uuid.UUID, actor: Actor) -> list[EvidenceDocument]:
        self._audits.get(audit_id, actor)
        return self._documents.list_for_audit(audit_id, actor)

    def get(self, document_id: uuid.UUID, actor: Actor) -> EvidenceDocument:
        document = self._documents.get_scoped(document_id, actor)
        if document is not None:
            return document
        if self._documents.exists_unscoped(document_id):
            raise ForbiddenError("You are not assigned to this audit.")
        raise NotFoundError("Evidence document not found.")

    def read_file(self, document_id: uuid.UUID, actor: Actor) -> tuple[bytes, EvidenceDocument]:
        """Return the original bytes for download.

        Authorization is resolved before the path is touched, and the path comes
        from the row rather than from the request — the client addresses the
        file by document id and never learns `storage_path` at all.
        """
        document = self.get(document_id, actor)
        return file_storage.read_stored(document.storage_path), document
