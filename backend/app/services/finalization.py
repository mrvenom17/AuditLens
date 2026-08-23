"""Engagement finalization and report generation (TASK-021).

01_REQUIREMENTS.md § Engagement Finalization. 05_SECURITY.md §10.11 and
07_TASKS.md both single this out as the highest-stakes path in the system, and
04_API_CONTRACT.md calls it "the single highest-stakes endpoint".

Three rules, all enforced here rather than in the route:

* **Reviewer only.** Not Admin. 00_PRODUCT.md §5.3: an Admin "cannot finalize
  engagements unless also a Reviewer — sign-off authority is a role property,
  not an escalation path."
* **No unresolved drafts.** Every confirmed requirement must have an approved
  Finding, or an explicitly acknowledged gap. The 409 names exactly what is
  blocking.
* **Never automatic.** 01_REQUIREMENTS.md, Explicitly Forbidden Behavior: "The
  system must never auto-finalize an engagement on any schedule, timeout, or
  batch process." This method is called from exactly one route, by a Reviewer,
  and from nowhere else — no worker, no scheduler, no cron.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.errors import (
    CODE_ALREADY_FINALIZED,
    CODE_UNRESOLVED_FINDINGS,
    ConflictError,
    ForbiddenError,
    NotFoundError,
)
from app.models.corpus import PCIRequirement
from app.models.engagement import Engagement
from app.models.enums import EngagementStatus, FindingStatus, Role
from app.models.finding import Report
from app.models.scoping import ScopedRequirement
from app.repositories.engagement import EngagementRepository
from app.repositories.finding import FindingRepository, ReportRepository
from app.repositories.scoping import ScopedRequirementRepository
from app.repositories.user import UserRepository
from app.services.engagement import EngagementService

if TYPE_CHECKING:
    from app.api.deps import Actor

logger = logging.getLogger(__name__)


class BlockingItem(NamedTuple):
    scoped_requirement_id: uuid.UUID
    clause_id: str
    reason: str


class FinalizationService:
    def __init__(self, db: DBSession) -> None:
        self._db = db
        self._engagements = EngagementService(db)
        self._engagement_repo = EngagementRepository(db)
        self._findings = FindingRepository(db)
        self._scoped = ScopedRequirementRepository(db)
        self._reports = ReportRepository(db)
        self._users = UserRepository(db)

    def check_blockers(self, engagement_id: uuid.UUID, actor: Actor) -> list[BlockingItem]:
        """What currently prevents finalization.

        Exposed separately from `finalize` so the UI can show the Reviewer their
        remaining work without attempting the action and reading it out of a 409.
        """
        confirmed = self._scoped.list_for_engagement(engagement_id, actor, confirmed_only=True)
        approved_by_requirement = {
            f.scoped_requirement_id for f in self._findings.approved_for_engagement(engagement_id)
        }
        drafts_by_requirement: dict[uuid.UUID, int] = {}
        for draft in self._findings.unresolved_drafts(engagement_id):
            drafts_by_requirement[draft.scoped_requirement_id] = (
                drafts_by_requirement.get(draft.scoped_requirement_id, 0) + 1
            )

        blockers: list[BlockingItem] = []
        for scoped in confirmed:
            clause_id = scoped.requirement.clause_id
            if scoped.id in drafts_by_requirement:
                # An unreviewed draft blocks regardless of anything else: the
                # whole point of the product is that a human saw every item.
                blockers.append(
                    BlockingItem(
                        scoped.id,
                        clause_id,
                        f"{drafts_by_requirement[scoped.id]} finding(s) still awaiting review",
                    )
                )
            elif scoped.id not in approved_by_requirement and not scoped.gap_acknowledged:
                blockers.append(
                    BlockingItem(
                        scoped.id,
                        clause_id,
                        "no approved finding and no acknowledged gap",
                    )
                )
        return blockers

    def finalize(self, engagement_id: uuid.UUID, actor: Actor) -> Report:
        # Order matters. The role check comes first so that a non-Reviewer
        # learns nothing about the engagement's readiness — they get a flat 403
        # whatever state it is in, which is what 04_API_CONTRACT.md requires
        # ("403 ... regardless of Finding state").
        if actor.role != Role.reviewer:
            raise ForbiddenError("Only a Reviewer may finalize an engagement.")

        engagement = self._engagements.get(engagement_id, actor)

        if engagement.status == EngagementStatus.finalized:
            # 04_API_CONTRACT.md Idempotency: 409, never a second Report.
            raise ConflictError(
                "This engagement has already been finalized.",
                code=CODE_ALREADY_FINALIZED,
            )

        blockers = self.check_blockers(engagement_id, actor)
        if blockers:
            raise ConflictError(
                "This engagement has unresolved findings and cannot be finalized.",
                code=CODE_UNRESOLVED_FINDINGS,
                blocking_requirements=[
                    {
                        "scoped_requirement_id": str(b.scoped_requirement_id),
                        "clause_id": b.clause_id,
                        "reason": b.reason,
                    }
                    for b in blockers
                ],
            )

        snapshot = self._build_snapshot(engagement, actor)
        report = self._reports.create(
            engagement_id=engagement_id,
            snapshot_data=snapshot,
            generated_by=actor.id,
        )

        engagement.finalized_by = actor.id
        engagement.finalized_at = datetime.now(UTC)
        self._engagements.advance_status(engagement, EngagementStatus.finalized)
        self._db.flush()

        logger.info(
            "engagement.finalized engagement=%s reviewer=%s report=%s",
            engagement_id,
            actor.id,
            report.id,
        )
        return report

    def _build_snapshot(self, engagement: Engagement, actor: Actor) -> dict[str, Any]:
        """Copy the approved findings and acknowledged gaps into the Report.

        A full copy rather than references, because 03_DATA_MODEL.md requires
        the Report be immutable: it must keep saying what it said on the day it
        was signed even if the corpus is later re-versioned. That is also why
        the clause text is copied in, not just the clause id.
        """
        rows = self._db.execute(
            select(ScopedRequirement, PCIRequirement)
            .join(PCIRequirement, ScopedRequirement.pci_requirement_id == PCIRequirement.id)
            .where(
                ScopedRequirement.engagement_id == engagement.id,
                ScopedRequirement.confirmed.is_(True),
            )
            .order_by(PCIRequirement.requirement_family, PCIRequirement.clause_id)
        ).all()

        approved = {
            f.scoped_requirement_id: f
            for f in self._findings.approved_for_engagement(engagement.id)
        }
        reviewer_names = self._reviewer_names(list(approved.values()))

        findings: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        corpus_versions: set[str] = set()

        for scoped, requirement in rows:
            corpus_versions.add(requirement.corpus_version)
            finding = approved.get(scoped.id)
            if finding is not None:
                findings.append(
                    {
                        "clause_id": requirement.clause_id,
                        "requirement_family": requirement.requirement_family,
                        "title": requirement.title,
                        "requirement_text": requirement.full_text,
                        "final_status": finding.final_status.value
                        if finding.final_status
                        else None,
                        "review_note": finding.review_note,
                        # The AI's original suggestion is carried into the
                        # report's audit record, not shown as the determination.
                        "ai_suggested_status": finding.ai_suggested_status.value
                        if finding.ai_suggested_status
                        else None,
                        "ai_confidence": finding.ai_confidence,
                        "ai_rationale": finding.ai_rationale,
                        "citations": finding.citations,
                        # An approved Finding always has a reviewer — the
                        # ck_approved_requires_reviewer constraint guarantees it
                        # — but the report must stay renderable even if a
                        # deactivated user's name can no longer be resolved.
                        "reviewed_by": (
                            reviewer_names.get(finding.reviewed_by, "unknown")
                            if finding.reviewed_by is not None
                            else "unknown"
                        ),
                        "reviewed_at": finding.reviewed_at.isoformat()
                        if finding.reviewed_at
                        else None,
                    }
                )
            elif scoped.gap_acknowledged:
                gaps.append(
                    {
                        "clause_id": requirement.clause_id,
                        "title": requirement.title,
                        "gap_note": scoped.gap_note,
                    }
                )

        rejected = [
            f
            for f in self._findings.list_for_engagement(engagement.id, actor)
            if f.status == FindingStatus.rejected
        ]

        return {
            "engagement": {
                "id": str(engagement.id),
                "client_name": engagement.client_name,
                "entity_type": engagement.entity_type.value,
                "merchant_level": engagement.merchant_level.value
                if engagement.merchant_level
                else None,
                "existing_saq_type": engagement.existing_saq_type,
            },
            "framework": "PCI DSS v4.0.1",
            "corpus_versions": sorted(corpus_versions),
            "generated_at": datetime.now(UTC).isoformat(),
            "generated_by": {"id": str(actor.id), "name": actor.name, "role": actor.role.value},
            "findings": findings,
            "acknowledged_gaps": gaps,
            "rejected_finding_count": len(rejected),
            "summary": {
                "confirmed_requirements": len(rows),
                "approved_findings": len(findings),
                "acknowledged_gaps": len(gaps),
            },
        }

    def _reviewer_names(self, findings: list[Any]) -> dict[uuid.UUID, str]:
        ids = {f.reviewed_by for f in findings if f.reviewed_by is not None}
        return {
            user.id: user.name
            for user in (self._users.get_by_id(i) for i in ids)
            if user is not None
        }

    # --- Report reads --------------------------------------------------------

    def get_report(self, engagement_id: uuid.UUID, actor: Actor) -> Report:
        self._engagements.get(engagement_id, actor)
        report = self._reports.get_for_engagement(engagement_id, actor)
        if report is None:
            raise NotFoundError("This engagement has not been finalized.")
        return report
