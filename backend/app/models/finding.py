"""Finding, FindingHistory and Report — the highest-stakes entities.

A Finding is now a *wrapper* around a ControlEvaluation, not a container for an
AI suggestion (03_DATA_MODEL.md § Finding — REDEFINED). That change is the whole
point of this revision: the machine's determination lives on the evaluation and
is immutable, while the human's determination lives here, in a genuinely
separate column. Nothing merges them, so the audit trail can always answer "what
did the engine say, and what did the human decide" — including when they differ.

The invariant ADR-003 makes structural lives in the service layer
(`app/services/finding.py`), because a database CHECK constraint cannot express
"reviewed_by must be the authenticated actor". What this module contributes is
that `reviewed_by` is nullable and `status` defaults to pending_review, so the
*only* way a row reaches `approved` is through code that deliberately sets both.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, created_at_column, uuid_pk
from app.models.enums import EvaluationResult, FindingAction, FindingStatus
from app.models.evaluation import ControlEvaluation


class Finding(Base, TimestampMixin):
    __tablename__ = "findings"
    __table_args__ = (
        Index("ix_findings_audit_status", "audit_id", "status"),
        # Defence in depth behind the service-layer rule. If any future code path
        # tries to write an approved Finding without a reviewer, the database
        # refuses the row rather than letting the invariant erode silently
        # (ADR-003, 08_TESTING.md § Security Tests).
        CheckConstraint(
            "status <> 'approved' OR (reviewed_by IS NOT NULL AND auditor_decision IS NOT NULL)",
            name="ck_approved_requires_reviewer",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    audit_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("audits.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # The machine's determination lives here, one indirection away, and is
    # immutable. A re-evaluation produces a new ControlEvaluation and a new
    # Finding rather than mutating this one.
    control_evaluation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("control_evaluations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    scoped_control_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("scoped_controls.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    # --- GenAI rendering. Never authoritative, never read by the engine. -----
    # Sensitivity: Sensitive — may reference client-specific detail. This column
    # is display-only: 02_ARCHITECTURE.md §7.4 forbids any code path where its
    # content flows into a field that determines compliance.
    ai_explanation: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Human determination -------------------------------------------------
    status: Mapped[FindingStatus] = mapped_column(
        Enum(FindingStatus, name="finding_status"),
        nullable=False,
        default=FindingStatus.pending_review,
        index=True,
    )
    # Same value set as ControlEvaluation.result, and deliberately a *different
    # column*. 01_REQUIREMENTS.md § Finding Review, Explicitly Forbidden
    # Behavior: system_result is never overwritten by an auditor action, so the
    # override rate stays measurable.
    auditor_decision: Mapped[EvaluationResult | None] = mapped_column(
        Enum(EvaluationResult, name="evaluation_result", create_type=False), nullable=True
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    evaluation: Mapped[ControlEvaluation] = relationship(lazy="joined")
    history: Mapped[list[FindingHistory]] = relationship(
        back_populates="finding", order_by="FindingHistory.created_at"
    )

    @property
    def is_override(self) -> bool:
        """True when the human's decision differs from the machine's.

        Surfaced explicitly because 02_ARCHITECTURE.md §7.8 treats the override
        rate as a product-quality signal about the control definitions, not just
        an audit-trail entry.
        """
        if self.auditor_decision is None or self.evaluation is None:
            return False
        return self.auditor_decision != self.evaluation.result


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
    previous_decision: Mapped[EvaluationResult | None] = mapped_column(
        Enum(EvaluationResult, name="evaluation_result", create_type=False), nullable=True
    )
    new_decision: Mapped[EvaluationResult | None] = mapped_column(
        Enum(EvaluationResult, name="evaluation_result", create_type=False), nullable=True
    )
    # What the machine said at the moment of this decision. Copied onto the
    # history row so an override is legible without re-joining an evaluation
    # that may since have been superseded by a re-run.
    system_result: Mapped[EvaluationResult | None] = mapped_column(
        Enum(EvaluationResult, name="evaluation_result", create_type=False), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    finding: Mapped[Finding] = relationship(back_populates="history")


class Report(Base):
    """The immutable snapshot handed to the client.

    `snapshot_data` is a full copy rather than a set of foreign keys on purpose:
    a report must keep saying what it said on the day it was signed, even if the
    corpus is later re-versioned or the rule engine changes (03_DATA_MODEL.md).
    That is why `corpus_version` and `engine_version` are stamped on the row
    itself — the snapshot records not just what was decided, but what logic and
    what standard text produced it.
    """

    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = uuid_pk()
    audit_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("audits.id", ondelete="RESTRICT"),
        nullable=False,
        # One report per audit: finalize is terminal, and a second call
        # returns 409 ALREADY_FINALIZED rather than creating a duplicate.
        unique=True,
    )
    snapshot_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    corpus_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    engine_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    generated_by: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    generated_at: Mapped[datetime] = created_at_column()
    pdf_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
