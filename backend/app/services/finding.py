"""Finding review — the mandatory human-judgment checkpoint (TASK-019, TASK-020).

This module holds the invariant the whole product rests on (ADR-003):

    No Finding reaches `approved` without `reviewed_by` set.

It is enforced here, at the service layer, rather than in the route, because
02_ARCHITECTURE.md §7.4 requires it to hold for *every* caller — a future route
that forgets to check, a script, a background job. `reviewed_by` is taken from
the authenticated actor passed in and is never read from a request body
(04_API_CONTRACT.md, Security Notes).

08_TESTING.md requires this be tested by calling the service directly, not only
through the API, precisely to catch a bypass path. `tests/test_findings.py` does.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session as DBSession

from app.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.logging_setup import log_finding_transition
from app.models.enums import ComplianceStatus, FindingAction, FindingStatus, Role
from app.models.finding import Finding, FindingHistory
from app.pipelines.matching import DraftFinding
from app.repositories.finding import FindingRepository
from app.services.engagement import EngagementService

if TYPE_CHECKING:
    from app.api.deps import Actor

logger = logging.getLogger(__name__)


class FindingService:
    def __init__(self, db: DBSession) -> None:
        self._db = db
        self._findings = FindingRepository(db)
        self._engagements = EngagementService(db)

    # --- Creation (called by the pipeline, never by a route) -----------------

    def create_draft(self, engagement_id: uuid.UUID, draft: DraftFinding) -> Finding:
        """Persist a pipeline-produced draft.

        02_ARCHITECTURE.md §7.5: the worker writes Findings *through this
        service* rather than directly to the database, so the business rules
        live in one place regardless of who is calling.
        """
        return self._findings.create(
            engagement_id=engagement_id,
            scoped_requirement_id=draft.scoped_requirement_id,
            citations=draft.citations,
            ai_suggested_status=draft.suggested_status,
            ai_confidence=draft.confidence,
            ai_rationale=draft.rationale,
            needs_manual_review=draft.needs_manual_review,
        )

    # --- Reads ---------------------------------------------------------------

    def list_for_engagement(
        self,
        engagement_id: uuid.UUID,
        actor: Actor,
        *,
        status: FindingStatus | None = None,
        needs_manual_review: bool | None = None,
    ) -> list[Finding]:
        self._engagements.get(engagement_id, actor)
        return self._findings.list_for_engagement(
            engagement_id, actor, status=status, needs_manual_review=needs_manual_review
        )

    def get(self, finding_id: uuid.UUID, actor: Actor) -> Finding:
        finding = self._findings.get_scoped(finding_id, actor)
        if finding is not None:
            return finding
        if self._findings.exists_unscoped(finding_id):
            raise ForbiddenError("You are not assigned to this engagement.")
        raise NotFoundError("Finding not found.")

    def history(self, finding_id: uuid.UUID, actor: Actor) -> list[FindingHistory]:
        self.get(finding_id, actor)  # authorization first
        return self._findings.history_for(finding_id)

    # --- Review --------------------------------------------------------------

    def review(
        self,
        finding_id: uuid.UUID,
        actor: Actor,
        *,
        action: FindingAction,
        edited_status: ComplianceStatus | None = None,
        note: str | None = None,
    ) -> Finding:
        """Accept, edit, or reject a Finding.

        The whole method runs in one transaction: the Finding update and its
        FindingHistory row are committed together or not at all
        (03_DATA_MODEL.md §8.3). The caller commits; this method only flushes,
        so a failure anywhere leaves neither written.
        """
        finding = self.get(finding_id, actor)
        engagement = self._engagements.get(finding.engagement_id, actor)

        # 01_REQUIREMENTS.md § Finalization, Business Rules: once finalized, an
        # engagement's Findings are read-only.
        self._engagements.ensure_not_finalized(engagement)

        self._validate_action(action, edited_status, note)

        previous_status = finding.status
        previous_final = finding.final_status
        is_override = previous_status != FindingStatus.draft

        # 01_REQUIREMENTS.md: a Reviewer may override an Auditor's prior
        # accept/edit. An Auditor may not — otherwise two auditors could flip a
        # determination back and forth with no senior involvement.
        if is_override and actor.role != Role.reviewer:
            raise ForbiddenError(
                "This finding has already been reviewed. Only a Reviewer may change it."
            )

        new_status, new_final = self._resolve_target_state(finding, action, edited_status)

        finding.status = new_status
        finding.final_status = new_final
        # Server-derived, always. 04_API_CONTRACT.md: never accepted from the
        # request body.
        finding.reviewed_by = actor.id
        finding.reviewed_at = datetime.now(UTC)
        finding.review_note = note
        # The determination has been made by a human; the flag has served its
        # purpose and would otherwise keep the item in the "needs attention"
        # queue forever.
        finding.needs_manual_review = False

        # The AI's original suggestion is deliberately not touched: 01_REQUIREMENTS.md
        # requires it be retained for the audit trail even when a human overrides it.

        self._findings.add_history(
            finding_id=finding.id,
            actor_id=actor.id,
            action=FindingAction.override if is_override else action,
            previous_status=previous_status,
            new_status=new_status,
            previous_final_status=previous_final,
            new_final_status=new_final,
            note=note,
        )
        self._db.flush()

        log_finding_transition(
            actor_id=str(actor.id),
            finding_id=str(finding.id),
            action=action.value,
            previous_status=previous_status.value,
            new_status=new_status.value,
        )
        return finding

    @staticmethod
    def _validate_action(
        action: FindingAction, edited_status: ComplianceStatus | None, note: str | None
    ) -> None:
        """01_REQUIREMENTS.md § Finding Review, Failure Cases."""
        if action == FindingAction.override:
            # Not a client-supplied action: `override` is derived server-side
            # from the finding's prior state, so accepting it as input would let
            # a caller mislabel its own action in the audit trail.
            raise ValidationError("'override' is not a valid action; use accept, edit or reject.")
        if action == FindingAction.edit and edited_status is None:
            raise ValidationError("edited_status is required when action is 'edit'.")
        if action == FindingAction.reject and not (note or "").strip():
            # "a rejection must be explainable"
            raise ValidationError("note is required when action is 'reject'.")

    @staticmethod
    def _resolve_target_state(
        finding: Finding, action: FindingAction, edited_status: ComplianceStatus | None
    ) -> tuple[FindingStatus, ComplianceStatus | None]:
        """Compute the resulting state. 01_REQUIREMENTS.md § Processing Rules."""
        if action == FindingAction.accept:
            if finding.ai_suggested_status is None:
                # Accepting nothing is not a determination. When the LLM failed,
                # the human has to supply the status themselves via `edit`.
                raise ConflictError(
                    "This finding has no AI suggestion to accept. Use 'edit' to set a "
                    "status yourself.",
                    code="NO_AI_SUGGESTION",
                )
            return FindingStatus.approved, finding.ai_suggested_status

        if action == FindingAction.edit:
            # The human value overrides the AI value; the AI's stays on the row.
            return FindingStatus.approved, edited_status

        # reject: retained, never deleted, and excluded from the report.
        return FindingStatus.rejected, None
