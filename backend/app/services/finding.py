"""Finding review — the mandatory human-judgment checkpoint (TASK-106, TASK-110).

Two invariants live here, and both are enforced at the service layer rather than
in a route, because 02_ARCHITECTURE.md §7.4 requires them to hold for *every*
caller — a future route that forgets, a script, a background job:

1. **No Finding reaches `approved` without `reviewed_by` set** (ADR-003).
   `reviewed_by` is taken from the authenticated actor and never read from a
   request body.

2. **`system_result` is never overwritten.** The machine's verdict lives on the
   immutable `ControlEvaluation`; the human's lives in `Finding.auditor_decision`.
   This module writes only the second. There is no code path here that touches
   the first, which is what keeps "how often did the human disagree with the
   machine" answerable forever (01_REQUIREMENTS.md § Finding Review, Explicitly
   Forbidden Behavior).

An override — a decision differing from the evaluation's result — is always
permitted. Human authority is final. It is also always logged, because the
disagreement is a quality signal about the control definition, not something to
suppress.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session as DBSession

from app.errors import ForbiddenError, NotFoundError, ValidationError
from app.logging_setup import log_finding_transition
from app.models.enums import EvaluationResult, FindingAction, FindingStatus, Role
from app.models.evaluation import ControlEvaluation
from app.models.finding import Finding, FindingHistory
from app.repositories.finding import FindingRepository
from app.services.audit import AuditService

if TYPE_CHECKING:
    from app.api.deps import Actor

logger = logging.getLogger(__name__)

# Which status each action produces. Explicit table rather than branching logic,
# so the state machine is readable in one glance.
_ACTION_STATUS = {
    FindingAction.approve: FindingStatus.approved,
    FindingAction.reject: FindingStatus.rejected,
    FindingAction.request_more_evidence: FindingStatus.needs_more_evidence,
}


class FindingService:
    def __init__(self, db: DBSession) -> None:
        self._db = db
        self._findings = FindingRepository(db)
        self._audits = AuditService(db)

    # --- Creation (called by the pipeline, never by a route) -----------------

    def create_for_evaluation(
        self,
        audit_id: uuid.UUID,
        evaluation: ControlEvaluation,
        *,
        scoped_control_id: uuid.UUID | None = None,
        ai_explanation: str | None = None,
    ) -> Finding:
        """Wrap a gated ControlEvaluation in a reviewable Finding.

        Created in `pending_review` regardless of the evaluation's result or gate
        status — including a gate-REJECTED one, which is surfaced to the auditor
        as explicitly unverifiable rather than hidden (01_REQUIREMENTS.md
        § Evidence Gate, Failure Cases). Nothing in this system auto-approves.
        """
        return self._findings.create(
            audit_id=audit_id,
            control_evaluation_id=evaluation.id,
            scoped_control_id=scoped_control_id,
            ai_explanation=ai_explanation,
        )

    # --- Reads ---------------------------------------------------------------

    def list_for_audit(
        self,
        audit_id: uuid.UUID,
        actor: Actor,
        *,
        status: FindingStatus | None = None,
    ) -> list[Finding]:
        self._audits.get(audit_id, actor)
        return self._findings.list_for_audit(audit_id, actor, status=status)

    def get(self, finding_id: uuid.UUID, actor: Actor) -> Finding:
        finding = self._findings.get_scoped(finding_id, actor)
        if finding is not None:
            return finding
        if self._findings.exists_unscoped(finding_id):
            raise ForbiddenError("You are not assigned to this audit.")
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
        auditor_decision: EvaluationResult | None = None,
        note: str | None = None,
    ) -> Finding:
        """Record a human decision on a Finding.

        The whole method runs in one transaction: the Finding update and its
        FindingHistory row are committed together or not at all
        (03_DATA_MODEL.md §8.3). The caller commits; this method only flushes,
        so a failure anywhere leaves neither written.
        """
        finding = self.get(finding_id, actor)
        audit = self._audits.get(finding.audit_id, actor)

        # 01_REQUIREMENTS.md § Finalization, Business Rules: once finalized, an
        # audit's Findings are read-only.
        self._audits.ensure_not_finalized(audit)

        system_result = finding.evaluation.result if finding.evaluation else None
        decision = self._resolve_decision(action, auditor_decision, system_result)
        self._validate(action, decision, system_result, note)

        previous_status = finding.status
        previous_decision = finding.auditor_decision
        is_rereview = previous_status != FindingStatus.pending_review

        # A Reviewer may revisit an Auditor's decision. An Auditor may not —
        # otherwise two auditors could flip a determination back and forth with
        # no senior involvement.
        if is_rereview and actor.role != Role.reviewer:
            raise ForbiddenError(
                "This finding has already been reviewed. Only a Reviewer may change it."
            )

        finding.status = _ACTION_STATUS[action]
        finding.auditor_decision = decision
        # Server-derived, always. 04_API_CONTRACT.md: never accepted from the
        # request body.
        finding.reviewed_by = actor.id
        finding.reviewed_at = datetime.now(UTC)
        finding.review_note = note

        # `finding.evaluation.result` is deliberately untouched. There is no
        # assignment to it anywhere in this file, and that is the point.

        self._findings.add_history(
            finding_id=finding.id,
            actor_id=actor.id,
            action=FindingAction.override if is_rereview else action,
            previous_status=previous_status,
            new_status=finding.status,
            previous_decision=previous_decision,
            new_decision=decision,
            system_result=system_result,
            note=note,
        )
        self._db.flush()

        log_finding_transition(
            actor_id=str(actor.id),
            finding_id=str(finding.id),
            action=action.value,
            previous_status=previous_status.value,
            new_status=finding.status.value,
        )
        if decision is not None and system_result is not None and decision != system_result:
            # 02_ARCHITECTURE.md §7.8: the override rate is a product-quality
            # metric about the control definitions, logged distinctly so it can
            # actually be measured.
            logger.info(
                "finding.override finding=%s system_result=%s auditor_decision=%s",
                finding.id,
                system_result.value,
                decision.value,
            )
        return finding

    @staticmethod
    def _resolve_decision(
        action: FindingAction,
        auditor_decision: EvaluationResult | None,
        system_result: EvaluationResult | None,
    ) -> EvaluationResult | None:
        """Work out the six-state value this action records.

        Approving without naming a value means "I agree with the machine", so
        the system result is copied across into the human's column — copied,
        not aliased, so the two remain independently readable afterwards.
        """
        if action == FindingAction.approve:
            return auditor_decision if auditor_decision is not None else system_result
        # A rejection or a request for more evidence is not a compliance
        # determination, so no decision value is recorded unless one was given.
        return auditor_decision

    @staticmethod
    def _validate(
        action: FindingAction,
        decision: EvaluationResult | None,
        system_result: EvaluationResult | None,
        note: str | None,
    ) -> None:
        """01_REQUIREMENTS.md § Finding Review, Failure Cases and
        04_API_CONTRACT.md → PATCH /api/findings/{id}/review, Validation Rules."""
        if action == FindingAction.override:
            # Not a client-supplied action: `override` is derived server-side
            # from the finding's prior state, so accepting it as input would let
            # a caller mislabel its own action in the audit trail.
            raise ValidationError(
                "'override' is not a valid action; use approve, reject or request_more_evidence."
            )

        has_note = bool((note or "").strip())

        if action in (FindingAction.reject, FindingAction.request_more_evidence) and not has_note:
            raise ValidationError(f"note is required when action is '{action.value}'.")

        if action == FindingAction.approve:
            if decision is None:
                raise ValidationError(
                    "This finding has no system result to approve. Supply an "
                    "auditor_decision explicitly."
                )
            # An override is always allowed, and always has to be explained —
            # the justification is what makes the disagreement reviewable later.
            if system_result is not None and decision != system_result and not has_note:
                raise ValidationError(
                    "note is required when your decision differs from the system result."
                )
