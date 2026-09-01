"""Audit business logic.

02_ARCHITECTURE.md §7.4: business rules live here and nowhere else. In
particular the audit state machine is enforced at this layer so that a
future route, script, or worker cannot advance an audit by writing the
column directly.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session as DBSession

from app.errors import CODE_AUDIT_FINALIZED, ConflictError, NotFoundError, ValidationError
from app.models.audit import Audit
from app.models.enums import AuditStatus, Role
from app.repositories.audit import AuditRepository, ClientProfileDocumentRepository
from app.repositories.user import UserRepository
from app.schemas.audit import AuditCreate, AuditProfileUpdate

if TYPE_CHECKING:
    from app.api.deps import Actor

logger = logging.getLogger(__name__)

# 03_DATA_MODEL.md → Audit lifecycle: intake → scoping → in_progress →
# finalized, one-way, with `finalized` terminal. Encoded as data so that an
# illegal transition is impossible to express rather than merely discouraged.
_ALLOWED_TRANSITIONS: dict[AuditStatus, set[AuditStatus]] = {
    AuditStatus.intake: {AuditStatus.scoping},
    AuditStatus.scoping: {AuditStatus.in_progress},
    AuditStatus.in_progress: {AuditStatus.finalized},
    AuditStatus.finalized: set(),
}


class AuditService:
    def __init__(self, db: DBSession) -> None:
        self._db = db
        self._audits = AuditRepository(db)
        self._profile_documents = ClientProfileDocumentRepository(db)
        self._users = UserRepository(db)

    # --- Creation ------------------------------------------------------------

    def create(self, payload: AuditCreate, actor: Actor) -> Audit:
        """Create an audit and assign its creator.

        01_REQUIREMENTS.md § Audit Creation, Explicitly Forbidden Behavior:
        no outbound network call to any client-associated domain happens here.
        Nothing in this method performs I/O beyond the database — the
        scope-matching LLM call belongs to the next feature, not this one.
        """
        if payload.source_document_ids:
            unique_ids = list(dict.fromkeys(payload.source_document_ids))
            found = self._profile_documents.count_existing(unique_ids)
            if found != len(unique_ids):
                # The defensive check 04_API_CONTRACT.md describes. A 400 rather
                # than a 404 because the offending value is in the request body.
                raise ValidationError(
                    "One or more source_document_ids do not reference a known document."
                )

        audit = self._audits.create(
            client_name=payload.client_name,
            entity_type=payload.entity_type,
            merchant_level=payload.merchant_level,
            annual_transaction_volume=payload.annual_transaction_volume,
            existing_saq_type=payload.existing_saq_type,
            tech_stack_summary=payload.tech_stack_summary,
            company_profile=payload.company_profile.model_dump(mode="json", exclude_none=True),
            created_by=actor.id,
        )
        # "The creator is automatically the first assigned Auditor."
        # Without this the creator would immediately lose access to what they
        # just created, since visibility is assignment-based.
        self._audits.assign(audit.id, actor.id)
        return audit

    def update_profile(
        self, audit_id: uuid.UUID, payload: AuditProfileUpdate, actor: Actor
    ) -> Audit:
        """Correct an audit's company profile.

        The profile now drives mechanical scope exclusion, so a mistyped answer
        silently removes controls from the audit. Without an edit path the only
        remedy would be abandoning the audit and starting over, which is how
        people end up keeping a second copy of the truth in a spreadsheet.

        `exclude_none` is what preserves the unanswered/answered distinction: a
        field left out stays unanswered rather than being written as a null the
        applicability engine would have to interpret.
        """
        audit = self.get(audit_id, actor)
        self.ensure_not_finalized(audit)
        audit.company_profile = payload.company_profile.model_dump(mode="json", exclude_none=True)
        self._db.flush()
        logger.info("audit.profile_updated audit=%s actor=%s", audit_id, actor.id)
        return audit

    # --- Reads ---------------------------------------------------------------

    def get(self, audit_id: uuid.UUID, actor: Actor) -> Audit:
        """Fetch one audit, or raise 403/404 per the contract."""
        audit = self._audits.get_scoped(audit_id, actor)
        if audit is None:
            # Re-derives the correct status code without ever having read the row.
            self._audits._require_access(audit_id, actor, action="read_audit")
            raise NotFoundError("Audit not found.")
        if actor.is_admin:
            from app.logging_setup import log_admin_audit_access

            log_admin_audit_access(actor_id=str(actor.id), audit_id=str(audit.id))
        return audit

    def list_visible(
        self,
        actor: Actor,
        *,
        status: AuditStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Audit], int]:
        return self._audits.list_scoped(actor, status=status, limit=limit, offset=offset)

    def assigned_user_ids(self, audit_id: uuid.UUID) -> list[uuid.UUID]:
        return [a.user_id for a in self._audits.list_assignments(audit_id)]

    # --- State machine -------------------------------------------------------

    def advance_status(self, audit: Audit, target: AuditStatus) -> None:
        """Move an audit forward, or raise.

        Called by the scoping, evidence and finalization services rather than by
        routes, so every path through the workflow passes the same check.
        """
        if audit.status == target:
            return
        if target not in _ALLOWED_TRANSITIONS[audit.status]:
            raise ConflictError(
                f"An audit in '{audit.status.value}' cannot move to '{target.value}'.",
                code="INVALID_STATUS_TRANSITION",
                current_status=audit.status.value,
                requested_status=target.value,
            )
        self._audits.set_status(audit, target)

    def ensure_not_finalized(self, audit: Audit) -> None:
        """Guard for every mutation of audit-owned data.

        01_REQUIREMENTS.md § Finalization, Business Rules: once finalized, an
        audit's Findings become read-only and a correction requires a new,
        explicitly-labelled record rather than a silent edit.
        """
        if audit.is_finalized:
            raise ConflictError(
                "This audit is finalized and can no longer be modified.",
                code=CODE_AUDIT_FINALIZED,
            )

    # --- Assignments ---------------------------------------------------------

    def assign_user(self, audit_id: uuid.UUID, user_id: uuid.UUID, actor: Actor) -> object:
        """Reviewer/Admin only — enforced by the route's role gate, re-checked
        here so a non-route caller cannot bypass it."""
        if actor.role not in (Role.reviewer, Role.admin):
            from app.errors import ForbiddenError

            raise ForbiddenError("Only a Reviewer or Admin may change assignments.")

        audit = self.get(audit_id, actor)
        self.ensure_not_finalized(audit)

        target = self._users.get_by_id(user_id)
        if target is None or not target.is_active:
            raise NotFoundError("User not found.")

        if self._audits.get_assignment(audit_id, user_id) is not None:
            raise ConflictError(
                "That user is already assigned to this audit.",
                code="ALREADY_ASSIGNED",
            )
        return self._audits.assign(audit_id, user_id)

    def unassign_user(self, audit_id: uuid.UUID, user_id: uuid.UUID, actor: Actor) -> None:
        if actor.role not in (Role.reviewer, Role.admin):
            from app.errors import ForbiddenError

            raise ForbiddenError("Only a Reviewer or Admin may change assignments.")

        audit = self.get(audit_id, actor)
        self.ensure_not_finalized(audit)

        assignment = self._audits.get_assignment(audit_id, user_id)
        if assignment is None:
            raise NotFoundError("That user is not assigned to this audit.")
        self._audits.remove_assignment(assignment)
