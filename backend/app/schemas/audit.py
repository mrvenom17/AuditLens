"""Engagement schemas (04_API_CONTRACT.md → /api/engagements)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.models.enums import EngagementStatus, EntityType, MerchantLevel
from app.schemas.common import ORMModel

# 05_SECURITY.md §10.4 requires explicit string length limits per field rather
# than implicit ones. These match 01_REQUIREMENTS.md § Engagement Creation.
CLIENT_NAME_MAX = 200
TECH_STACK_SUMMARY_MAX = 5000
SAQ_TYPE_MAX = 20


class EngagementCreate(BaseModel):
    client_name: str = Field(min_length=1, max_length=CLIENT_NAME_MAX)
    entity_type: EntityType
    merchant_level: MerchantLevel | None = None
    annual_transaction_volume: int | None = Field(default=None, ge=0)
    existing_saq_type: str | None = Field(default=None, max_length=SAQ_TYPE_MAX)
    tech_stack_summary: str | None = Field(default=None, max_length=TECH_STACK_SUMMARY_MAX)
    source_document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)

    # Deliberately absent: `status`, `created_by`, `finalized_by`. Accepting any
    # of them would let a client set its own ownership or skip the workflow —
    # they are derived server-side from the session and the state machine
    # (05_SECURITY.md §10.3).

    @model_validator(mode="after")
    def merchant_level_required_for_merchants(self) -> EngagementCreate:
        """01_REQUIREMENTS.md: required and validated only when
        entity_type = merchant."""
        if self.entity_type == EntityType.merchant and self.merchant_level is None:
            raise ValueError("merchant_level is required when entity_type is 'merchant'")
        if self.entity_type == EntityType.service_provider and self.merchant_level is not None:
            raise ValueError("merchant_level does not apply to a service provider")
        return self

    @model_validator(mode="after")
    def strip_client_name(self) -> EngagementCreate:
        cleaned = self.client_name.strip()
        if not cleaned:
            raise ValueError("client_name must not be blank")
        object.__setattr__(self, "client_name", cleaned)
        return self


class EngagementSummary(ORMModel):
    """List-view shape. Omits `tech_stack_summary`, which is classified
    Sensitive (03_DATA_MODEL.md §8.4) and is not needed to render a list."""

    id: uuid.UUID
    client_name: str
    entity_type: EntityType
    merchant_level: MerchantLevel | None
    status: EngagementStatus
    created_at: datetime
    updated_at: datetime


class EngagementCounts(BaseModel):
    """The summary 04_API_CONTRACT.md requires on the detail response."""

    scoped_requirements: int
    confirmed_requirements: int
    evidence_requests: int
    evidence_documents: int
    findings_total: int
    findings_draft: int
    findings_approved: int
    findings_rejected: int
    findings_needing_manual_review: int


class EngagementDetail(ORMModel):
    id: uuid.UUID
    client_name: str
    entity_type: EntityType
    merchant_level: MerchantLevel | None
    annual_transaction_volume: int | None
    existing_saq_type: str | None
    tech_stack_summary: str | None
    status: EngagementStatus
    created_by: uuid.UUID
    finalized_by: uuid.UUID | None
    finalized_at: datetime | None
    created_at: datetime
    updated_at: datetime
    counts: EngagementCounts
    assigned_user_ids: list[uuid.UUID]


class AssignmentCreate(BaseModel):
    user_id: uuid.UUID


class AssignmentResponse(ORMModel):
    id: uuid.UUID
    engagement_id: uuid.UUID
    user_id: uuid.UUID
    assigned_at: datetime


class ClientProfileDocumentResponse(ORMModel):
    """Note the absence of `storage_path`: Sensitive per 03_DATA_MODEL.md §8.4,
    and a client that knows a server path knows more than it needs to."""

    id: uuid.UUID
    original_filename: str
    content_hash: str
    mime_type: str
    uploaded_by: uuid.UUID
    created_at: datetime
