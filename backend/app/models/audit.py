"""Audit, AuditAssignment and ClientProfileDocument (03_DATA_MODEL.md)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, created_at_column, uuid_pk
from app.models.enums import AuditStatus, EntityType, MerchantLevel


class Audit(Base, TimestampMixin):
    """One PCI DSS v4.0.1 assessment for one client.

    The central authorization object: nearly every permission check in the
    system resolves to "is this user assigned to this audit, or a
    Reviewer/Admin" (03_DATA_MODEL.md §8.2).
    """

    __tablename__ = "audits"

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
    # The structured company profile the applicability engine evaluates control
    # conditions against. Validated by the `CompanyProfile` Pydantic model on
    # write and stored as JSONB — the same treatment `ControlDefinition.facts`
    # and `.rules` get, because this data becomes logic (05_SECURITY.md §10.4).
    #
    # A key being absent is meaningful and is not the same as an empty list: an
    # unanswered question yields UNDETERMINED, while an answered-but-empty one is
    # a real "none of these".
    company_profile: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # 01_REQUIREMENTS.md § Audit Creation: the Level 0 acceptance table runs
    # against a fabricated company with deliberately constructed pass/fail/
    # missing/conflicting evidence. This flag keeps that data structurally
    # distinguishable from real client work in every view, export and report.
    test_company: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    status: Mapped[AuditStatus] = mapped_column(
        Enum(AuditStatus, name="audit_status"),
        nullable=False,
        default=AuditStatus.intake,
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

    assignments: Mapped[list[AuditAssignment]] = relationship(
        back_populates="audit", cascade="save-update, merge"
    )

    @property
    def is_finalized(self) -> bool:
        return self.status == AuditStatus.finalized


class AuditAssignment(Base):
    """Which users may access which audit.

    This join table *is* the ownership boundary. Every audit-scoped query
    joins against it rather than filtering in Python (03_DATA_MODEL.md §8.2).
    """

    __tablename__ = "audit_assignments"
    __table_args__ = (UniqueConstraint("audit_id", "user_id", name="uq_assignment"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    audit_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("audits.id", ondelete="RESTRICT"),
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

    audit: Mapped[Audit] = relationship(back_populates="assignments")


class ClientProfileDocument(Base):
    """A firm-held document about a client, referenced by `source_document_ids`
    at audit creation (ADR-011 item 6).

    Distinct from EvidenceDocument: these are the firm's own files, are not
    attached to an audit, and never enter the extraction/matching pipeline.
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
