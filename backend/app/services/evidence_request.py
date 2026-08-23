"""Evidence-request generation (TASK-015).

01_REQUIREMENTS.md § Evidence Request Generation. Two rules dominate this
module:

* **Draft only.** ADR-004: the system produces a checklist. It never sends an
  email, message, or any external communication. Nothing in this file performs
  network I/O except the LLM call that drafts wording.
* **This feature must never fail outright.** If the LLM is unavailable, the
  descriptions fall back to a template built from the clause text. A degraded
  checklist is still a usable checklist; no checklist is a blocked auditor.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.config.settings import settings
from app.errors import CODE_NO_CONFIRMED_SCOPE, ConflictError, NotFoundError
from app.models.enums import EvidenceRequestStatus
from app.models.scoping import EvidenceRequest, ScopedRequirement
from app.pipelines.llm import LLMError, get_llm_client
from app.repositories.scoping import EvidenceRequestRepository, ScopedRequirementRepository
from app.services.engagement import EngagementService

if TYPE_CHECKING:
    from app.api.deps import Actor

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You draft evidence-request checklists for a PCI DSS v4.0.1 assessment team.

For each requirement given, write one short, plain-language request describing \
what artifact the client should provide. Address it to a non-specialist at the \
client, not to an assessor. Name the artifact concretely (a configuration \
export, a policy document, a screenshot of a specific screen) rather than \
restating the clause.

Do not include the clause number in the text; it is stored separately.
Do not write a greeting, a signature, or any covering message.

Respond with JSON only: {"requests": [{"clause_id": "1.2.1", "description": "..."}]}"""


class GeneratedRequests(NamedTuple):
    created: list[EvidenceRequest]
    skipped_already_requested: int
    llm_available: bool


class EvidenceRequestService:
    def __init__(self, db: DBSession) -> None:
        self._db = db
        self._requests = EvidenceRequestRepository(db)
        self._scoped = ScopedRequirementRepository(db)
        self._engagements = EngagementService(db)

    def generate(self, engagement_id: uuid.UUID, actor: Actor) -> GeneratedRequests:
        engagement = self._engagements.get(engagement_id, actor)
        self._engagements.ensure_not_finalized(engagement)

        confirmed = self._scoped.list_for_engagement(engagement_id, actor, confirmed_only=True)
        if not confirmed:
            # 04_API_CONTRACT.md: 409 NO_CONFIRMED_SCOPE, with guidance to
            # complete scoping first.
            raise ConflictError(
                "This engagement has no confirmed scope yet. Confirm at least one "
                "requirement before generating an evidence checklist.",
                code=CODE_NO_CONFIRMED_SCOPE,
            )

        # 01_REQUIREMENTS.md Edge Cases: re-running only drafts requests for
        # genuinely still-missing items and does not duplicate existing ones.
        already_requested = self._requests.scoped_requirement_ids_with_requests(engagement_id)
        outstanding = [s for s in confirmed if s.id not in already_requested]
        skipped = len(confirmed) - len(outstanding)

        if not outstanding:
            return GeneratedRequests(
                created=[], skipped_already_requested=skipped, llm_available=True
            )

        descriptions, llm_available = self._draft_descriptions(outstanding)

        created = [
            self._requests.create(
                engagement_id=engagement_id,
                scoped_requirement_id=scoped.id,
                description=descriptions[scoped.id],
                description_source="llm" if llm_available else "template",
            )
            for scoped in outstanding
        ]
        return GeneratedRequests(
            created=created, skipped_already_requested=skipped, llm_available=llm_available
        )

    def _draft_descriptions(
        self, outstanding: list[ScopedRequirement]
    ) -> tuple[dict[uuid.UUID, str], bool]:
        """Return a description per requirement, and whether the LLM supplied them.

        The template fallback is not a placeholder to be filled in later — it is
        the specified behaviour when the LLM is unavailable, and it produces a
        genuinely usable request from the clause text the corpus already holds.
        """
        fallback = {s.id: _template_description(s) for s in outstanding}

        catalogue = "\n".join(
            f"{s.requirement.clause_id}: {s.requirement.title} — {s.requirement.full_text}"
            for s in outstanding
        )
        try:
            response = get_llm_client().complete(
                system=_SYSTEM_PROMPT,
                prompt=f"Requirements needing evidence:\n{catalogue}",
                timeout=settings.LLM_BACKGROUND_TIMEOUT_SECONDS,
                max_tokens=4096,
            )
            payload = response.as_json()
        except LLMError as exc:
            logger.warning(
                "Evidence-request drafting fell back to templates: %s", type(exc).__name__
            )
            return fallback, False

        if not isinstance(payload, dict) or not isinstance(payload.get("requests"), list):
            logger.warning(
                "Evidence-request drafting returned an unexpected shape; using templates"
            )
            return fallback, False

        by_clause = {s.requirement.clause_id: s.id for s in outstanding}
        drafted = dict(fallback)
        for item in payload["requests"]:
            if not isinstance(item, dict):
                continue
            scoped_id = by_clause.get(str(item.get("clause_id", "")).strip())
            description = str(item.get("description", "")).strip()
            if scoped_id and description:
                drafted[scoped_id] = description
        return drafted, True

    # --- Reads and edits -----------------------------------------------------

    def list_for_engagement(self, engagement_id: uuid.UUID, actor: Actor) -> list[EvidenceRequest]:
        self._engagements.get(engagement_id, actor)
        return self._requests.list_for_engagement(engagement_id, actor)

    def update(
        self,
        request_id: uuid.UUID,
        actor: Actor,
        *,
        description: str | None,
        status: EvidenceRequestStatus | None,
    ) -> EvidenceRequest:
        """Edit a draft request, or record the auditor's own note about it.

        `status = sent_externally` is a note-to-self: the system does not verify
        that anything was actually sent, and says so in 03_DATA_MODEL.md so no
        future reader assumes otherwise (ADR-004).
        """
        request = self._requests.get_scoped(request_id, actor)
        if request is None:
            raise NotFoundError("Evidence request not found.")

        engagement = self._engagements.get(request.engagement_id, actor)
        self._engagements.ensure_not_finalized(engagement)

        if description is not None:
            request.description = description
        if status is not None:
            request.status = status
        self._db.flush()
        return request

    def clause_ids_for(self, requests: list[EvidenceRequest]) -> dict[uuid.UUID, str]:
        """Resolve each request's clause id for the response shape."""
        if not requests:
            return {}
        scoped_ids = {r.scoped_requirement_id for r in requests}
        rows = self._db.scalars(
            select(ScopedRequirement).where(ScopedRequirement.id.in_(scoped_ids))
        ).all()
        return {row.id: row.requirement.clause_id for row in rows}


def _template_description(scoped: ScopedRequirement) -> str:
    """The non-LLM description. Uses the corpus text the engagement is already
    scoped against, so it is specific to the clause rather than generic."""
    clause = scoped.requirement
    return (
        f"Please provide documentation evidencing compliance with PCI DSS "
        f"requirement {clause.clause_id} ({clause.title}). {clause.full_text}"
    )
