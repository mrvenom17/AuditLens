"""Finding schemas.

04_API_CONTRACT.md → GET /api/audits/{id}/findings, Security Notes: the response
schema itself keeps `system_result` (machine) and `auditor_decision` (human,
null until reviewed) as separate top-level fields. That separation is enforced
here, in the contract, not merely in the UI — a client physically cannot receive
them pre-merged, and so cannot render one as the other by reading a single field.

`FindingReviewRequest` has no `system_result` field. There is no field name a
caller could send that would overwrite the machine's determination
(04_API_CONTRACT.md → PATCH /api/findings/{id}/review, Security Notes).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, computed_field

from app.models.enums import (
    EvaluationMode,
    EvaluationResult,
    EvidenceStrength,
    FindingAction,
    FindingStatus,
    GateStatus,
)
from app.models.finding import Finding
from app.schemas.common import ORMModel


class Citation(BaseModel):
    """One evidence reference, carrying enough provenance for a human to open
    the document and check the claim themselves — and to confirm the file has
    not changed since."""

    fact: str | None = None
    value: str | None = None
    evidence_document_id: uuid.UUID | None = None
    # Defaults to None so citations written before hashes were carried still
    # parse; no backfill needed.
    source_hash: str | None = None
    location: str | None = None
    page: int | None = None
    line: int | None = None
    cell: str | None = None


class FindingResponse(ORMModel):
    id: uuid.UUID
    audit_id: uuid.UUID
    control_evaluation_id: uuid.UUID
    scoped_control_id: uuid.UUID | None
    control_id: str
    # The requirement itself. Without it the auditor is reviewing a verdict
    # against the string "8.3.6" — 01_REQUIREMENTS.md § Finding Review requires
    # the original requirement and its assessment procedure be on screen.
    control_name: str
    requirement_text: str
    assessment_procedures: list[str]

    # --- What the machine determined. Read-only, always. --------------------
    system_result: EvaluationResult
    evaluation_mode: EvaluationMode
    gate_status: GateStatus
    gate_checks_failed: list[str]
    rules_used: list[dict[str, Any]]
    evidence_locations: list[Citation]
    contradictions: list[dict[str, Any]] | None
    stale_evidence: bool
    # How much weight the evidence can bear, graded mechanically. `strength_factors`
    # names the criteria that fired so the UI can explain the grade rather than
    # asserting it.
    evidence_strength: EvidenceStrength
    strength_factors: list[str]
    engine_version: str
    llm_involved: bool

    # --- Non-authoritative prose. Never a determination. --------------------
    ai_explanation: str | None

    # --- What the human decided. Null until reviewed. ------------------------
    status: FindingStatus
    auditor_decision: EvaluationResult | None
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None
    review_note: str | None

    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def awaiting_review(self) -> bool:
        """True while no human has ruled on this finding."""
        return self.status == FindingStatus.pending_review

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_override(self) -> bool:
        """Whether the human disagreed with the machine.

        Computed server-side and returned explicitly so the UI does not have to
        infer it by comparing two fields and risk getting the comparison wrong.
        """
        return self.auditor_decision is not None and self.auditor_decision != self.system_result

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unverified_by_gate(self) -> bool:
        """True when the Evidence Gate could not verify this evaluation.

        01_REQUIREMENTS.md § Finding Review, Edge Cases requires a gate-REJECTED
        finding be unmistakable from a normally-verified one. Returning this as
        an explicit flag — rather than leaving the client to interpret
        `gate_status` — is what makes that a contract guarantee rather than a
        styling convention.
        """
        return self.gate_status != GateStatus.VERIFIED

    @classmethod
    def of(cls, finding: Finding, control: Any) -> FindingResponse:
        evaluation = finding.evaluation
        return cls(
            id=finding.id,
            audit_id=finding.audit_id,
            control_evaluation_id=finding.control_evaluation_id,
            scoped_control_id=finding.scoped_control_id,
            control_id=control.control_id if control is not None else "unknown",
            control_name=control.name if control is not None else "",
            requirement_text=control.requirement_text if control is not None else "",
            assessment_procedures=list(control.assessment_procedures) if control else [],
            system_result=evaluation.result,
            evaluation_mode=evaluation.evaluation_mode,
            gate_status=evaluation.gate_status,
            gate_checks_failed=list(evaluation.gate_checks_failed),
            rules_used=evaluation.rules_used,
            evidence_locations=[Citation(**c) for c in evaluation.evidence_locations],
            contradictions=evaluation.contradictions,
            stale_evidence=bool(evaluation.stale),
            evidence_strength=evaluation.evidence_strength,
            strength_factors=list(evaluation.strength_factors),
            engine_version=evaluation.engine_version,
            llm_involved=bool(evaluation.llm_involved),
            ai_explanation=finding.ai_explanation,
            status=finding.status,
            auditor_decision=finding.auditor_decision,
            reviewed_by=finding.reviewed_by,
            reviewed_at=finding.reviewed_at,
            review_note=finding.review_note,
            created_at=finding.created_at,
            updated_at=finding.updated_at,
        )


class FindingReviewRequest(BaseModel):
    """04_API_CONTRACT.md → PATCH /api/findings/{id}/review.

    Note the two fields that are deliberately absent: `reviewed_by`, derived
    from the authenticated session server-side, and `system_result`, which has
    no external writer anywhere in this API (05_SECURITY.md §10.3).

    `auditor_decision` is optional on approve — omitting it means "I agree with
    the system result", and the service copies that value across into the
    human's own column so both remain independently readable.
    """

    action: FindingAction
    auditor_decision: EvaluationResult | None = None
    note: str | None = Field(default=None, max_length=5000)


class FindingHistoryEntry(ORMModel):
    id: uuid.UUID
    finding_id: uuid.UUID
    actor_id: uuid.UUID
    action: FindingAction
    previous_status: FindingStatus
    new_status: FindingStatus
    previous_decision: EvaluationResult | None
    new_decision: EvaluationResult | None
    system_result: EvaluationResult | None
    note: str | None
    created_at: datetime


class BlockingRequirement(BaseModel):
    scoped_control_id: uuid.UUID
    control_id: str
    reason: str


class FinalizationReadiness(BaseModel):
    ready: bool
    blocking_requirements: list[BlockingRequirement]


class FinalizeResponse(BaseModel):
    report_id: uuid.UUID
    audit_status: str


class ReportResponse(ORMModel):
    id: uuid.UUID
    audit_id: uuid.UUID
    generated_by: uuid.UUID
    generated_at: datetime
    corpus_version: str | None
    engine_version: str | None
    snapshot_data: dict[str, Any]
