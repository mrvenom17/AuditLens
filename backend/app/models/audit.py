"""Engagement, EngagementAssignment and ClientProfileDocument (03_DATA_MODEL.md)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, created_at_column, uuid_pk
from app.models.enums import EngagementStatus, EntityType, MerchantLevel


class Engagement(Base, TimestampMixin):
    """One PCI DSS v4.0.1 assessment for one client.

    The central authorization object: nearly every permission check in the
    system resolves to "is this user assigned to this engagement, or a
    Reviewer/Admin" (03_DATA_MODEL.md §8.2).
    """

    __tablename__ = "engagements"

    id: Mapped[uuid.UUID] = uuid_pk()
    client_name: Mapped[str] = mapped_column(String(200), nullable=False)
    entity_type: Mapped[EntityType] = mapped_column(
        Enum(EntityType, name="entity_type"), nullable=False
    )
    # Required when entity_type=merchant. That conditional rule is enforced in
    # the Pydantic schema and the service layer, not by a nullable column.
    merchant_level: Mapped[MerchantLevel | None] = mapped_column(
        Enum(MerchantLevel, name="merchant_level"), nullable=True
    )
    annual_transaction_volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    existing_saq_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Sensitivity: Sensitive (03_DATA_MODEL.md §8.4) — never logged.
    tech_stack_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[EngagementStatus] = mapped_column(
        Enum(EngagementStatus, name="engagement_status"),
        nullable=False,
        default=EngagementStatus.intake,
        index=True,
    )

    created_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    finalized_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    assignments: Mapped[list[EngagementAssignment]] = relationship(
        back_populates="engagement", cascade="save-update, merge"
    )

    @property
    def is_finalized(self) -> bool:
        return self.status == EngagementStatus.finalized


class EngagementAssignment(Base):
    """Which users may access which engagement.

    This join table *is* the ownership boundary. Every engagement-scoped query
    joins against it rather than filtering in Python (03_DATA_MODEL.md §8.2).
    """

    __tablename__ = "engagement_assignments"
    __table_args__ = (UniqueConstraint("engagement_id", "user_id", name="uq_assignment"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    engagement_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("engagements.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    assigned_at: Mapped[datetime] = created_at_column()

    engagement: Mapped[Engagement] = relationship(back_populates="assignments")


class ClientProfileDocument(Base):
    """A firm-held document about a client, referenced by `source_document_ids`
    at engagement creation (ADR-011 item 6).

    Distinct from EvidenceDocument: these are the firm's own files, are not
    attached to an engagement, and never enter the extraction/matching pipeline.
    """

    __tablename__ = "client_profile_documents"

    id: Mapped[uuid.UUID] = uuid_pk()
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Sensitivity: Sensitive — never returned in an API response.
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = created_at_column()
