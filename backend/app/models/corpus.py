"""PCIRequirement — the versioned clause corpus (03_DATA_MODEL.md).

This is firm-wide reference data, not client data, so it carries no ownership
rules and no engagement scoping. A corpus update inserts rows under a new
`corpus_version` rather than mutating existing ones, so a past engagement always
cites the text that was actually in effect when it ran.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Date, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.config.settings import settings
from app.db.base import Base, created_at_column, uuid_pk


class PCIRequirement(Base):
    __tablename__ = "pci_requirements"
    __table_args__ = (
        UniqueConstraint("clause_id", "corpus_version", name="uq_clause_per_version"),
        Index("ix_pci_requirements_family", "requirement_family"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    clause_id: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    requirement_family: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    # Sensitivity: Public — the published standard. Under ADR-010 the shipped
    # corpus carries firm-authored summaries, not the Council's text, and
    # `corpus_version` records which.
    full_text: Mapped[str] = mapped_column(Text, nullable=False)
    corpus_version: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.EMBEDDING_DIMENSIONS), nullable=True
    )
    created_at: Mapped[datetime] = created_at_column()
