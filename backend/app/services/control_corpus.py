"""Control corpus authoring and versioning (TASK-102, TASK-103).

Controls are firm-wide reference data. Two rules govern every write here:

* **Human-authored only.** No function in this module accepts, calls, or is
  reachable from an LLM. 01_REQUIREMENTS.md is unusually blunt about this — "the
  single most important rule in this entire document" — because every downstream
  guarantee is only as good as the rules being what a human meant.
* **Versioned, never mutated.** Once any ScopedControl references a definition,
  editing it creates a new row and points the old one at it via `superseded_by`.
  An audit already citing the old version keeps citing it, which is what makes a
  finalized report immune to a later rule change (08_TESTING.md, policy-version
  immutability test).
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import exists, select
from sqlalchemy.orm import Session as DBSession

from app.config.settings import settings
from app.errors import ForbiddenError, NotFoundError, ValidationError
from app.models.corpus import ControlDefinition
from app.models.enums import EvaluationMode, Role
from app.models.scoping import ScopedControl
from app.schemas.evaluation import ControlDefinitionCreate

if TYPE_CHECKING:
    from app.api.deps import Actor

logger = logging.getLogger(__name__)

# 04_API_CONTRACT.md gives this its own named code precisely because it is the
# most important validation in the system.
CODE_DETERMINISTIC_MISSING_RULES = "DETERMINISTIC_CONTROL_MISSING_RULES"


class ControlCorpusService:
    def __init__(self, db: DBSession) -> None:
        self._db = db

    # --- Reads (any authenticated user) --------------------------------------

    def list_controls(
        self,
        *,
        evaluation_mode: EvaluationMode | None = None,
        requirement_family: int | None = None,
        corpus_version: str | None = None,
        include_superseded: bool = False,
    ) -> list[ControlDefinition]:
        """The corpus is not secret — it is the published standard's mechanics,
        readable by any authenticated user (04_API_CONTRACT.md)."""
        stmt = select(ControlDefinition)
        if evaluation_mode is not None:
            stmt = stmt.where(ControlDefinition.evaluation_mode == evaluation_mode)
        if requirement_family is not None:
            stmt = stmt.where(ControlDefinition.requirement_family == requirement_family)
        if corpus_version is not None:
            stmt = stmt.where(ControlDefinition.corpus_version == corpus_version)
        if not include_superseded:
            stmt = stmt.where(ControlDefinition.superseded_by.is_(None))
        return list(
            self._db.scalars(
                stmt.order_by(ControlDefinition.requirement_family, ControlDefinition.control_id)
            )
        )

    def get(self, control_definition_id: uuid.UUID) -> ControlDefinition:
        control = self._db.get(ControlDefinition, control_definition_id)
        if control is None:
            raise NotFoundError("Control definition not found.")
        return control

    # --- Authoring (Admin only) ----------------------------------------------

    def create(self, payload: ControlDefinitionCreate, actor: Actor) -> ControlDefinition:
        """Author a control, or a new version of one.

        The role check lives here rather than in the route so it holds for any
        caller, matching how every other privileged action in this codebase is
        gated (02_ARCHITECTURE.md §7.4).
        """
        if actor.role != Role.admin:
            raise ForbiddenError("Only an Admin may author control definitions.")

        # Enforced at the service layer as well as the database CHECK. TASK-102
        # asks for belt and suspenders on this one specifically.
        if payload.evaluation_mode == EvaluationMode.DETERMINISTIC and (
            not payload.rules or not payload.facts
        ):
            raise ValidationError(
                "A DETERMINISTIC control requires at least one fact and one rule.",
                code=CODE_DETERMINISTIC_MISSING_RULES,
            )

        corpus_version = payload.corpus_version or settings.CONTROL_CORPUS_VERSION
        existing = self._db.scalar(
            select(ControlDefinition).where(
                ControlDefinition.control_id == payload.control_id,
                ControlDefinition.corpus_version == corpus_version,
                ControlDefinition.superseded_by.is_(None),
            )
        )
        if existing is not None:
            raise ValidationError(
                f"Control {payload.control_id} already exists in corpus version "
                f"{corpus_version}. Author a new corpus_version to revise it."
            )

        control = ControlDefinition(
            control_id=payload.control_id,
            name=payload.name,
            requirement_text=payload.requirement_text,
            requirement_family=payload.requirement_family,
            evaluation_mode=payload.evaluation_mode,
            evidence_requirements=[e.model_dump() for e in payload.evidence_requirements],
            facts=[f.model_dump(mode="json") for f in payload.facts],
            rules=[r.model_dump(mode="json") for r in payload.rules],
            freshness_window_days=payload.freshness_window_days,
            corpus_version=corpus_version,
        )
        self._db.add(control)
        self._db.flush()

        logger.info(
            "control_definition.authored control=%s mode=%s version=%s actor=%s",
            control.control_id,
            control.evaluation_mode.value,
            control.corpus_version,
            actor.id,
        )
        return control

    def supersede(
        self, control_definition_id: uuid.UUID, payload: ControlDefinitionCreate, actor: Actor
    ) -> ControlDefinition:
        """Create a new version and point the old one at it.

        The old row is never edited. Any audit that already referenced it — and
        any finalized report that snapshotted it — is untouched by this call,
        which is the whole mechanism behind the policy-version immutability
        test in 08_TESTING.md.
        """
        if actor.role != Role.admin:
            raise ForbiddenError("Only an Admin may author control definitions.")

        previous = self.get(control_definition_id)
        if payload.corpus_version and payload.corpus_version == previous.corpus_version:
            raise ValidationError("A superseding control must carry a new corpus_version.")

        replacement = self.create(payload, actor)
        previous.superseded_by = replacement.id
        self._db.flush()

        logger.info(
            "control_definition.superseded old=%s new=%s actor=%s",
            previous.id,
            replacement.id,
            actor.id,
        )
        return replacement

    def is_referenced(self, control_definition_id: uuid.UUID) -> bool:
        """Whether any audit has scoped this control. A referenced definition is
        never deleted and never edited in place (03_DATA_MODEL.md)."""
        return bool(
            self._db.scalar(
                select(exists().where(ScopedControl.control_definition_id == control_definition_id))
            )
        )
