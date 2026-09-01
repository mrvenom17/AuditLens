"""Audit schemas (04_API_CONTRACT.md → /api/audits)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.models.enums import (
    AuditStatus,
    CloudProvider,
    DataType,
    EntityType,
    Environment,
    Industry,
    MerchantLevel,
    SystemComponent,
)
from app.schemas.common import ORMModel

# 05_SECURITY.md §10.4 requires explicit string length limits per field rather
# than implicit ones. These match 01_REQUIREMENTS.md § Audit Creation.
CLIENT_NAME_MAX = 200
TECH_STACK_SUMMARY_MAX = 5000
SAQ_TYPE_MAX = 20


class CompanyProfile(BaseModel):
    """What the applicability engine evaluates control conditions against.

    Every field is optional, and that is the point: an unanswered question must
    stay distinguishable from an answered-negative one. `None` means "never
    asked" and yields UNDETERMINED for any condition on it; an empty list means
    "asked, none apply" and is a real answer that may exclude a control.

    Validated strictly here and stored as JSONB — the same treatment
    `ControlDefinition.facts`/`.rules` get, because this data becomes logic
    (05_SECURITY.md §10.4 forbids accepting arbitrary JSON for anything that
    does).
    """

    model_config = {"extra": "forbid"}

    industry: Industry | None = None
    environment: Environment | None = None
    systems: list[SystemComponent] | None = None
    data_types: list[DataType] | None = None
    cloud_providers: list[CloudProvider] | None = None

    # The three that PCI applicability actually turns on. Kept as separate
    # booleans rather than inferred from `data_types`, because "we store PAN" and
    # "PAN is one of the data types in our environment" are different claims.
    stores_cardholder_data: bool | None = None
    transmits_cardholder_data: bool | None = None
    outsources_card_processing: bool | None = None


class AuditProfileUpdate(BaseModel):
    """Body for PATCH /api/audits/{id}.

    The profile now drives mechanical scope exclusion, so a typo in it silently
    removes controls from an audit. Without an edit path the only remedy would be
    abandoning the audit and starting again.
    """

    company_profile: CompanyProfile


class AuditCreate(BaseModel):
    client_name: str = Field(min_length=1, max_length=CLIENT_NAME_MAX)
    entity_type: EntityType
    merchant_level: MerchantLevel | None = None
    annual_transaction_volume: int | None = Field(default=None, ge=0)
    existing_saq_type: str | None = Field(default=None, max_length=SAQ_TYPE_MAX)
    tech_stack_summary: str | None = Field(default=None, max_length=TECH_STACK_SUMMARY_MAX)
    company_profile: CompanyProfile = Field(default_factory=lambda: CompanyProfile())
    source_document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)

    # Deliberately absent: `status`, `created_by`, `finalized_by`. Accepting any
    # of them would let a client set its own ownership or skip the workflow —
    # they are derived server-side from the session and the state machine
    # (05_SECURITY.md §10.3).

    @model_validator(mode="after")
    def merchant_level_required_for_merchants(self) -> AuditCreate:
        """01_REQUIREMENTS.md: required and validated only when
        entity_type = merchant."""
        if self.entity_type == EntityType.merchant and self.merchant_level is None:
            raise ValueError("merchant_level is required when entity_type is 'merchant'")
        if self.entity_type == EntityType.service_provider and self.merchant_level is not None:
            raise ValueError("merchant_level does not apply to a service provider")
        return self

    @model_validator(mode="after")
    def strip_client_name(self) -> AuditCreate:
        cleaned = self.client_name.strip()
        if not cleaned:
            raise ValueError("client_name must not be blank")
        object.__setattr__(self, "client_name", cleaned)
        return self


class AuditSummary(ORMModel):
    """List-view shape. Omits `tech_stack_summary`, which is classified
    Sensitive (03_DATA_MODEL.md §8.4) and is not needed to render a list."""

    id: uuid.UUID
    client_name: str
    entity_type: EntityType
    merchant_level: MerchantLevel | None
    status: AuditStatus
    created_at: datetime
    updated_at: datetime


class AuditCounts(BaseModel):
    """The summary 04_API_CONTRACT.md requires on the detail response."""

    scoped_controls: int
    confirmed_requirements: int
    evidence_requests: int
    evidence_documents: int
    findings_total: int
    findings_pending_review: int
    findings_approved: int
    findings_rejected: int
    findings_needing_more_evidence: int


class AuditDetail(ORMModel):
    id: uuid.UUID
    client_name: str
    entity_type: EntityType
    merchant_level: MerchantLevel | None
    annual_transaction_volume: int | None
    existing_saq_type: str | None
    tech_stack_summary: str | None
    status: AuditStatus
    created_by: uuid.UUID
    finalized_by: uuid.UUID | None
    finalized_at: datetime | None
    created_at: datetime
    updated_at: datetime
    counts: AuditCounts
    assigned_user_ids: list[uuid.UUID]


class AssignmentCreate(BaseModel):
    user_id: uuid.UUID


class AssignmentResponse(ORMModel):
    id: uuid.UUID
    audit_id: uuid.UUID
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
