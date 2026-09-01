"""EvidenceDocument and EvidenceChunk data access.

This module also holds the worker's job-claim query. ADR-013: the
`evidence_documents` table is the queue, claimed with SELECT ... FOR UPDATE SKIP
LOCKED so multiple workers cannot pick up the same row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession
from sqlalchemy.orm import defer

from app.models.enums import ExtractionStatus
from app.models.evidence import EvidenceChunk, EvidenceDocument
from app.repositories.base import AuditScopedRepository

if TYPE_CHECKING:
    from app.api.deps import Actor


class EvidenceDocumentRepository(AuditScopedRepository):
    def create(
        self,
        *,
        audit_id: uuid.UUID,
        evidence_request_id: uuid.UUID | None,
        original_filename: str,
        content_hash: str,
        storage_path: str,
        mime_type: str,
        size_bytes: int,
        uploaded_by: uuid.UUID,
    ) -> EvidenceDocument:
        document = EvidenceDocument(
            audit_id=audit_id,
            evidence_request_id=evidence_request_id,
            original_filename=original_filename,
            content_hash=content_hash,
            storage_path=storage_path,
            mime_type=mime_type,
            size_bytes=size_bytes,
            uploaded_by=uploaded_by,
            extraction_status=ExtractionStatus.processing,
        )
        self._db.add(document)
        self._db.flush()
        return document

    def list_for_audit(self, audit_id: uuid.UUID, actor: Actor) -> list[EvidenceDocument]:
        """List view. `extracted_text` is deferred rather than selected and
        discarded: it is Sensitive and can be megabytes per row, so a list of
        sixty documents should not pull all of it out of the database."""
        stmt = self._scoped(
            select(EvidenceDocument)
            .where(EvidenceDocument.audit_id == audit_id)
            .options(defer(EvidenceDocument.extracted_text)),
            EvidenceDocument.audit_id,
            actor,
        )
        return list(self._db.scalars(stmt.order_by(EvidenceDocument.created_at.desc())).all())

    def get_scoped(self, document_id: uuid.UUID, actor: Actor) -> EvidenceDocument | None:
        stmt = self._scoped(
            select(EvidenceDocument).where(EvidenceDocument.id == document_id),
            EvidenceDocument.audit_id,
            actor,
        )
        return self._db.scalar(stmt)

    def exists_unscoped(self, document_id: uuid.UUID) -> bool:
        return (
            self._db.scalar(select(EvidenceDocument.id).where(EvidenceDocument.id == document_id))
            is not None
        )

    def find_duplicate(self, audit_id: uuid.UUID, content_hash: str) -> EvidenceDocument | None:
        """Whether this exact content is already on this audit.

        Content-addressed storage means a re-upload costs no extra disk, but
        creating a second EvidenceDocument row would double every Finding
        generated from it.
        """
        return self._db.scalar(
            select(EvidenceDocument).where(
                EvidenceDocument.audit_id == audit_id,
                EvidenceDocument.content_hash == content_hash,
            )
        )

    # --- Worker queue (ADR-013) ---------------------------------------------

    def claim_for_extraction(self, limit: int = 5) -> list[EvidenceDocument]:
        """Claim documents awaiting extraction.

        `SKIP LOCKED` is what makes this safe with more than one worker: a row
        another worker already holds is skipped rather than waited on.
        """
        stmt = (
            select(EvidenceDocument)
            .where(
                EvidenceDocument.extraction_status == ExtractionStatus.processing,
                EvidenceDocument.extraction_started_at.is_(None),
            )
            .order_by(EvidenceDocument.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        documents = list(self._db.scalars(stmt).all())
        now = datetime.now(UTC)
        for document in documents:
            document.extraction_started_at = now
        self._db.flush()
        return documents

    def claim_for_matching(self, limit: int = 5) -> list[EvidenceDocument]:
        """Claim extracted documents awaiting the embedding/matching stage.

        Separate from extraction because 02_ARCHITECTURE.md §7.6 requires that
        an embedding outage defer matching without failing the upload — so a
        document can sit here, complete and stored, until the model returns.
        """
        stmt = (
            select(EvidenceDocument)
            .where(
                EvidenceDocument.extraction_status == ExtractionStatus.complete,
                EvidenceDocument.matching_status.in_(("pending", "deferred")),
            )
            .order_by(EvidenceDocument.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(self._db.scalars(stmt).all())

    def sweep_stuck_extractions(self, timeout: timedelta) -> list[EvidenceDocument]:
        """Fail anything stuck in `processing` past the timeout.

        02_ARCHITECTURE.md §7.5 requires this explicitly: a row must never sit
        in `processing` indefinitely, because the auditor would be waiting on a
        result that is never coming with nothing on screen to say so.
        """
        cutoff = datetime.now(UTC) - timeout
        stmt = select(EvidenceDocument).where(
            EvidenceDocument.extraction_status == ExtractionStatus.processing,
            EvidenceDocument.extraction_started_at.is_not(None),
            EvidenceDocument.extraction_started_at < cutoff,
        )
        stuck = list(self._db.scalars(stmt).all())
        for document in stuck:
            document.extraction_status = ExtractionStatus.extraction_failed
            document.extraction_error = (
                "Extraction did not complete within the expected time and was marked failed. "
                "Please review this document manually."
            )
            document.extraction_completed_at = datetime.now(UTC)
        self._db.flush()
        return stuck


class EvidenceChunkRepository:
    """Chunks are only ever reached through their parent document, which is
    itself audit-scoped, so this repository takes no Actor. The retrieval
    query below is scoped by an explicit audit id supplied by the caller."""

    def __init__(self, db: DBSession) -> None:
        self._db = db

    def replace_for_document(
        self, document_id: uuid.UUID, chunks: list[tuple[int, str, str, list[float] | None]]
    ) -> list[EvidenceChunk]:
        """Write this document's chunks, replacing any earlier attempt.

        Extraction may be retried after a transient failure; without the delete
        a retry would leave duplicate chunks that retrieval would then match
        twice, producing duplicate findings.
        """
        existing = self._db.scalars(
            select(EvidenceChunk).where(EvidenceChunk.evidence_document_id == document_id)
        ).all()
        for chunk in existing:
            self._db.delete(chunk)
        self._db.flush()

        created = [
            EvidenceChunk(
                evidence_document_id=document_id,
                chunk_index=index,
                content=content,
                location=location,
                embedding=embedding,
            )
            for index, content, location, embedding in chunks
        ]
        self._db.add_all(created)
        self._db.flush()
        return created

    def list_for_document(self, document_id: uuid.UUID) -> list[EvidenceChunk]:
        return list(
            self._db.scalars(
                select(EvidenceChunk)
                .where(EvidenceChunk.evidence_document_id == document_id)
                .order_by(EvidenceChunk.chunk_index)
            ).all()
        )
