"""Finding, FindingHistory and Report — the highest-stakes entities.

The invariant ADR-003 makes structural lives in the service layer
(`app/services/finding.py`), because a database CHECK constraint cannot express
"reviewed_by must be the authenticated actor". What this module contributes is
that `reviewed_by` is nullable and `status` defaults to draft, so the *only* way
a row reaches `approved` is through code that deliberately sets both.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, created_at_column, uuid_pk
from app.models.enums import ComplianceStatus, FindingAction, FindingStatus


class Finding(Base, TimestampMixin):
    __tablename__ = "findings"
    __table_args__ = (
        Index("ix_findings_engagement_status", "engagement_id", "status"),
        # Defence in depth behind the service-layer rule. If any future code path
        # tries to write an approved Finding without a reviewer, the database
        # refuses the row rather than letting the invariant erode silently
        # (ADR-003, 08_TESTING.md § Security Tests).
        CheckConstraint(
            "status <> 'approved' OR (reviewed_by IS NOT NULL AND final_status IS NOT NULL)",
            name="ck_approved_requires_reviewer",
        ),
    )

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

    # Citation detail per ADR-011 item 5: [{evidence_document_id, location}].
    citations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # Derived from `citations` by the service layer, kept for cheap filtering.
    evidence_document_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, default=list, server_default="{}"
    )

    # --- AI suggestion (all nullable: the LLM may have failed) ---------------
    ai_suggested_status: Mapped[ComplianceStatus | None] = mapped_column(
        Enum(ComplianceStatus, name="compliance_status"), nullable=True
    )
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Sensitivity: Sensitive — may reference client-specific detail.
    ai_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    needs_manual_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # --- Human determination -------------------------------------------------
    status: Mapped[FindingStatus] = mapped_column(
        Enum(FindingStatus, name="finding_status"),
        nullable=False,
        default=FindingStatus.draft,
        index=True,
    )
    final_status: Mapped[ComplianceStatus | None] = mapped_column(
        Enum(ComplianceStatus, name="compliance_status", create_type=False), nullable=True
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    history: Mapped[list[FindingHistory]] = relationship(
        back_populates="finding", order_by="FindingHistory.created_at"
    )


class FindingHistory(Base):
    """Append-only log of every state change, including Reviewer overrides.

    This table exists specifically so nothing is ever silently overwritten
    (03_DATA_MODEL.md). It is written in the same transaction as the Finding
    update it describes — never one without the other (§8.3).
    """

    __tablename__ = "finding_history"

    id: Mapped[uuid.UUID] = uuid_pk()
    finding_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("findings.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[FindingAction] = mapped_column(
        Enum(FindingAction, name="finding_action"), nullable=False
    )
    previous_status: Mapped[FindingStatus] = mapped_column(
        Enum(FindingStatus, name="finding_status", create_type=False), nullable=False
    )
    new_status: Mapped[FindingStatus] = mapped_column(
        Enum(FindingStatus, name="finding_status", create_type=False), nullable=False
    )
    # The prior determination this action replaced, so an override records what
    # it overrode rather than just that it happened.
    previous_final_status: Mapped[ComplianceStatus | None] = mapped_column(
        Enum(ComplianceStatus, name="compliance_status", create_type=False), nullable=True
    )
    new_final_status: Mapped[ComplianceStatus | None] = mapped_column(
        Enum(ComplianceStatus, name="compliance_status", create_type=False), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    finding: Mapped[Finding] = relationship(back_populates="history")


class Report(Base):
    """The immutable snapshot handed to the client.

    `snapshot_data` is a full copy rather than a set of foreign keys on purpose:
    a report must keep saying what it said on the day it was signed, even if the
    corpus is later re-versioned (03_DATA_MODEL.md).
    """

    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = uuid_pk()
    engagement_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("engagements.id", ondelete="RESTRICT"),
        nullable=False,
        # One report per engagement: finalize is terminal, and a second call
        # returns 409 ALREADY_FINALIZED rather than creating a duplicate.
        unique=True,
    )
    snapshot_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    generated_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    generated_at: Mapped[datetime] = created_at_column()
    pdf_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
