"""Finding, FindingHistory and Report data access."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.models.enums import EvaluationResult, FindingAction, FindingStatus
from app.models.finding import Finding, FindingHistory, Report
from app.repositories.base import AuditScopedRepository

if TYPE_CHECKING:
    from app.api.deps import Actor


class FindingRepository(AuditScopedRepository):
    def create(
        self,
        *,
        audit_id: uuid.UUID,
        control_evaluation_id: uuid.UUID,
        scoped_control_id: uuid.UUID | None = None,
        ai_explanation: str | None = None,
    ) -> Finding:
        """Create a Finding wrapping a gated ControlEvaluation.

        `status` and `auditor_decision` are not parameters. A Finding always
        begins awaiting a human, and there is no way to express anything else
        here — the review path is the only route to `approved` (ADR-003).
        """
        finding = Finding(
            audit_id=audit_id,
            control_evaluation_id=control_evaluation_id,
            scoped_control_id=scoped_control_id,
            ai_explanation=ai_explanation,
            status=FindingStatus.pending_review,
        )
        self._db.add(finding)
        self._db.flush()
        return finding

    def list_for_audit(
        self,
        audit_id: uuid.UUID,
        actor: Actor,
        *,
        status: FindingStatus | None = None,
    ) -> list[Finding]:
        stmt = select(Finding).where(Finding.audit_id == audit_id)
        if status is not None:
            stmt = stmt.where(Finding.status == status)
        stmt = self._scoped(stmt, Finding.audit_id, actor)
        return list(self._db.scalars(stmt.order_by(Finding.created_at)).all())

    def get_scoped(self, finding_id: uuid.UUID, actor: Actor) -> Finding | None:
        stmt = self._scoped(
            select(Finding).where(Finding.id == finding_id), Finding.audit_id, actor
        )
        return self._db.scalar(stmt)

    def exists_unscoped(self, finding_id: uuid.UUID) -> bool:
        return self._db.scalar(select(Finding.id).where(Finding.id == finding_id)) is not None

    def existing_for_control(
        self, audit_id: uuid.UUID, scoped_control_id: uuid.UUID
    ) -> list[Finding]:
        return list(
            self._db.scalars(
                select(Finding).where(
                    Finding.audit_id == audit_id,
                    Finding.scoped_control_id == scoped_control_id,
                )
            ).all()
        )

    def unresolved_drafts(self, audit_id: uuid.UUID) -> list[Finding]:
        """Findings still awaiting a human — the set that blocks finalization.

        `needs_more_evidence` counts as unresolved too: the auditor asked for
        something and it has not come back, so the audit is not finished.
        """
        return list(
            self._db.scalars(
                select(Finding).where(
                    Finding.audit_id == audit_id,
                    Finding.status.in_(
                        (FindingStatus.pending_review, FindingStatus.needs_more_evidence)
                    ),
                )
            ).all()
        )

    def approved_for_audit(self, audit_id: uuid.UUID) -> list[Finding]:
        return list(
            self._db.scalars(
                select(Finding)
                .where(
                    Finding.audit_id == audit_id,
                    Finding.status == FindingStatus.approved,
                )
                .order_by(Finding.created_at)
            ).all()
        )

    def add_history(
        self,
        *,
        finding_id: uuid.UUID,
        actor_id: uuid.UUID,
        action: FindingAction,
        previous_status: FindingStatus,
        new_status: FindingStatus,
        previous_decision: EvaluationResult | None,
        new_decision: EvaluationResult | None,
        system_result: EvaluationResult | None,
        note: str | None,
    ) -> FindingHistory:
        """Append a history row.

        Called only from `FindingService.review`, in the same transaction as the
        Finding update it describes — 03_DATA_MODEL.md §8.3 requires the two to
        be written together, never one without the other.
        """
        entry = FindingHistory(
            finding_id=finding_id,
            actor_id=actor_id,
            action=action,
            previous_status=previous_status,
            new_status=new_status,
            previous_decision=previous_decision,
            new_decision=new_decision,
            system_result=system_result,
            note=note,
        )
        self._db.add(entry)
        self._db.flush()
        return entry

    def history_for(self, finding_id: uuid.UUID) -> list[FindingHistory]:
        return list(
            self._db.scalars(
                select(FindingHistory)
                .where(FindingHistory.finding_id == finding_id)
                .order_by(FindingHistory.created_at)
            ).all()
        )


class ReportRepository(AuditScopedRepository):
    def create(
        self,
        *,
        audit_id: uuid.UUID,
        snapshot_data: dict[str, Any],
        generated_by: uuid.UUID,
        corpus_version: str | None = None,
        engine_version: str | None = None,
    ) -> Report:
        report = Report(
            audit_id=audit_id,
            snapshot_data=snapshot_data,
            generated_by=generated_by,
            corpus_version=corpus_version,
            engine_version=engine_version,
        )
        self._db.add(report)
        self._db.flush()
        return report

    def get_for_audit(self, audit_id: uuid.UUID, actor: Actor) -> Report | None:
        stmt = self._scoped(
            select(Report).where(Report.audit_id == audit_id),
            Report.audit_id,
            actor,
        )
        return self._db.scalar(stmt)

    def exists_for_audit(self, audit_id: uuid.UUID) -> bool:
        return self._db.scalar(select(Report.id).where(Report.audit_id == audit_id)) is not None
