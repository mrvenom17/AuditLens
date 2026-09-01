"""EvidenceDocument and its extracted-chunk index (03_DATA_MODEL.md)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config.settings import settings
from app.db.base import Base, created_at_column, uuid_pk
from app.models.enums import ExtractionStatus, MalwareScanStatus


class EvidenceDocument(Base):
    """An uploaded file and its extracted content.

    Immutable once created: no update or delete path exists anywhere in the
    application, because this is the evidentiary record (01_REQUIREMENTS.md
    § Evidence Document Ingestion, Business Rules).

    This table doubles as the background job queue (ADR-013) — the worker claims
    rows in `processing` with SELECT ... FOR UPDATE SKIP LOCKED.
    """

    __tablename__ = "evidence_documents"
    __table_args__ = (
        # The worker's claim query and the stuck-row sweep both filter on status.
        Index("ix_evidence_documents_status_created", "extraction_status", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    audit_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("audits.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    evidence_request_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("evidence_requests.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Sensitivity: Sensitive (03_DATA_MODEL.md §8.4). Never returned in an API
    # response — files are addressed by document id and resolved server-side.
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    # 00_PRODUCT.md §5.8 decision for Level 0: recorded, not upload-gating.
    # `not_scanned` is the honest default until a scanner is wired in — the
    # field exists so the answer is visible rather than assumed.
    malware_scan_status: Mapped[MalwareScanStatus] = mapped_column(
        Enum(MalwareScanStatus, name="malware_scan_status"),
        nullable=False,
        default=MalwareScanStatus.not_scanned,
        server_default="not_scanned",
    )

    extraction_status: Mapped[ExtractionStatus] = mapped_column(
        Enum(ExtractionStatus, name="extraction_status"),
        nullable=False,
        default=ExtractionStatus.processing,
    )
    # Sensitivity: Sensitive — client evidence content. Never logged, and
    # excluded from list responses.
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Why extraction failed, in terms safe to show an auditor (e.g. "password
    # protected"). Never carries a raw parser stack trace.
    extraction_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    extraction_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    extraction_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Second pipeline stage, tracked separately: 02_ARCHITECTURE.md §7.6 requires
    # that if embedding is down, extraction still completes and is stored, with
    # matching deferred rather than failing the whole upload.
    matching_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    matching_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    matching_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = created_at_column()

    chunks: Mapped[list[EvidenceChunk]] = relationship(back_populates="document")


class EvidenceChunk(Base):
    """A chunk of extracted text with its embedding.

    Not named in 03_DATA_MODEL.md, but implied by it: the data model gives
    ControlDefinition an `embedding` for retrieval, and 01_REQUIREMENTS.md
    § Evidence-to-Clause Matching rule 1 requires the evidence side to be
    "chunked and embedded" too. Storing chunks rather than one vector per
    document is what makes the citation `location` meaningful — a finding can
    point at the page a match actually came from.
    """

    __tablename__ = "evidence_chunks"
    __table_args__ = (Index("ix_evidence_chunks_document", "evidence_document_id", "chunk_index"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    evidence_document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("evidence_documents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # Sensitivity: Sensitive — a slice of client evidence.
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Human-readable pointer used verbatim in Finding citations, e.g. "page 3".
    location: Mapped[str] = mapped_column(String(120), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.EMBEDDING_DIMENSIONS), nullable=True
    )
    created_at: Mapped[datetime] = created_at_column()

    document: Mapped[EvidenceDocument] = relationship(back_populates="chunks")
