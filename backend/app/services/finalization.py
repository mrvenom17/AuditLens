"""Audit finalization and report generation (TASK-021).

01_REQUIREMENTS.md § Audit Finalization. 05_SECURITY.md §10.11 and
07_TASKS.md both single this out as the highest-stakes path in the system, and
04_API_CONTRACT.md calls it "the single highest-stakes endpoint".

Three rules, all enforced here rather than in the route:

* **Reviewer only.** Not Admin. 00_PRODUCT.md §5.3: an Admin "cannot finalize
  audits unless also a Reviewer — sign-off authority is a role property,
  not an escalation path."
* **No unresolved drafts.** Every confirmed requirement must have an approved
  Finding, or an explicitly acknowledged gap. The 409 names exactly what is
  blocking.
* **Never automatic.** 01_REQUIREMENTS.md, Explicitly Forbidden Behavior: "The
  system must never auto-finalize an audit on any schedule, timeout, or
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
from app.models.audit import Audit
from app.models.corpus import ControlDefinition
from app.models.enums import AuditStatus, FindingStatus, Role
from app.models.finding import Report
from app.models.scoping import ScopedControl
from app.repositories.audit import AuditRepository
from app.repositories.finding import FindingRepository, ReportRepository
from app.repositories.scoping import ScopedRequirementRepository
from app.repositories.user import UserRepository
from app.services import genai_service
from app.services.audit import AuditService

if TYPE_CHECKING:
    from app.api.deps import Actor

logger = logging.getLogger(__name__)


class BlockingItem(NamedTuple):
    scoped_control_id: uuid.UUID
    control_id: str
    reason: str


class FinalizationService:
    def __init__(self, db: DBSession) -> None:
        self._db = db
        self._audits = AuditService(db)
        self._audit_repo = AuditRepository(db)
        self._findings = FindingRepository(db)
        self._scoped = ScopedRequirementRepository(db)
        self._reports = ReportRepository(db)
        self._users = UserRepository(db)

    def check_blockers(self, audit_id: uuid.UUID, actor: Actor) -> list[BlockingItem]:
        """What currently prevents finalization.

        Exposed separately from `finalize` so the UI can show the Reviewer their
        remaining work without attempting the action and reading it out of a 409.

        The access check is explicit and comes first. Relying on the ownership
        filter inside `list_for_audit` is not sufficient here: for a caller
        with no access it returns an empty set, which this method would read as
        "nothing is blocking" and report as `ready: true`. An authorization
        failure that presents as a confident answer is worse than one that
        presents as an error, so the denial has to be raised rather than
        inferred from an empty result.
        """
        self._audits.get(audit_id, actor)

        confirmed = self._scoped.list_for_audit(audit_id, actor, confirmed_only=True)
        # These two are keyed on audit id alone, which is safe only because
        # access to this audit has just been asserted above.
        approved_by_requirement = {
            f.scoped_control_id for f in self._findings.approved_for_audit(audit_id)
        }
        drafts_by_requirement: dict[uuid.UUID, int] = {}
        # A Finding not linked to a scope row cannot be attributed to a control,
        # but it is still unreviewed — and an unreviewed item that does not block
        # sign-off would defeat the one guarantee this product makes. Counted
        # separately and reported below rather than skipped.
        unattributed_drafts = 0
        for draft in self._findings.unresolved_drafts(audit_id):
            if draft.scoped_control_id is None:
                unattributed_drafts += 1
                continue
            drafts_by_requirement[draft.scoped_control_id] = (
                drafts_by_requirement.get(draft.scoped_control_id, 0) + 1
            )

        blockers: list[BlockingItem] = []
        if unattributed_drafts:
            blockers.append(
                BlockingItem(
                    uuid.UUID(int=0),
                    "-",
                    f"{unattributed_drafts} finding"
                    f"{'' if unattributed_drafts == 1 else 's'} awaiting review",
                )
            )
        for scoped in confirmed:
            control_id = scoped.control.control_id
            if scoped.id in drafts_by_requirement:
                # An unreviewed draft blocks regardless of anything else: the
                # whole point of the product is that a human saw every item.
                count = drafts_by_requirement[scoped.id]
                blockers.append(
                    BlockingItem(
                        scoped.id,
                        control_id,
                        # Shown verbatim to a Reviewer deciding whether to sign
                        # off, so it agrees in number rather than reading
                        # "1 finding(s)".
                        f"{count} finding{'' if count == 1 else 's'} still awaiting review",
                    )
                )
            elif scoped.id not in approved_by_requirement and not scoped.gap_acknowledged:
                blockers.append(
                    BlockingItem(
                        scoped.id,
                        control_id,
                        "no approved finding and no acknowledged gap",
                    )
                )
        return blockers

    def finalize(self, audit_id: uuid.UUID, actor: Actor) -> Report:
        # Order matters. The role check comes first so that a non-Reviewer
        # learns nothing about the audit's readiness — they get a flat 403
        # whatever state it is in, which is what 04_API_CONTRACT.md requires
        # ("403 ... regardless of Finding state").
        if actor.role != Role.reviewer:
            raise ForbiddenError("Only a Reviewer may finalize an audit.")

        audit = self._audits.get(audit_id, actor)

        if audit.status == AuditStatus.finalized:
            # 04_API_CONTRACT.md Idempotency: 409, never a second Report.
            raise ConflictError(
                "This audit has already been finalized.",
                code=CODE_ALREADY_FINALIZED,
            )

        blockers = self.check_blockers(audit_id, actor)
        if blockers:
            raise ConflictError(
                "This audit has unresolved findings and cannot be finalized.",
                code=CODE_UNRESOLVED_FINDINGS,
                blocking_requirements=[
                    {
                        "scoped_control_id": str(b.scoped_control_id),
                        "control_id": b.control_id,
                        "reason": b.reason,
                    }
                    for b in blockers
                ],
            )

        snapshot = self._build_snapshot(audit, actor)
        report = self._reports.create(
            audit_id=audit_id,
            snapshot_data=snapshot,
            generated_by=actor.id,
            corpus_version=", ".join(snapshot.get("corpus_versions", [])) or None,
            engine_version=", ".join(snapshot.get("engine_versions", [])) or None,
        )

        audit.finalized_by = actor.id
        audit.finalized_at = datetime.now(UTC)
        self._audits.advance_status(audit, AuditStatus.finalized)
        self._db.flush()

        logger.info(
            "audit.finalized audit=%s reviewer=%s report=%s",
            audit_id,
            actor.id,
            report.id,
        )
        return report

    def _build_snapshot(self, audit: Audit, actor: Actor) -> dict[str, Any]:
        """Copy the approved findings and acknowledged gaps into the Report.

        A full copy rather than references, because 03_DATA_MODEL.md requires
        the Report be immutable: it must keep saying what it said on the day it
        was signed even if the corpus is later re-versioned. That is also why
        the requirement text, the rules as applied and the evidence citations
        are all copied in, not just their ids — the report has to remain
        defensible evidence of exactly what was checked, mechanically, versus
        what was decided, humanly.
        """
        rows = self._db.execute(
            select(ScopedControl, ControlDefinition)
            .join(ControlDefinition, ScopedControl.control_definition_id == ControlDefinition.id)
            .where(
                ScopedControl.audit_id == audit.id,
                ScopedControl.confirmed.is_(True),
            )
            .order_by(ControlDefinition.requirement_family, ControlDefinition.control_id)
        ).all()

        approved = {f.scoped_control_id: f for f in self._findings.approved_for_audit(audit.id)}
        reviewer_names = self._reviewer_names(list(approved.values()))

        findings: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        corpus_versions: set[str] = set()
        engine_versions: set[str] = set()

        for scoped, requirement in rows:
            corpus_versions.add(requirement.corpus_version)
            finding = approved.get(scoped.id)
            if finding is not None:
                evaluation = finding.evaluation
                if evaluation is not None:
                    engine_versions.add(evaluation.engine_version)
                findings.append(
                    {
                        "control_id": requirement.control_id,
                        "requirement_family": requirement.requirement_family,
                        "name": requirement.name,
                        "requirement_text": requirement.requirement_text,
                        "assessment_procedures": list(requirement.assessment_procedures),
                        "applicability_conditions": list(requirement.applicability_conditions),
                        "evaluation_mode": requirement.evaluation_mode.value,
                        # --- What the machine determined, mechanically -------
                        # Retained alongside the human decision, never merged
                        # into it: 03_DATA_MODEL.md → Report requires both be
                        # preserved so a later reader can see where the auditor
                        # agreed with the engine and where they did not.
                        "system_result": evaluation.result.value if evaluation else None,
                        "gate_status": evaluation.gate_status.value if evaluation else None,
                        "gate_checks_failed": (
                            list(evaluation.gate_checks_failed) if evaluation else []
                        ),
                        "rules_used": evaluation.rules_used if evaluation else [],
                        "evidence_locations": (evaluation.evidence_locations if evaluation else []),
                        "contradictions": evaluation.contradictions if evaluation else None,
                        "stale_evidence": bool(evaluation.stale) if evaluation else False,
                        # How much weight the evidence bears, and why. A reader
                        # of a frozen report should be able to tell a
                        # well-corroborated PASS from a thinly-supported one.
                        "evidence_strength": (
                            evaluation.evidence_strength.value if evaluation else None
                        ),
                        "strength_factors": (
                            list(evaluation.strength_factors) if evaluation else []
                        ),
                        "engine_version": evaluation.engine_version if evaluation else None,
                        "llm_involved_in_result": (
                            bool(evaluation.llm_involved) if evaluation else None
                        ),
                        # --- What the human decided --------------------------
                        "auditor_decision": (
                            finding.auditor_decision.value if finding.auditor_decision else None
                        ),
                        "is_override": finding.is_override,
                        "review_note": finding.review_note,
                        # Non-authoritative prose, labelled as such in the
                        # report itself so it can never be mistaken for the
                        # determination.
                        "ai_explanation": finding.ai_explanation,
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
                        "control_id": requirement.control_id,
                        "name": requirement.name,
                        "gap_note": scoped.gap_note,
                    }
                )

        rejected = [
            f
            for f in self._findings.list_for_audit(audit.id, actor)
            if f.status == FindingStatus.rejected
        ]

        return {
            "audit": {
                "id": str(audit.id),
                "client_name": audit.client_name,
                "entity_type": audit.entity_type.value,
                "merchant_level": audit.merchant_level.value if audit.merchant_level else None,
                "existing_saq_type": audit.existing_saq_type,
            },
            "framework": "PCI DSS v4.0.1",
            "corpus_versions": sorted(corpus_versions),
            # Stamped so a later change to either the control corpus or the rule
            # engine can never retroactively alter what this report claims
            # (01_REQUIREMENTS.md § Audit Finalization).
            "engine_versions": sorted(engine_versions),
            # 03_DATA_MODEL.md → Report: the report discloses where GenAI was
            # involved and in what role, rather than leaving a reader to guess.
            "ai_disclosure": genai_service.explanation_metadata(),
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

    def get_report(self, audit_id: uuid.UUID, actor: Actor) -> Report:
        self._audits.get(audit_id, actor)
        report = self._reports.get_for_audit(audit_id, actor)
        if report is None:
            raise NotFoundError("This audit has not been finalized.")
        return report
