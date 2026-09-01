"""Control corpus, fact and evaluation schemas (04_API_CONTRACT.md).

The single most important property of this module is a *negative* one: no model
here has a writable `result` field. 03_DATA_MODEL.md §8.2 and 05_SECURITY.md
§10.3 require `ControlEvaluation.result` to be unreachable from any request
body under any role — not permission-gated, absent. `ControlEvaluationResponse`
is response-only, and no `...Request` model in this file mentions the field at
all, so there is no name a client could send that would bind to it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import (
    EvaluationMode,
    EvaluationResult,
    FactValueType,
    GateStatus,
    RuleOperator,
    VerificationStatus,
)
from app.schemas.common import ORMModel


class FactSpec(BaseModel):
    """One entry in ControlDefinition.facts."""

    name: str = Field(min_length=1, max_length=120)
    type: FactValueType


class RuleSpec(BaseModel):
    """One entry in ControlDefinition.rules.

    05_SECURITY.md §10.4: this JSON becomes executable logic in the rule engine,
    so it is validated against a strict model on write rather than accepted as
    arbitrary JSON. `operator` is constrained to the fixed enum, which means the
    engine can never meet an operator it does not implement.
    """

    fact: str = Field(min_length=1, max_length=120)
    operator: RuleOperator
    expected: Any = None

    @model_validator(mode="after")
    def _expected_required_unless_presence_check(self) -> RuleSpec:
        """EXISTS/NOT_EXISTS take no operand; every other operator needs one.

        Catching this at authoring time matters more than it looks: a rule with
        a missing `expected` would otherwise compare against None and fail
        silently for reasons no auditor could see.
        """
        presence_only = {RuleOperator.EXISTS, RuleOperator.NOT_EXISTS}
        if self.operator not in presence_only and self.expected is None:
            raise ValueError(f"operator {self.operator.value} requires an 'expected' value")
        return self


class EvidenceRequirementSpec(BaseModel):
    type: str = Field(min_length=1, max_length=60)
    description: str = Field(min_length=1, max_length=1000)


class ControlDefinitionResponse(ORMModel):
    id: uuid.UUID
    control_id: str
    name: str
    requirement_text: str
    requirement_family: int
    evaluation_mode: EvaluationMode
    evidence_requirements: list[dict[str, Any]]
    facts: list[dict[str, Any]]
    rules: list[dict[str, Any]]
    freshness_window_days: int | None
    corpus_version: str
    superseded_by: uuid.UUID | None
    created_at: datetime


class ControlDefinitionCreate(BaseModel):
    """04_API_CONTRACT.md → POST /api/control-definitions (Admin only).

    There is deliberately no AI-assisted "auto-populate rules" path anywhere
    near this model. 01_REQUIREMENTS.md calls human authorship of rules the
    single most important rule in the whole document: the deterministic engine's
    trustworthiness depends entirely on its rules being human-authored.
    """

    control_id: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=500)
    requirement_text: str = Field(min_length=1)
    requirement_family: int = Field(ge=1, le=12)
    evaluation_mode: EvaluationMode
    evidence_requirements: list[EvidenceRequirementSpec] = Field(default_factory=list)
    facts: list[FactSpec] = Field(default_factory=list)
    rules: list[RuleSpec] = Field(default_factory=list)
    freshness_window_days: int | None = Field(default=None, ge=1, le=3650)
    corpus_version: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def _deterministic_requires_rules(self) -> ControlDefinitionCreate:
        """The validation 04_API_CONTRACT.md gives its own error code to.

        A DETERMINISTIC control with no rules is a contradiction in terms, and
        must be rejected at authoring time rather than discovered at evaluation
        time as a control that silently checks nothing.
        """
        if self.evaluation_mode == EvaluationMode.DETERMINISTIC and (
            not self.rules or not self.facts
        ):
            raise ValueError("A DETERMINISTIC control requires at least one fact and one rule.")
        return self

    @model_validator(mode="after")
    def _rules_reference_declared_facts(self) -> ControlDefinitionCreate:
        """Every rule must name a fact the control actually declares.

        Otherwise the rule can never match anything and the control would
        evaluate to INSUFFICIENT_EVIDENCE forever, looking like missing evidence
        when it is really a typo in the corpus.
        """
        declared = {f.name for f in self.facts}
        unknown = sorted({r.fact for r in self.rules} - declared)
        if unknown:
            raise ValueError(
                f"rules reference facts not declared on this control: {', '.join(unknown)}"
            )
        return self


class EvidenceFactResponse(ORMModel):
    """04_API_CONTRACT.md → GET /api/audits/{id}/facts.

    `source_hash` is included on purpose: a client or a test can compare it to
    the document's current hash, which makes evidence tampering a first-class,
    externally-checkable condition rather than an internal detail.
    """

    id: uuid.UUID
    audit_id: uuid.UUID
    control_definition_id: uuid.UUID
    name: str
    value: str | None
    value_type: FactValueType
    document_id: uuid.UUID
    page: int | None
    line: int | None
    cell: str | None
    source_hash: str
    observed_at: datetime | None
    extracted_at: datetime
    extractor_version: str
    verification_status: VerificationStatus


class ControlEvaluationResponse(ORMModel):
    """Response-only. `result` is readable here and writable nowhere."""

    id: uuid.UUID
    audit_id: uuid.UUID
    control_definition_id: uuid.UUID
    result: EvaluationResult
    evaluation_mode: EvaluationMode
    facts_used: list[uuid.UUID]
    rules_used: list[dict[str, Any]]
    evidence_locations: list[dict[str, Any]]
    contradictions: list[dict[str, Any]] | None
    stale: bool
    gate_status: GateStatus
    gate_checks_failed: list[str]
    evaluated_at: datetime
    engine_version: str
    llm_involved: bool


class EvaluateResponse(BaseModel):
    evaluations: list[ControlEvaluationResponse]


class ControlDefinitionQuery(BaseModel):
    evaluation_mode: EvaluationMode | None = None
    requirement_family: int | None = Field(default=None, ge=1, le=12)
    corpus_version: str | None = None

    @field_validator("corpus_version")
    @classmethod
    def _bounded(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 40:
            raise ValueError("corpus_version is too long")
        return value
