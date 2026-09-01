"""EvidenceFact and ControlEvaluation — the deterministic core's two entities
(03_DATA_MODEL.md).

These are the entities that separate "a document exists" from "a checkable claim
exists", and "a model thinks this is compliant" from "the rules, run against
provenanced facts, produced this result".

Both are append-only. A re-extraction or a re-evaluation writes a new row rather
than editing an old one, because the history of what the system claimed, and
when, is itself evidentiary — an audit trail that can be overwritten is not one.
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_column, uuid_pk
from app.models.corpus import ControlDefinition
from app.models.enums import (
    EvaluationMode,
    EvaluationResult,
    EvidenceStrength,
    FactValueType,
    GateStatus,
    VerificationStatus,
)


class EvidenceFact(Base):
    """A structured, source-traceable claim extracted from one evidence document.

    The provenance columns are not metadata decoration — `document_id` plus
    `page`/`line`/`cell` plus `source_hash` are what the Evidence Gate re-checks
    before any result reaches a human. A fact that cannot say exactly where it
    came from cannot be verified, and 01_REQUIREMENTS.md forbids creating one.
    """

    __tablename__ = "evidence_facts"
    __table_args__ = (
        Index("ix_evidence_facts_lookup", "audit_id", "control_definition_id", "name"),
        # A VERIFIED fact must carry a checkable location (TASK-104). The service
        # layer enforces this too; the constraint is what makes a future code
        # path unable to erode it quietly.
        CheckConstraint(
            "verification_status <> 'VERIFIED' "
            "OR (page IS NOT NULL OR line IS NOT NULL OR cell IS NOT NULL)",
            name="ck_verified_requires_location",
        ),
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
    # Matches a name declared in ControlDefinition.facts.
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Sensitivity: Sensitive (03_DATA_MODEL.md §8.4) — reveals specific client
    # configuration values. Stored as text and cast per `value_type` at
    # evaluation time, so one column serves every fact shape.
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_type: Mapped[FactValueType] = mapped_column(
        Enum(FactValueType, name="fact_value_type"), nullable=False
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("evidence_documents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # The checkable location. At least one is populated for a VERIFIED fact.
    page: Mapped[int | None] = mapped_column(nullable=True)
    line: Mapped[int | None] = mapped_column(nullable=True)
    cell: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # The document's content hash at extraction time. A later mismatch means the
    # underlying file changed and this fact can no longer be trusted — detected
    # at gate time rather than by deleting the row.
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # When the fact was true per the evidence itself, where the evidence states
    # it. Distinct from `extracted_at`, and it is what freshness is measured on.
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extracted_at: Mapped[datetime] = created_at_column()
    extractor_version: Mapped[str] = mapped_column(String(40), nullable=False)
    verification_status: Mapped[VerificationStatus] = mapped_column(
        Enum(VerificationStatus, name="verification_status"),
        nullable=False,
        default=VerificationStatus.UNVERIFIED,
    )

    @property
    def location_label(self) -> str:
        """Human-readable citation, e.g. "page 7". Used verbatim in the review UI."""
        if self.page is not None:
            return f"page {self.page}"
        if self.line is not None:
            return f"line {self.line}"
        if self.cell is not None:
            return f"cell {self.cell}"
        return "unspecified location"


class ControlEvaluation(Base):
    """The mechanical, LLM-free result of running one control's rules.

    `result` is the single field in this system with no API write path at all
    (03_DATA_MODEL.md §8.2, 05_SECURITY.md §10.3). It is not merely
    permission-gated: no Pydantic request model anywhere includes it, so there is
    no field name a client could even send. The only writer is the rule engine.
    """

    __tablename__ = "control_evaluations"
    __table_args__ = (
        Index("ix_control_evaluations_audit", "audit_id", "control_definition_id"),
        # 03_DATA_MODEL.md §8.3: a row must never exist with no gate verdict,
        # even transiently — the evaluation and its gate result are written in
        # one transaction.
        CheckConstraint(
            "gate_status <> 'VERIFIED' OR array_length(gate_checks_failed, 1) IS NULL",
            name="ck_verified_gate_has_no_failures",
        ),
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

    result: Mapped[EvaluationResult] = mapped_column(
        Enum(EvaluationResult, name="evaluation_result"), nullable=False
    )
    evaluation_mode: Mapped[EvaluationMode] = mapped_column(
        Enum(EvaluationMode, name="evaluation_mode", create_type=False), nullable=False
    )
    # Which facts the engine actually consumed.
    facts_used: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, default=list, server_default="{}"
    )
    # A snapshot of the rules as applied, not a foreign key to them: a later
    # edit to the control's rules must not retroactively change what this
    # evaluation claims to have checked.
    rules_used: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # Denormalised citations for fast display in the review queue.
    evidence_locations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # Populated when result=CONFLICT: the specific disagreeing facts, so the
    # auditor sees what conflicts rather than merely that something did.
    contradictions: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    # Set alongside the mechanical result when evidence is past the control's
    # freshness window — never silently treated as current.
    stale: Mapped[bool] = mapped_column(nullable=False, default=False)

    # How well-supported this result is, graded mechanically (never by a model)
    # from verification status, corroboration, freshness margin and citation
    # granularity. `strength_factors` names which criteria fired, so the review
    # UI can show an auditor *why* rather than an unexplained grade.
    evidence_strength: Mapped[EvidenceStrength] = mapped_column(
        Enum(EvidenceStrength, name="evidence_strength"),
        nullable=False,
        default=EvidenceStrength.NONE,
        server_default="NONE",
    )
    strength_factors: Mapped[list[str]] = mapped_column(
        ARRAY(String(48)), nullable=False, default=list, server_default="{}"
    )

    gate_status: Mapped[GateStatus] = mapped_column(
        Enum(GateStatus, name="gate_status"), nullable=False
    )
    gate_checks_failed: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), nullable=False, default=list, server_default="{}"
    )

    evaluated_at: Mapped[datetime] = created_at_column()
    engine_version: Mapped[str] = mapped_column(String(40), nullable=False)
    # 02_ARCHITECTURE.md §7.8: whether an LLM contributed to *any* input to this
    # evaluation. For a DETERMINISTIC control this must be false, which makes the
    # invariant monitorable in production rather than merely intended.
    llm_involved: Mapped[bool] = mapped_column(nullable=False, default=False)

    control: Mapped[ControlDefinition] = relationship(lazy="joined")
