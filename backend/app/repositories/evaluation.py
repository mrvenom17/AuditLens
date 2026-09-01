"""Repositories for EvidenceFact and ControlEvaluation.

Both entities are audit-scoped and append-only. Neither repository exposes an
update or a delete: a superseding fact or a re-evaluation is a new row
(03_DATA_MODEL.md), and the absence of a mutation method is the cheapest way to
keep that true.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.models.enums import (
    EvaluationMode,
    EvaluationResult,
    EvidenceStrength,
    FactValueType,
    GateStatus,
    VerificationStatus,
)
from app.models.evaluation import ControlEvaluation, EvidenceFact
from app.repositories.base import AuditScopedRepository

if TYPE_CHECKING:
    from app.api.deps import Actor


class EvidenceFactRepository(AuditScopedRepository):
    def create(
        self,
        *,
        audit_id: uuid.UUID,
        control_definition_id: uuid.UUID,
        document_id: uuid.UUID,
        name: str,
        value: str | None,
        value_type: FactValueType,
        page: int | None,
        line: int | None,
        cell: str | None,
        source_hash: str,
        observed_at: datetime | None,
        extractor_version: str,
        verification_status: VerificationStatus,
    ) -> EvidenceFact:
        fact = EvidenceFact(
            audit_id=audit_id,
            control_definition_id=control_definition_id,
            document_id=document_id,
            name=name,
            value=value,
            value_type=value_type,
            page=page,
            line=line,
            cell=cell,
            source_hash=source_hash,
            observed_at=observed_at,
            extractor_version=extractor_version,
            verification_status=verification_status,
        )
        self._db.add(fact)
        self._db.flush()
        return fact

    def list_for_audit(
        self,
        audit_id: uuid.UUID,
        actor: Actor,
        *,
        control_definition_id: uuid.UUID | None = None,
        verification_status: VerificationStatus | None = None,
    ) -> list[EvidenceFact]:
        stmt = select(EvidenceFact).where(EvidenceFact.audit_id == audit_id)
        if control_definition_id is not None:
            stmt = stmt.where(EvidenceFact.control_definition_id == control_definition_id)
        if verification_status is not None:
            stmt = stmt.where(EvidenceFact.verification_status == verification_status)
        stmt = self._scoped(stmt, EvidenceFact.audit_id, actor)
        return list(self._db.scalars(stmt.order_by(EvidenceFact.extracted_at)))

    def list_for_control(
        self, audit_id: uuid.UUID, control_definition_id: uuid.UUID
    ) -> list[EvidenceFact]:
        """Internal read for the evaluation pipeline — no actor, because the
        worker is not a user. Callers reach this only from a context that has
        already established the audit."""
        return list(
            self._db.scalars(
                select(EvidenceFact)
                .where(
                    EvidenceFact.audit_id == audit_id,
                    EvidenceFact.control_definition_id == control_definition_id,
                )
                .order_by(EvidenceFact.extracted_at)
            )
        )

    def delete_for_control(self, audit_id: uuid.UUID, control_definition_id: uuid.UUID) -> None:
        """Used only when re-extracting a control's facts from scratch, so a
        re-run does not accumulate duplicate rows that would read as a false
        contradiction."""
        for fact in self.list_for_control(audit_id, control_definition_id):
            self._db.delete(fact)
        self._db.flush()


class ControlEvaluationRepository(AuditScopedRepository):
    def create(
        self,
        *,
        audit_id: uuid.UUID,
        control_definition_id: uuid.UUID,
        result: EvaluationResult,
        evaluation_mode: EvaluationMode,
        facts_used: list[uuid.UUID],
        rules_used: list[dict[str, Any]],
        evidence_locations: list[dict[str, Any]],
        contradictions: list[dict[str, Any]] | None,
        stale: bool,
        gate_status: GateStatus,
        gate_checks_failed: list[str],
        evidence_strength: EvidenceStrength,
        strength_factors: list[str],
        engine_version: str,
        llm_involved: bool,
    ) -> ControlEvaluation:
        """The only writer of `result` in the entire system.

        03_DATA_MODEL.md §8.2: this field has no API write path at all — not a
        permission-gated one, none. Keeping creation here, with no update
        method beside it, is what makes that structural rather than a
        convention someone could forget.
        """
        evaluation = ControlEvaluation(
            audit_id=audit_id,
            control_definition_id=control_definition_id,
            result=result,
            evaluation_mode=evaluation_mode,
            facts_used=facts_used,
            rules_used=rules_used,
            evidence_locations=evidence_locations,
            contradictions=contradictions,
            stale=stale,
            gate_status=gate_status,
            gate_checks_failed=gate_checks_failed,
            evidence_strength=evidence_strength,
            strength_factors=strength_factors,
            engine_version=engine_version,
            llm_involved=llm_involved,
        )
        self._db.add(evaluation)
        self._db.flush()
        return evaluation

    def list_for_audit(self, audit_id: uuid.UUID, actor: Actor) -> list[ControlEvaluation]:
        stmt = select(ControlEvaluation).where(ControlEvaluation.audit_id == audit_id)
        stmt = self._scoped(stmt, ControlEvaluation.audit_id, actor)
        return list(self._db.scalars(stmt.order_by(ControlEvaluation.evaluated_at)))

    def latest_for_control(
        self, audit_id: uuid.UUID, control_definition_id: uuid.UUID
    ) -> ControlEvaluation | None:
        return self._db.scalars(
            select(ControlEvaluation)
            .where(
                ControlEvaluation.audit_id == audit_id,
                ControlEvaluation.control_definition_id == control_definition_id,
            )
            .order_by(ControlEvaluation.evaluated_at.desc())
            .limit(1)
        ).first()
