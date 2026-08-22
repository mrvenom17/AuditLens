"""Scoping and evidence-request schemas (04_API_CONTRACT.md)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import EvidenceRequestStatus, ScopeSource
from app.models.scoping import EvidenceRequest, ScopedRequirement
from app.schemas.common import ORMModel


class ScopedRequirementResponse(ORMModel):
    id: uuid.UUID
    engagement_id: uuid.UUID
    pci_requirement_id: uuid.UUID
    clause_id: str
    title: str
    requirement_family: int
    source: ScopeSource
    confirmed: bool
    rationale: str | None
    gap_acknowledged: bool
    gap_note: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, scoped: ScopedRequirement) -> ScopedRequirementResponse:
        """Flatten the joined corpus clause into the response.

        The clause id and title come from the corpus row rather than being
        duplicated onto ScopedRequirement, so a corpus re-version cannot leave
        the two disagreeing.
        """
        return cls(
            id=scoped.id,
            engagement_id=scoped.engagement_id,
            pci_requirement_id=scoped.pci_requirement_id,
            clause_id=scoped.requirement.clause_id,
            title=scoped.requirement.title,
            requirement_family=scoped.requirement.requirement_family,
            source=scoped.source,
            confirmed=scoped.confirmed,
            rationale=scoped.rationale,
            gap_acknowledged=scoped.gap_acknowledged,
            gap_note=scoped.gap_note,
            created_at=scoped.created_at,
            updated_at=scoped.updated_at,
        )


class ScopeSuggestionResponse(BaseModel):
    """04_API_CONTRACT.md → POST /api/engagements/{id}/scope-suggestion.

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
    clause_id: str = Field(min_length=1, max_length=20)
    rationale: str | None = Field(default=None, max_length=2000)


class EvidenceRequestResponse(ORMModel):
    id: uuid.UUID
    engagement_id: uuid.UUID
    scoped_requirement_id: uuid.UUID
    clause_id: str
    description: str
    status: EvidenceRequestStatus
    description_source: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, request: EvidenceRequest, clause_id: str) -> EvidenceRequestResponse:
        return cls(
            id=request.id,
            engagement_id=request.engagement_id,
            scoped_requirement_id=request.scoped_requirement_id,
            clause_id=clause_id,
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
