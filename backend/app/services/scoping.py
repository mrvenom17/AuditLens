"""Scope-matching business logic (TASK-013, TASK-014).

01_REQUIREMENTS.md § PCI DSS Scope Matching. The single most important rule
here, from that document's Explicitly Forbidden Behavior section: the system
must never mark a ScopedControl `confirmed = true` without an explicit human
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
from app.models.audit import Audit
from app.models.corpus import ControlDefinition
from app.models.enums import ApplicabilityStatus, AuditStatus, EntityType, Role, ScopeSource
from app.models.scoping import ScopedControl
from app.pipelines.llm import LLMError, get_llm_client
from app.repositories.scoping import CorpusRepository, ScopedRequirementRepository
from app.services import applicability
from app.services.applicability import ApplicabilityOutcome
from app.services.audit import AuditService

if TYPE_CHECKING:
    from app.api.deps import Actor

logger = logging.getLogger(__name__)


def _applicability_rationale(outcome: ApplicabilityOutcome) -> str:
    """Plain-English reason for a determination, stored on the row.

    An auditor asking "why is 12.9.1 not in scope?" should get the condition that
    excluded it, not a bare status.
    """
    if outcome.status is ApplicabilityStatus.NOT_APPLICABLE:
        unmet = [e["detail"] for e in outcome.evidence if e.get("result") == "FAIL"]
        return "Not applicable: " + ("; ".join(unmet) or "a condition did not hold.")
    if outcome.status is ApplicabilityStatus.UNDETERMINED:
        return (
            "Applicability could not be determined — the company profile does not "
            "answer: " + ", ".join(outcome.unanswered or ["(unknown attribute)"])
        )
    return "Applicable: every condition on this control holds for this company."


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
 "requirements": [{"control_id": "1.2.1", "rationale": "why this applies"}]}"""


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

    proposed: list[ScopedControl]
    manual_scoping_required: bool
    saq_type: str | None
    ambiguous_entity_type: bool


class ScopingService:
    def __init__(self, db: DBSession) -> None:
        self._db = db
        self._scoped = ScopedRequirementRepository(db)
        self._corpus = CorpusRepository(db)
        self._audits = AuditService(db)

    # --- Suggestion ----------------------------------------------------------

    def suggest_scope(self, audit_id: uuid.UUID, actor: Actor) -> ScopeSuggestion:
        """Scope an audit: deterministic first, model second.

        The applicability engine runs over the whole corpus and decides what it
        can decide. Only then is the LLM consulted, and only as an advisory pass
        over what the engine left open — its proposals are filtered against the
        deterministic verdict, never allowed to override it.

        Note the rate limiter no longer guards this whole method. It caps LLM
        cost, and mechanical scoping costs nothing; gating it behind an LLM quota
        would lock an auditor out of the deterministic path for the rest of the
        hour.
        """
        audit = self._audits.get(audit_id, actor)
        self._audits.ensure_not_finalized(audit)
        self._require_profile_fields(audit)

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

        # --- 1. Deterministic pass. No model, no network, no quota. ---------
        determinations = self.apply_applicability(audit, corpus)
        excluded = {
            control_id
            for control_id, outcome in determinations.items()
            if outcome.status is ApplicabilityStatus.NOT_APPLICABLE
        }

        # --- 2. Advisory pass over what the engine could not settle ---------
        degraded = False
        parsed = None
        try:
            self._enforce_rate_limit(actor)
            parsed = self._call_llm(audit, corpus)
        except ConflictError:
            # Rate-limited. The deterministic work above already happened and is
            # persisted, so this degrades rather than failing the request.
            logger.info("Scope suggestion rate-limited; deterministic scope stands")
            degraded = True
        except LLMError as exc:
            # 01_REQUIREMENTS.md: the audit stays usable and the response tells
            # the auditor to scope the remainder manually. Never a 500.
            logger.warning("Scope suggestion degraded to manual: %s", type(exc).__name__)
            degraded = True

        proposed: list[ScopedControl] = [
            row
            for row in self._scoped.list_for_audit(audit.id, actor)
            if row.applicability_status is ApplicabilityStatus.IN_SCOPE
            and row.source is ScopeSource.deterministic
        ]

        if parsed is not None:
            proposed += self._persist_suggestions(
                audit, corpus, parsed.requirements, excluded=excluded
            )

        if proposed:
            # Any successful scoping moves intake → scoping.
            self._audits.advance_status(audit, AuditStatus.scoping)

        return ScopeSuggestion(
            proposed=proposed,
            manual_scoping_required=degraded,
            saq_type=parsed.saq_type if parsed else None,
            ambiguous_entity_type=parsed.ambiguous if parsed else False,
        )

    def apply_applicability(
        self, audit: Audit, corpus: list[ControlDefinition]
    ) -> dict[str, ApplicabilityOutcome]:
        """Run the applicability engine over the corpus and record what it decided.

        Rows are written only for controls that actually have authored
        conditions. A control with none applies universally, and writing 167
        meaningless "in scope by default" rows per audit would bury the ones that
        represent a real determination.

        Nothing here is auto-confirmed. Confirmation stays the human gate
        (01_REQUIREMENTS.md § PCI DSS Scope Matching).
        """
        profile = self._profile_for(audit)
        existing = {
            row.control_definition_id: row for row in self._scoped.list_all_for_audit(audit.id)
        }

        determinations: dict[str, ApplicabilityOutcome] = {}
        unanswered: set[str] = set()
        for control in corpus:
            conditions = [c for c in control.applicability_conditions if isinstance(c, dict)]
            outcome = applicability.evaluate(conditions, profile)
            determinations[control.control_id] = outcome
            unanswered.update(outcome.unanswered)

            if not conditions:
                continue

            # Persist only determinations that carry information. An UNDETERMINED
            # row says "the profile does not answer this", which is true of every
            # conditioned control on a blank profile — writing dozens of them per
            # audit would bury the handful that represent a real decision. The
            # actionable signal for those is an incomplete profile, surfaced on
            # the audit itself, not one scope row per unanswered question.
            if outcome.status is ApplicabilityStatus.UNDETERMINED:
                continue

            row = existing.get(control.id)
            if row is not None:
                # Never overwrite a human's confirmation with a fresh machine run.
                if row.confirmed:
                    continue
                row.applicability_status = outcome.status
                row.applicability_evidence = outcome.evidence
                continue

            self._scoped.create(
                audit_id=audit.id,
                control_definition_id=control.id,
                source=ScopeSource.deterministic,
                rationale=_applicability_rationale(outcome),
                applicability_status=outcome.status,
                applicability_evidence=outcome.evidence,
            )

        self._db.flush()
        if unanswered:
            # Worth logging: every unanswered attribute is a control the engine
            # could have settled and could not.
            logger.info(
                "applicability.unanswered audit=%s attributes=%s",
                audit.id,
                ",".join(sorted(unanswered)),
            )
        logger.info(
            "applicability.evaluated audit=%s in_scope=%d not_applicable=%d undetermined=%d",
            audit.id,
            sum(1 for o in determinations.values() if o.status is ApplicabilityStatus.IN_SCOPE),
            sum(
                1 for o in determinations.values() if o.status is ApplicabilityStatus.NOT_APPLICABLE
            ),
            sum(1 for o in determinations.values() if o.status is ApplicabilityStatus.UNDETERMINED),
        )
        return determinations

    @staticmethod
    def _profile_for(audit: Audit) -> dict[str, object]:
        """The profile conditions are evaluated against.

        `entity_type` and `merchant_level` live in their own columns; they are
        folded in here so a condition can name them without the profile
        duplicating storage.
        """
        profile = dict(audit.company_profile or {})
        profile["entity_type"] = audit.entity_type.value
        if audit.merchant_level is not None:
            profile["merchant_level"] = audit.merchant_level.value
        return profile

    def _require_profile_fields(self, audit: Audit) -> None:
        """04_API_CONTRACT.md: 409 MISSING_PROFILE_FIELDS."""
        missing: list[str] = []
        if audit.entity_type is None:
            missing.append("entity_type")
        if audit.entity_type == EntityType.merchant and audit.merchant_level is None:
            missing.append("merchant_level")
        if missing:
            raise ConflictError(
                "This audit is missing profile fields required for scoping.",
                code=CODE_MISSING_PROFILE_FIELDS,
                missing_fields=missing,
            )

    def _enforce_rate_limit(self, actor: Actor) -> None:
        """04_API_CONTRACT.md: capped per user to prevent runaway LLM cost.

        Counted from ScopedControl rows this actor's suggestions produced
        would be wrong (a failed call produces none), so the limiter counts
        suggestion *events*. With no separate events table, the cheapest honest
        proxy is the number of distinct audits this actor has suggested
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

    def _call_llm(self, audit: Audit, corpus: list[ControlDefinition]) -> _ParsedSuggestion:
        """Build the prompt and parse the reply.

        05_SECURITY.md and TASK-013: only structured profile fields are sent at
        this step — there is no evidence content in the audit yet, and this
        method deliberately reads no evidence table so that stays true.
        """
        clause_catalogue = "\n".join(
            f"{r.control_id} [family {r.requirement_family}] {r.name}" for r in corpus
        )
        profile = {
            "entity_type": audit.entity_type.value,
            "merchant_level": audit.merchant_level.value if audit.merchant_level else None,
            "annual_transaction_volume": audit.annual_transaction_volume,
            "existing_saq_type": audit.existing_saq_type,
            "tech_stack_summary": audit.tech_stack_summary,
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
        audit: Audit,
        corpus: list[ControlDefinition],
        raw_requirements: list[Any],
        *,
        excluded: set[str] | None = None,
    ) -> list[ScopedControl]:
        # Replace prior unconfirmed AI suggestions; confirmed rows are untouched.
        self._scoped.clear_unconfirmed_suggestions(audit.id)
        already_present = self._scoped.existing_requirement_ids(audit.id)
        by_clause = {r.control_id: r for r in corpus}

        proposed: list[ScopedControl] = []
        for item in raw_requirements:
            if not isinstance(item, dict):
                continue
            control_id = str(item.get("control_id", "")).strip()
            requirement = by_clause.get(control_id)
            if requirement is None:
                # A hallucinated clause id is dropped rather than stored. The
                # scope must only ever reference real corpus rows.
                logger.info("Dropped suggested clause not present in corpus: %s", control_id)
                continue
            if requirement.id in already_present:
                # Already scoped (manually, deterministically, or confirmed from
                # a previous run).
                continue
            if excluded and control_id in excluded:
                # The applicability engine determined this control does not apply.
                # A model proposal cannot overturn a mechanical determination —
                # dropped and logged, exactly as a hallucinated clause id is.
                logger.info(
                    "Dropped AI-suggested clause the applicability engine excluded: %s",
                    control_id,
                )
                continue

            # Prefixed so the UI — and anyone reading the row later — can tell a
            # model's reasoning from a rule's.
            raw_rationale = str(item.get("rationale", "")).strip()
            rationale = f"AI-suggested (advisory): {raw_rationale}" if raw_rationale else None
            proposed.append(
                self._scoped.create(
                    audit_id=audit.id,
                    control_definition_id=requirement.id,
                    source=ScopeSource.ai_suggested,
                    rationale=rationale,
                    confirmed=False,  # never auto-confirmed
                )
            )
            already_present.add(requirement.id)
        return proposed

    # --- Human actions -------------------------------------------------------

    def list_scope(
        self, audit_id: uuid.UUID, actor: Actor, *, confirmed_only: bool = False
    ) -> list[ScopedControl]:
        self._audits.get(audit_id, actor)
        return self._scoped.list_for_audit(audit_id, actor, confirmed_only=confirmed_only)

    def add_manual(
        self, audit_id: uuid.UUID, control_id: str, actor: Actor, rationale: str | None
    ) -> ScopedControl:
        """The auditor adds a clause the AI did not propose.

        01_REQUIREMENTS.md: "the auditor ... can add/remove rows (source =
        manual for anything the auditor added directly)". Added rows are still
        created unconfirmed — adding is not confirming.
        """
        audit = self._audits.get(audit_id, actor)
        self._audits.ensure_not_finalized(audit)

        version = self._corpus.current_version()
        if version is None:
            raise ConflictError("The PCI DSS corpus has not been loaded.", code="CORPUS_NOT_LOADED")

        matches = self._corpus.get_by_clause_ids([control_id], version)
        if not matches:
            raise ValidationError(f"No PCI DSS clause '{control_id}' in corpus {version}.")

        if matches[0].id in self._scoped.existing_requirement_ids(audit_id):
            raise ConflictError(
                f"Clause {control_id} is already in this audit's scope.",
                code="ALREADY_SCOPED",
            )

        # Adding a requirement means scoping has begun, whoever proposed it.
        # Without this, an audit scoped entirely by hand — the documented
        # path when the LLM is unavailable — would sit in `intake` forever and
        # could never be finalized, because `intake` has no edge to
        # `in_progress`. Only a *successful* AI suggestion used to advance it.
        if audit.status == AuditStatus.intake:
            self._audits.advance_status(audit, AuditStatus.scoping)

        return self._scoped.create(
            audit_id=audit_id,
            control_definition_id=matches[0].id,
            source=ScopeSource.manual,
            rationale=rationale,
            confirmed=False,
        )

    def confirm(self, scoped_id: uuid.UUID, actor: Actor, *, confirmed: bool) -> ScopedControl:
        """The human confirmation gate (04_API_CONTRACT.md → PATCH).

        One row, one call, one audit-trail event. 04_API_CONTRACT.md is explicit
        that a bulk UI action is fine but must still write one confirmation per
        row, which is why no bulk endpoint exists.
        """
        scoped = self._get_scoped_or_raise(scoped_id, actor)
        audit = self._audits.get(scoped.audit_id, actor)
        self._audits.ensure_not_finalized(audit)

        scoped.confirmed = confirmed
        self._db.flush()

        if confirmed:
            # Walk the documented lifecycle forward rather than adding a
            # shortcut edge to the state machine. The `intake` step is reachable
            # here for a requirement created before scoping was marked as begun;
            # confirming one unambiguously means the audit is past intake.
            if audit.status == AuditStatus.intake:
                self._audits.advance_status(audit, AuditStatus.scoping)
            if audit.status == AuditStatus.scoping:
                self._audits.advance_status(audit, AuditStatus.in_progress)
        return scoped

    def acknowledge_gap(
        self, scoped_id: uuid.UUID, actor: Actor, *, acknowledged: bool, note: str | None
    ) -> ScopedControl:
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
        audit = self._audits.get(scoped.audit_id, actor)
        self._audits.ensure_not_finalized(audit)

        scoped.gap_acknowledged = acknowledged
        scoped.gap_note = note.strip() if acknowledged and note else None
        self._db.flush()
        return scoped

    def _get_scoped_or_raise(self, scoped_id: uuid.UUID, actor: Actor) -> ScopedControl:
        scoped = self._scoped.get_scoped(scoped_id, actor)
        if scoped is not None:
            return scoped
        if self._scoped.exists_unscoped(scoped_id):
            raise ForbiddenError("You are not assigned to this audit.")
        raise NotFoundError("Scoped requirement not found.")

    def count_confirmed(self, audit_id: uuid.UUID) -> int:
        return self._scoped.count_confirmed(audit_id)


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
