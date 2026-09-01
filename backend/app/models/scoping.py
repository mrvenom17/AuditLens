"""ScopedControl and EvidenceRequest (03_DATA_MODEL.md)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Boolean, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, uuid_pk
from app.models.corpus import ControlDefinition
from app.models.enums import ApplicabilityStatus, EvidenceRequestStatus, ScopeSource


class ScopedControl(Base, TimestampMixin):
    """Which corpus clauses apply to one audit.

    `confirmed = true` is the human gate that unlocks evidence-request
    generation. Nothing in the system may set it without an explicit human
    action (01_REQUIREMENTS.md § PCI DSS Scope Matching, Explicitly Forbidden
    Behavior).
    """

    __tablename__ = "scoped_controls"
    __table_args__ = (
        UniqueConstraint("audit_id", "control_definition_id", name="uq_scoped_control"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    audit_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("audits.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    control_definition_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("control_definitions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source: Mapped[ScopeSource] = mapped_column(
        Enum(ScopeSource, name="scope_source"), nullable=False
    )
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)

    # What the applicability engine concluded, and the evidence for it. Recorded
    # rather than merely acted on: an auditor asked "why is 12.9.1 not in scope?"
    # must be able to see the exact condition that excluded it.
    applicability_status: Mapped[ApplicabilityStatus] = mapped_column(
        Enum(ApplicabilityStatus, name="applicability_status"),
        nullable=False,
        default=ApplicabilityStatus.UNDETERMINED,
        server_default="UNDETERMINED",
    )
    # Serialised RuleOutcome list: fact, operator, expected, actual, result, detail.
    applicability_evidence: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB, nullable=True
    )

    # ADR-011 item 3. Set only by a Reviewer, and only with a stated reason:
    # this is what permits finalizing a confirmed requirement that has no
    # approved Finding (01_REQUIREMENTS.md § Audit Finalization).
    gap_acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    gap_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Eager-loaded: every response that includes a scoped requirement also needs
    # its clause id and title, so a lazy load here would be an N+1 on the list
    # endpoint without exception.
    control: Mapped[ControlDefinition] = relationship(lazy="joined")


class EvidenceRequest(Base, TimestampMixin):
    """A drafted "please provide X" checklist item.

    Never dispatched by the system. `sent_externally` is the auditor's own
    note-to-self after sending through their channel (ADR-004).
    """

    __tablename__ = "evidence_requests"

    id: Mapped[uuid.UUID] = uuid_pk()
    audit_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("audits.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    scoped_control_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("scoped_controls.id", ondelete="RESTRICT"),
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
