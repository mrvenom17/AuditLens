"""Finding, FindingHistory and Report data access."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.models.enums import ComplianceStatus, FindingAction, FindingStatus
from app.models.finding import Finding, FindingHistory, Report
from app.repositories.base import EngagementScopedRepository

if TYPE_CHECKING:
    from app.api.deps import Actor


class FindingRepository(EngagementScopedRepository):
    def create(
        self,
        *,
        engagement_id: uuid.UUID,
        scoped_requirement_id: uuid.UUID,
        citations: list[dict[str, str]],
        ai_suggested_status: ComplianceStatus | None,
        ai_confidence: float | None,
        ai_rationale: str | None,
        needs_manual_review: bool,
    ) -> Finding:
        """Create a Finding.

        `status` is not a parameter. A Finding always begins as a draft, and
        there is no way to express anything else here — the review path is the
        only route to `approved` (ADR-003).
        """
        document_ids = list(dict.fromkeys(uuid.UUID(c["evidence_document_id"]) for c in citations))
        finding = Finding(
            engagement_id=engagement_id,
            scoped_requirement_id=scoped_requirement_id,
            citations=citations,
            evidence_document_ids=document_ids,
            ai_suggested_status=ai_suggested_status,
            ai_confidence=ai_confidence,
            ai_rationale=ai_rationale,
            needs_manual_review=needs_manual_review,
            status=FindingStatus.draft,
        )
        self._db.add(finding)
        self._db.flush()
        return finding

    def list_for_engagement(
        self,
        engagement_id: uuid.UUID,
        actor: Actor,
        *,
        status: FindingStatus | None = None,
        needs_manual_review: bool | None = None,
    ) -> list[Finding]:
        stmt = select(Finding).where(Finding.engagement_id == engagement_id)
        if status is not None:
            stmt = stmt.where(Finding.status == status)
        if needs_manual_review is not None:
            stmt = stmt.where(Finding.needs_manual_review.is_(needs_manual_review))
        stmt = self._scoped(stmt, Finding.engagement_id, actor)
        return list(self._db.scalars(stmt.order_by(Finding.created_at)).all())

    def get_scoped(self, finding_id: uuid.UUID, actor: Actor) -> Finding | None:
        stmt = self._scoped(
            select(Finding).where(Finding.id == finding_id), Finding.engagement_id, actor
        )
        return self._db.scalar(stmt)

    def exists_unscoped(self, finding_id: uuid.UUID) -> bool:
        return self._db.scalar(select(Finding.id).where(Finding.id == finding_id)) is not None

    def existing_for_requirement(
        self, engagement_id: uuid.UUID, scoped_requirement_id: uuid.UUID
    ) -> list[Finding]:
        return list(
            self._db.scalars(
                select(Finding).where(
                    Finding.engagement_id == engagement_id,
                    Finding.scoped_requirement_id == scoped_requirement_id,
                )
            ).all()
        )

    def unresolved_drafts(self, engagement_id: uuid.UUID) -> list[Finding]:
        """Findings still in `draft` — the set that blocks finalization."""
        return list(
            self._db.scalars(
                select(Finding).where(
                    Finding.engagement_id == engagement_id,
                    Finding.status == FindingStatus.draft,
                )
            ).all()
        )

    def approved_for_engagement(self, engagement_id: uuid.UUID) -> list[Finding]:
        return list(
            self._db.scalars(
                select(Finding)
                .where(
                    Finding.engagement_id == engagement_id,
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
        previous_final_status: ComplianceStatus | None,
        new_final_status: ComplianceStatus | None,
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
            previous_final_status=previous_final_status,
            new_final_status=new_final_status,
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


class ReportRepository(EngagementScopedRepository):
    def create(
        self,
        *,
        engagement_id: uuid.UUID,
        snapshot_data: dict[str, Any],
        generated_by: uuid.UUID,
    ) -> Report:
        report = Report(
            engagement_id=engagement_id,
            snapshot_data=snapshot_data,
            generated_by=generated_by,
        )
        self._db.add(report)
        self._db.flush()
        return report

    def get_for_engagement(self, engagement_id: uuid.UUID, actor: Actor) -> Report | None:
        stmt = self._scoped(
            select(Report).where(Report.engagement_id == engagement_id),
            Report.engagement_id,
            actor,
        )
        return self._db.scalar(stmt)

    def exists_for_engagement(self, engagement_id: uuid.UUID) -> bool:
        return (
            self._db.scalar(select(Report.id).where(Report.engagement_id == engagement_id))
            is not None
        )
