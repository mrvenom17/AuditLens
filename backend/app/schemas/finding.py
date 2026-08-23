"""Finding schemas.

04_API_CONTRACT.md → GET /api/engagements/{id}/findings, Security Notes: "the
API response schema itself (not just the UI) should make it impossible to
mistake a draft AI suggestion for a final determination."

That is why `ai_suggested_status` and `final_status` are separate fields that
are never merged, why `final_status` is null until a human approves, and why
`is_ai_draft` is computed and returned explicitly rather than left for the
client to infer.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, computed_field

from app.models.enums import ComplianceStatus, FindingAction, FindingStatus
from app.models.finding import Finding
from app.schemas.common import ORMModel


class Citation(BaseModel):
    evidence_document_id: uuid.UUID
    location: str


class FindingResponse(ORMModel):
    id: uuid.UUID
    engagement_id: uuid.UUID
    scoped_requirement_id: uuid.UUID
    clause_id: str

    # --- AI draft. Never a determination. ---
    ai_suggested_status: ComplianceStatus | None
    ai_confidence: float | None
    ai_rationale: str | None
    needs_manual_review: bool

    # --- Human determination. Null until approved. ---
    status: FindingStatus
    final_status: ComplianceStatus | None
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None
    review_note: str | None

    citations: list[Citation]
    evidence_document_ids: list[uuid.UUID]
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_ai_draft(self) -> bool:
        """True while no human has ruled on this finding.

        Returned explicitly so a client cannot accidentally render an AI
        suggestion as a determination by reading the wrong field.
        """
        return self.status == FindingStatus.draft

    @classmethod
    def of(cls, finding: Finding, clause_id: str) -> FindingResponse:
        return cls(
            id=finding.id,
            engagement_id=finding.engagement_id,
            scoped_requirement_id=finding.scoped_requirement_id,
            clause_id=clause_id,
            ai_suggested_status=finding.ai_suggested_status,
            ai_confidence=finding.ai_confidence,
            ai_rationale=finding.ai_rationale,
            needs_manual_review=finding.needs_manual_review,
            status=finding.status,
            final_status=finding.final_status,
            reviewed_by=finding.reviewed_by,
            reviewed_at=finding.reviewed_at,
            review_note=finding.review_note,
            citations=[Citation(**c) for c in finding.citations],
            evidence_document_ids=finding.evidence_document_ids,
            created_at=finding.created_at,
            updated_at=finding.updated_at,
        )


class FindingReviewRequest(BaseModel):
    """04_API_CONTRACT.md → PATCH /api/findings/{id}/review.

    `reviewed_by` is deliberately not a field. It is derived from the
    authenticated session server-side and never accepted from the body
    (05_SECURITY.md §10.3).
    """

    action: FindingAction
    edited_status: ComplianceStatus | None = None
    note: str | None = Field(default=None, max_length=5000)


class FindingHistoryEntry(ORMModel):
    id: uuid.UUID
    finding_id: uuid.UUID
    actor_id: uuid.UUID
    action: FindingAction
    previous_status: FindingStatus
    new_status: FindingStatus
    previous_final_status: ComplianceStatus | None
    new_final_status: ComplianceStatus | None
    note: str | None
    created_at: datetime


class BlockingRequirement(BaseModel):
    scoped_requirement_id: uuid.UUID
    clause_id: str
    reason: str


class FinalizationReadiness(BaseModel):
    ready: bool
    blocking_requirements: list[BlockingRequirement]


class FinalizeResponse(BaseModel):
    report_id: uuid.UUID
    engagement_status: FindingStatus | str


class ReportResponse(ORMModel):
    id: uuid.UUID
    engagement_id: uuid.UUID
    generated_by: uuid.UUID
    generated_at: datetime
    snapshot_data: dict
