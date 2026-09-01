"""ControlDefinition — the machine-readable control corpus (03_DATA_MODEL.md).

This is firm-wide reference data, not client data, so it carries no ownership
rules and no audit scoping. A corpus update inserts rows under a new
`corpus_version` rather than mutating existing ones, so a past audit always
cites the text — and the rules — that were actually in effect when it ran.

What makes this entity different from the prior revision's PCIRequirement is
`facts` and `rules`: free-text requirement prose alone cannot be executed, and
the whole deterministic core rests on a human having authored, per control, the
exact fact names to look for and the exact comparisons to apply. Those two
columns are the ground truth the rule engine runs on, which is why
01_REQUIREMENTS.md forbids an LLM from ever populating them.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.config.settings import settings
from app.db.base import Base, created_at_column, uuid_pk
from app.models.enums import EvaluationMode


class ControlDefinition(Base):
    __tablename__ = "control_definitions"
    __table_args__ = (
        UniqueConstraint("control_id", "corpus_version", name="uq_clause_per_version"),
        Index("ix_control_definitions_family", "requirement_family"),
        Index("ix_control_definitions_mode", "evaluation_mode"),
        # Belt and suspenders, per TASK-102: the service layer validates this at
        # authoring time, and the database refuses the row anyway. A
        # DETERMINISTIC control with no rules is the one malformed record that
        # would quietly turn the deterministic engine into a no-op.
        CheckConstraint(
            "evaluation_mode <> 'DETERMINISTIC' "
            "OR (jsonb_array_length(rules) > 0 AND jsonb_array_length(facts) > 0)",
            name="ck_deterministic_requires_rules",
        ),
        # A STRUCTURED control checks that its declared facts are *present and
        # well-formed*. With no facts declared it would check nothing and return
        # INSUFFICIENT_EVIDENCE forever, which reads as missing evidence rather
        # than as the authoring mistake it is.
        CheckConstraint(
            "evaluation_mode <> 'STRUCTURED' OR jsonb_array_length(facts) > 0",
            name="ck_structured_requires_facts",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    control_id: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    requirement_family: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    # Sensitivity: Public — the published standard. Under ADR-010 the shipped
    # corpus carries firm-authored summaries, not the Council's text, and
    # `corpus_version` records which.
    requirement_text: Mapped[str] = mapped_column(Text, nullable=False)

    evaluation_mode: Mapped[EvaluationMode] = mapped_column(
        Enum(EvaluationMode, name="evaluation_mode"),
        nullable=False,
        default=EvaluationMode.HUMAN_ASSISTED,
    )
    # [{type, description}] — what to ask the client for.
    evidence_requirements: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # [{name, type}] — the fact schema this control needs extracted.
    facts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # [{fact, operator, expected}] — executed verbatim by the rule engine.
    rules: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # [{fact, operator, expected}] — same shape as `rules`, but evaluated against
    # the company profile at scoping time rather than against evidence. An empty
    # list means the control applies universally.
    #
    # EXISTS/NOT_EXISTS are rejected at authoring time: they answer PASS/FAIL for
    # a fact that is absent, so a condition using one could never report
    # UNDETERMINED — it would silently convert "the company never answered this"
    # into "this control does not apply".
    applicability_conditions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # How an assessor tests this control — shown to the auditor beside the
    # requirement so they can see what was meant to be checked, not just what the
    # engine concluded (01_REQUIREMENTS.md § Finding Review).
    assessment_procedures: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # How old evidence may be before this control treats it as stale. Null means
    # "no freshness constraint", which is a deliberate authoring choice rather
    # than an omission.
    freshness_window_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    corpus_version: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Discovery/RAG only. 02_ARCHITECTURE.md §7.5: no code path may treat a
    # vector similarity score as a compliance signal.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.EMBEDDING_DIMENSIONS), nullable=True
    )
    # Versioning chain (03_DATA_MODEL.md): editing a control's rules creates a
    # new row and points the old one here. Audits already referencing the old
    # definition keep citing it, which is what makes a finalized report immune
    # to a later rule change.
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("control_definitions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = created_at_column()

    @property
    def is_machine_evaluable(self) -> bool:
        """Whether the rule engine may evaluate this control at all.

        HUMAN_ASSISTED controls are excluded by design — routing one through the
        engine and labelling the output deterministic is precisely the dishonesty
        00_PRODUCT.md §5.7 exists to prevent.
        """
        return self.evaluation_mode in (EvaluationMode.DETERMINISTIC, EvaluationMode.STRUCTURED)
