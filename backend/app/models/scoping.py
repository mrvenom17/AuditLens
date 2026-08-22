"""ScopedRequirement and EvidenceRequest (03_DATA_MODEL.md)."""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, uuid_pk
from app.models.enums import EvidenceRequestStatus, ScopeSource


class ScopedRequirement(Base, TimestampMixin):
    """Which corpus clauses apply to one engagement.

    `confirmed = true` is the human gate that unlocks evidence-request
    generation. Nothing in the system may set it without an explicit human
    action (01_REQUIREMENTS.md § PCI DSS Scope Matching, Explicitly Forbidden
    Behavior).
    """

    __tablename__ = "scoped_requirements"
    __table_args__ = (
        UniqueConstraint("engagement_id", "pci_requirement_id", name="uq_scoped_requirement"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    engagement_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("engagements.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    pci_requirement_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("pci_requirements.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source: Mapped[ScopeSource] = mapped_column(
        Enum(ScopeSource, name="scope_source"), nullable=False
    )
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ADR-011 item 3. Set only by a Reviewer, and only with a stated reason:
    # this is what permits finalizing a confirmed requirement that has no
    # approved Finding (01_REQUIREMENTS.md § Engagement Finalization).
    gap_acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    gap_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class EvidenceRequest(Base, TimestampMixin):
    """A drafted "please provide X" checklist item.

    Never dispatched by the system. `sent_externally` is the auditor's own
    note-to-self after sending through their channel (ADR-004).
    """

    __tablename__ = "evidence_requests"

    id: Mapped[uuid.UUID] = uuid_pk()
    engagement_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("engagements.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    scoped_requirement_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("scoped_requirements.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[EvidenceRequestStatus] = mapped_column(
        Enum(EvidenceRequestStatus, name="evidence_request_status"),
        nullable=False,
        default=EvidenceRequestStatus.draft,
    )
    # Records whether the plain-language description came from the LLM or from
    # the template fallback, so a degraded run is visible rather than silent
    # (01_REQUIREMENTS.md § Evidence Request Generation, External Dependencies).
    description_source: Mapped[str] = mapped_column(String(20), nullable=False, default="template")
