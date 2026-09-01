"""Scoping and evidence-request schemas (04_API_CONTRACT.md)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import ApplicabilityStatus, EvidenceRequestStatus, ScopeSource
from app.models.scoping import EvidenceRequest, ScopedControl
from app.schemas.common import ORMModel


class ScopedRequirementResponse(ORMModel):
    id: uuid.UUID
    audit_id: uuid.UUID
    control_definition_id: uuid.UUID
    control_id: str
    name: str
    requirement_family: int
    source: ScopeSource
    confirmed: bool
    rationale: str | None
    gap_acknowledged: bool
    gap_note: str | None
    # What the applicability engine decided, and the conditions behind it. An
    # auditor asking "why is this out of scope?" gets the rule, not a verdict.
    applicability_status: ApplicabilityStatus
    applicability_evidence: list[dict[str, Any]] | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, scoped: ScopedControl) -> ScopedRequirementResponse:
        """Flatten the joined corpus clause into the response.

        The control id and name come from the corpus row rather than being
        duplicated onto ScopedControl, so a corpus re-version cannot leave
        the two disagreeing.
        """
        return cls(
            id=scoped.id,
            audit_id=scoped.audit_id,
            control_definition_id=scoped.control_definition_id,
            control_id=scoped.control.control_id,
            name=scoped.control.name,
            requirement_family=scoped.control.requirement_family,
            source=scoped.source,
            confirmed=scoped.confirmed,
            rationale=scoped.rationale,
            gap_acknowledged=scoped.gap_acknowledged,
            gap_note=scoped.gap_note,
            applicability_status=scoped.applicability_status,
            applicability_evidence=scoped.applicability_evidence,
            created_at=scoped.created_at,
            updated_at=scoped.updated_at,
        )


class ScopeSuggestionResponse(BaseModel):
    """04_API_CONTRACT.md → POST /api/audits/{id}/scope-suggestion.

    `manual_scoping_required` is a first-class part of the success response, not
    an error shape: an unavailable LLM degrades the feature, it does not fail
    the request (01_REQUIREMENTS.md acceptance criteria).
    """

    proposed_requirements: list[ScopedRequirementResponse]
    manual_scoping_required: bool
    saq_type: str | None = None
    ambiguous_entity_type: bool = False


class ScopedRequirementUpdate(BaseModel):
    confirmed: bool


class ScopedRequirementGapUpdate(BaseModel):
    gap_acknowledged: bool
    gap_note: str | None = Field(default=None, max_length=2000)


class ManualScopeCreate(BaseModel):
    control_id: str = Field(min_length=1, max_length=20)
    rationale: str | None = Field(default=None, max_length=2000)


class EvidenceRequestResponse(ORMModel):
    id: uuid.UUID
    audit_id: uuid.UUID
    scoped_control_id: uuid.UUID
    control_id: str
    description: str
    status: EvidenceRequestStatus
    description_source: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, request: EvidenceRequest, control_id: str) -> EvidenceRequestResponse:
        return cls(
            id=request.id,
            audit_id=request.audit_id,
            scoped_control_id=request.scoped_control_id,
            control_id=control_id,
            description=request.description,
            status=request.status,
            description_source=request.description_source,
            created_at=request.created_at,
            updated_at=request.updated_at,
        )


class EvidenceRequestUpdate(BaseModel):
    description: str | None = Field(default=None, min_length=1, max_length=5000)
    status: EvidenceRequestStatus | None = None


class EvidenceRequestGenerateResponse(BaseModel):
    created: list[EvidenceRequestResponse]
    skipped_already_requested: int
    llm_available: bool
