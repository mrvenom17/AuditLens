"""Engagement business logic.

02_ARCHITECTURE.md §7.4: business rules live here and nowhere else. In
particular the engagement state machine is enforced at this layer so that a
future route, script, or worker cannot advance an engagement by writing the
column directly.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session as DBSession

from app.errors import CODE_ENGAGEMENT_FINALIZED, ConflictError, NotFoundError, ValidationError
from app.models.engagement import Engagement
from app.models.enums import EngagementStatus, Role
from app.repositories.engagement import ClientProfileDocumentRepository, EngagementRepository
from app.repositories.user import UserRepository
from app.schemas.engagement import EngagementCreate

if TYPE_CHECKING:
    from app.api.deps import Actor

# 03_DATA_MODEL.md → Engagement lifecycle: intake → scoping → in_progress →
# finalized, one-way, with `finalized` terminal. Encoded as data so that an
# illegal transition is impossible to express rather than merely discouraged.
_ALLOWED_TRANSITIONS: dict[EngagementStatus, set[EngagementStatus]] = {
    EngagementStatus.intake: {EngagementStatus.scoping},
    EngagementStatus.scoping: {EngagementStatus.in_progress},
    EngagementStatus.in_progress: {EngagementStatus.finalized},
    EngagementStatus.finalized: set(),
}


class EngagementService:
    def __init__(self, db: DBSession) -> None:
        self._db = db
        self._engagements = EngagementRepository(db)
        self._profile_documents = ClientProfileDocumentRepository(db)
        self._users = UserRepository(db)

    # --- Creation ------------------------------------------------------------

    def create(self, payload: EngagementCreate, actor: Actor) -> Engagement:
        """Create an engagement and assign its creator.

        01_REQUIREMENTS.md § Engagement Creation, Explicitly Forbidden Behavior:
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

        engagement = self._engagements.create(
            client_name=payload.client_name,
            entity_type=payload.entity_type,
            merchant_level=payload.merchant_level,
            annual_transaction_volume=payload.annual_transaction_volume,
            existing_saq_type=payload.existing_saq_type,
            tech_stack_summary=payload.tech_stack_summary,
            created_by=actor.id,
        )
        # "The creator is automatically the first assigned Auditor."
        # Without this the creator would immediately lose access to what they
        # just created, since visibility is assignment-based.
        self._engagements.assign(engagement.id, actor.id)
        return engagement

    # --- Reads ---------------------------------------------------------------

    def get(self, engagement_id: uuid.UUID, actor: Actor) -> Engagement:
        """Fetch one engagement, or raise 403/404 per the contract."""
        engagement = self._engagements.get_scoped(engagement_id, actor)
        if engagement is None:
            # Re-derives the correct status code without ever having read the row.
            self._engagements._require_access(engagement_id, actor, action="read_engagement")
            raise NotFoundError("Engagement not found.")
        if actor.is_admin:
            from app.logging_setup import log_admin_engagement_access

            log_admin_engagement_access(actor_id=str(actor.id), engagement_id=str(engagement.id))
        return engagement

    def list_visible(
        self,
        actor: Actor,
        *,
        status: EngagementStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Engagement], int]:
        return self._engagements.list_scoped(actor, status=status, limit=limit, offset=offset)

    def assigned_user_ids(self, engagement_id: uuid.UUID) -> list[uuid.UUID]:
        return [a.user_id for a in self._engagements.list_assignments(engagement_id)]

    # --- State machine -------------------------------------------------------

    def advance_status(self, engagement: Engagement, target: EngagementStatus) -> None:
        """Move an engagement forward, or raise.

        Called by the scoping, evidence and finalization services rather than by
        routes, so every path through the workflow passes the same check.
        """
        if engagement.status == target:
            return
        if target not in _ALLOWED_TRANSITIONS[engagement.status]:
            raise ConflictError(
                f"An engagement in '{engagement.status.value}' cannot move to '{target.value}'.",
                code="INVALID_STATUS_TRANSITION",
                current_status=engagement.status.value,
                requested_status=target.value,
            )
        self._engagements.set_status(engagement, target)

    def ensure_not_finalized(self, engagement: Engagement) -> None:
        """Guard for every mutation of engagement-owned data.

        01_REQUIREMENTS.md § Finalization, Business Rules: once finalized, an
        engagement's Findings become read-only and a correction requires a new,
        explicitly-labelled record rather than a silent edit.
        """
        if engagement.is_finalized:
            raise ConflictError(
                "This engagement is finalized and can no longer be modified.",
                code=CODE_ENGAGEMENT_FINALIZED,
            )

    # --- Assignments ---------------------------------------------------------

    def assign_user(self, engagement_id: uuid.UUID, user_id: uuid.UUID, actor: Actor) -> object:
        """Reviewer/Admin only — enforced by the route's role gate, re-checked
        here so a non-route caller cannot bypass it."""
        if actor.role not in (Role.reviewer, Role.admin):
            from app.errors import ForbiddenError

            raise ForbiddenError("Only a Reviewer or Admin may change assignments.")

        engagement = self.get(engagement_id, actor)
        self.ensure_not_finalized(engagement)

        target = self._users.get_by_id(user_id)
        if target is None or not target.is_active:
            raise NotFoundError("User not found.")

        if self._engagements.get_assignment(engagement_id, user_id) is not None:
            raise ConflictError(
                "That user is already assigned to this engagement.",
                code="ALREADY_ASSIGNED",
            )
        return self._engagements.assign(engagement_id, user_id)

    def unassign_user(self, engagement_id: uuid.UUID, user_id: uuid.UUID, actor: Actor) -> None:
        if actor.role not in (Role.reviewer, Role.admin):
            from app.errors import ForbiddenError

            raise ForbiddenError("Only a Reviewer or Admin may change assignments.")

        engagement = self.get(engagement_id, actor)
        self.ensure_not_finalized(engagement)

        assignment = self._engagements.get_assignment(engagement_id, user_id)
        if assignment is None:
            raise NotFoundError("That user is not assigned to this engagement.")
        self._engagements.remove_assignment(assignment)
