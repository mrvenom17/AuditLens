"""Scope-matching business logic (TASK-013, TASK-014).

01_REQUIREMENTS.md § PCI DSS Scope Matching. The single most important rule
here, from that document's Explicitly Forbidden Behavior section: the system
must never mark a ScopedRequirement `confirmed = true` without an explicit human
action. Every suggestion this service writes is `confirmed=False`, and the only
code path that sets it true is `confirm()`, which takes an actor.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple

from sqlalchemy.orm import Session as DBSession

from app.config.settings import settings
from app.errors import (
    CODE_MISSING_PROFILE_FIELDS,
    CODE_RATE_LIMITED,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.models.corpus import PCIRequirement
from app.models.engagement import Engagement
from app.models.enums import EngagementStatus, EntityType, Role, ScopeSource
from app.models.scoping import ScopedRequirement
from app.pipelines.llm import LLMError, get_llm_client
from app.repositories.scoping import CorpusRepository, ScopedRequirementRepository
from app.services.engagement import EngagementService

if TYPE_CHECKING:
    from app.api.deps import Actor

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are assisting a PCI DSS v4.0.1 assessment team at an audit firm.

Given a client profile, propose which PCI DSS requirements are in scope and which \
SAQ type applies. You are producing a DRAFT for a qualified human assessor to \
review, edit, and approve. You are not making a compliance determination.

Rules:
- Only propose clause IDs that appear in the provided corpus list. Never invent one.
- When the entity type is ambiguous, propose the broader and stricter scope rather \
than guessing narrow, and say so in the rationale.
- Give a short, plain-language rationale for each major inclusion or exclusion.

Respond with JSON only, in exactly this shape:
{"saq_type": "A|A-EP|B|B-IP|C|C-VT|D|D-SP|not_applicable",
 "ambiguous_entity_type": true|false,
 "requirements": [{"clause_id": "1.2.1", "rationale": "why this applies"}]}"""


class _ParsedSuggestion(NamedTuple):
    """The model's reply after shape validation, before it touches the database."""

    saq_type: str | None
    ambiguous: bool
    requirements: list[Any]


class ScopeSuggestion(NamedTuple):
    """The result of a suggestion attempt.

    `manual_scoping_required` is the degraded outcome, not an error: the endpoint
    returns 200 with an empty proposal when the LLM is unavailable
    (04_API_CONTRACT.md, 01_REQUIREMENTS.md acceptance criteria).
    """

    proposed: list[ScopedRequirement]
    manual_scoping_required: bool
    saq_type: str | None
    ambiguous_entity_type: bool


class ScopingService:
    def __init__(self, db: DBSession) -> None:
        self._db = db
        self._scoped = ScopedRequirementRepository(db)
        self._corpus = CorpusRepository(db)
        self._engagements = EngagementService(db)

    # --- Suggestion ----------------------------------------------------------

    def suggest_scope(self, engagement_id: uuid.UUID, actor: Actor) -> ScopeSuggestion:
        engagement = self._engagements.get(engagement_id, actor)
        self._engagements.ensure_not_finalized(engagement)
        self._require_profile_fields(engagement)
        self._enforce_rate_limit(actor)

        version = self._corpus.current_version()
        if version is None:
            # Not an LLM failure — the corpus has never been loaded. That is an
            # operational fault the auditor cannot work around by scoping
            # manually, so it is a real error rather than a degraded success.
            raise ConflictError(
                "The PCI DSS corpus has not been loaded on this system.",
                code="CORPUS_NOT_LOADED",
            )
        corpus = self._corpus.list_by_version(version)

        try:
            parsed = self._call_llm(engagement, corpus)
        except LLMError as exc:
            # 01_REQUIREMENTS.md: the engagement stays in `intake` and the
            # response tells the auditor to scope manually. Never a 500.
            logger.warning("Scope suggestion degraded to manual: %s", type(exc).__name__)
            return ScopeSuggestion(
                proposed=[],
                manual_scoping_required=True,
                saq_type=None,
                ambiguous_entity_type=False,
            )

        proposed = self._persist_suggestions(engagement, corpus, parsed.requirements)

        if proposed:
            # A successful suggestion moves intake → scoping. A degraded run
            # above returns before this line, leaving the engagement in intake.
            self._engagements.advance_status(engagement, EngagementStatus.scoping)

        return ScopeSuggestion(
            proposed=proposed,
            manual_scoping_required=False,
            saq_type=parsed.saq_type,
            ambiguous_entity_type=parsed.ambiguous,
        )

    def _require_profile_fields(self, engagement: Engagement) -> None:
        """04_API_CONTRACT.md: 409 MISSING_PROFILE_FIELDS."""
        missing: list[str] = []
        if engagement.entity_type is None:
            missing.append("entity_type")
        if engagement.entity_type == EntityType.merchant and engagement.merchant_level is None:
            missing.append("merchant_level")
        if missing:
            raise ConflictError(
                "This engagement is missing profile fields required for scoping.",
                code=CODE_MISSING_PROFILE_FIELDS,
                missing_fields=missing,
            )

    def _enforce_rate_limit(self, actor: Actor) -> None:
        """04_API_CONTRACT.md: capped per user to prevent runaway LLM cost.

        Counted from ScopedRequirement rows this actor's suggestions produced
        would be wrong (a failed call produces none), so the limiter counts
        suggestion *events*. With no separate events table, the cheapest honest
        proxy is the number of distinct engagements this actor has suggested
        against in the window, tracked in-process.
        """
        now = datetime.now(UTC)
        window_start = now - timedelta(hours=1)
        recent = [t for t in _suggestion_calls.get(actor.id, []) if t > window_start]
        if len(recent) >= settings.SCOPE_SUGGESTION_PER_HOUR:
            retry_after = int((recent[0] + timedelta(hours=1) - now).total_seconds())
            raise ConflictError(
                "Scope suggestion has been requested too many times in the last hour.",
                code=CODE_RATE_LIMITED,
                retry_after=max(1, retry_after),
            )
        recent.append(now)
        _suggestion_calls[actor.id] = recent

    def _call_llm(self, engagement: Engagement, corpus: list[PCIRequirement]) -> _ParsedSuggestion:
        """Build the prompt and parse the reply.

        05_SECURITY.md and TASK-013: only structured profile fields are sent at
        this step — there is no evidence content in the engagement yet, and this
        method deliberately reads no evidence table so that stays true.
        """
        clause_catalogue = "\n".join(
            f"{r.clause_id} [family {r.requirement_family}] {r.title}" for r in corpus
        )
        profile = {
            "entity_type": engagement.entity_type.value,
            "merchant_level": engagement.merchant_level.value
            if engagement.merchant_level
            else None,
            "annual_transaction_volume": engagement.annual_transaction_volume,
            "existing_saq_type": engagement.existing_saq_type,
            "tech_stack_summary": engagement.tech_stack_summary,
        }
        prompt = (
            "Client profile:\n"
            + "\n".join(f"- {k}: {v}" for k, v in profile.items() if v is not None)
            + "\n\nAvailable PCI DSS v4.0.1 clauses:\n"
            + clause_catalogue
        )

        response = get_llm_client().complete(
            system=_SYSTEM_PROMPT,
            prompt=prompt,
            timeout=settings.LLM_INTERACTIVE_TIMEOUT_SECONDS,
            max_tokens=4096,
        )
        payload = response.as_json()
        if not isinstance(payload, dict) or not isinstance(payload.get("requirements"), list):
            raise LLMError("Scope suggestion response had an unexpected shape.")

        saq_type = payload.get("saq_type")
        return _ParsedSuggestion(
            saq_type=str(saq_type) if saq_type is not None else None,
            ambiguous=bool(payload.get("ambiguous_entity_type", False)),
            requirements=payload["requirements"],
        )

    def _persist_suggestions(
        self,
        engagement: Engagement,
        corpus: list[PCIRequirement],
        raw_requirements: list[Any],
    ) -> list[ScopedRequirement]:
        # Replace prior unconfirmed AI suggestions; confirmed rows are untouched.
        self._scoped.clear_unconfirmed_suggestions(engagement.id)
        already_present = self._scoped.existing_requirement_ids(engagement.id)
        by_clause = {r.clause_id: r for r in corpus}

        proposed: list[ScopedRequirement] = []
        for item in raw_requirements:
            if not isinstance(item, dict):
                continue
            clause_id = str(item.get("clause_id", "")).strip()
            requirement = by_clause.get(clause_id)
            if requirement is None:
                # A hallucinated clause id is dropped rather than stored. The
                # scope must only ever reference real corpus rows.
                logger.info("Dropped suggested clause not present in corpus: %s", clause_id)
                continue
            if requirement.id in already_present:
                # Already scoped (manually, or confirmed from a previous run).
                continue

            rationale = str(item.get("rationale", "")).strip() or None
            proposed.append(
                self._scoped.create(
                    engagement_id=engagement.id,
                    pci_requirement_id=requirement.id,
                    source=ScopeSource.ai_suggested,
                    rationale=rationale,
                    confirmed=False,  # never auto-confirmed
                )
            )
            already_present.add(requirement.id)
        return proposed

    # --- Human actions -------------------------------------------------------

    def list_scope(
        self, engagement_id: uuid.UUID, actor: Actor, *, confirmed_only: bool = False
    ) -> list[ScopedRequirement]:
        self._engagements.get(engagement_id, actor)
        return self._scoped.list_for_engagement(engagement_id, actor, confirmed_only=confirmed_only)

    def add_manual(
        self, engagement_id: uuid.UUID, clause_id: str, actor: Actor, rationale: str | None
    ) -> ScopedRequirement:
        """The auditor adds a clause the AI did not propose.

        01_REQUIREMENTS.md: "the auditor ... can add/remove rows (source =
        manual for anything the auditor added directly)". Added rows are still
        created unconfirmed — adding is not confirming.
        """
        engagement = self._engagements.get(engagement_id, actor)
        self._engagements.ensure_not_finalized(engagement)

        version = self._corpus.current_version()
        if version is None:
            raise ConflictError("The PCI DSS corpus has not been loaded.", code="CORPUS_NOT_LOADED")

        matches = self._corpus.get_by_clause_ids([clause_id], version)
        if not matches:
            raise ValidationError(f"No PCI DSS clause '{clause_id}' in corpus {version}.")

        if matches[0].id in self._scoped.existing_requirement_ids(engagement_id):
            raise ConflictError(
                f"Clause {clause_id} is already in this engagement's scope.",
                code="ALREADY_SCOPED",
            )

        # Adding a requirement means scoping has begun, whoever proposed it.
        # Without this, an engagement scoped entirely by hand — the documented
        # path when the LLM is unavailable — would sit in `intake` forever and
        # could never be finalized, because `intake` has no edge to
        # `in_progress`. Only a *successful* AI suggestion used to advance it.
        if engagement.status == EngagementStatus.intake:
            self._engagements.advance_status(engagement, EngagementStatus.scoping)

        return self._scoped.create(
            engagement_id=engagement_id,
            pci_requirement_id=matches[0].id,
            source=ScopeSource.manual,
            rationale=rationale,
            confirmed=False,
        )

    def confirm(self, scoped_id: uuid.UUID, actor: Actor, *, confirmed: bool) -> ScopedRequirement:
        """The human confirmation gate (04_API_CONTRACT.md → PATCH).

        One row, one call, one audit-trail event. 04_API_CONTRACT.md is explicit
        that a bulk UI action is fine but must still write one confirmation per
        row, which is why no bulk endpoint exists.
        """
        scoped = self._get_scoped_or_raise(scoped_id, actor)
        engagement = self._engagements.get(scoped.engagement_id, actor)
        self._engagements.ensure_not_finalized(engagement)

        scoped.confirmed = confirmed
        self._db.flush()

        if confirmed:
            # Walk the documented lifecycle forward rather than adding a
            # shortcut edge to the state machine. The `intake` step is reachable
            # here for a requirement created before scoping was marked as begun;
            # confirming one unambiguously means the engagement is past intake.
            if engagement.status == EngagementStatus.intake:
                self._engagements.advance_status(engagement, EngagementStatus.scoping)
            if engagement.status == EngagementStatus.scoping:
                self._engagements.advance_status(engagement, EngagementStatus.in_progress)
        return scoped

    def acknowledge_gap(
        self, scoped_id: uuid.UUID, actor: Actor, *, acknowledged: bool, note: str | None
    ) -> ScopedRequirement:
        """Reviewer-only (ADR-012).

        This flag is what permits finalizing a confirmed requirement with no
        approved Finding, so it carries the same authority as finalization
        itself and is restricted the same way.
        """
        if actor.role != Role.reviewer:
            raise ForbiddenError("Only a Reviewer may acknowledge a scope gap.")

        if acknowledged and not (note or "").strip():
            raise ValidationError("gap_note is required when acknowledging a gap.")

        scoped = self._get_scoped_or_raise(scoped_id, actor)
        engagement = self._engagements.get(scoped.engagement_id, actor)
        self._engagements.ensure_not_finalized(engagement)

        scoped.gap_acknowledged = acknowledged
        scoped.gap_note = note.strip() if acknowledged and note else None
        self._db.flush()
        return scoped

    def _get_scoped_or_raise(self, scoped_id: uuid.UUID, actor: Actor) -> ScopedRequirement:
        scoped = self._scoped.get_scoped(scoped_id, actor)
        if scoped is not None:
            return scoped
        if self._scoped.exists_unscoped(scoped_id):
            raise ForbiddenError("You are not assigned to this engagement.")
        raise NotFoundError("Scoped requirement not found.")

    def count_confirmed(self, engagement_id: uuid.UUID) -> int:
        return self._scoped.count_confirmed(engagement_id)


# Per-user, in-process record of interactive scope-suggestion calls.
# ponytail: in-process dict, so the cap is per API worker rather than global.
# That is adequate for the documented single-server deployment
# (02_ARCHITECTURE.md §7.9 — one application server, no horizontal scaling at
# this stage). Move to a `scope_suggestion_calls` table if a second API replica
# is ever run, since the cap exists to bound spend and a per-replica cap would
# multiply by the replica count.
_suggestion_calls: dict[uuid.UUID, list[datetime]] = {}


def reset_rate_limits() -> None:
    """Clear the in-process limiter. Used by tests."""
    _suggestion_calls.clear()
