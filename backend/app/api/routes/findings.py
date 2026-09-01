"""Finding review and audit finalization routes.

04_API_CONTRACT.md → PATCH /api/findings/{id}/review and
POST /api/audits/{id}/finalize — the latter described there as "the single
highest-stakes endpoint in the system".
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session as DBSession

from app.api.deps import CurrentActor
from app.db.session import get_db
from app.models.enums import FindingStatus
from app.models.finding import Finding
from app.schemas.common import ErrorResponse
from app.schemas.finding import (
    BlockingRequirement,
    FinalizationReadiness,
    FinalizeResponse,
    FindingHistoryEntry,
    FindingResponse,
    FindingReviewRequest,
    ReportResponse,
)
from app.services.finalization import FinalizationService
from app.services.finding import FindingService
from app.services.report_pdf import render_report_pdf

router = APIRouter(tags=["findings"])


def get_finding_service(db: Annotated[DBSession, Depends(get_db)]) -> FindingService:
    return FindingService(db)


def get_finalization_service(db: Annotated[DBSession, Depends(get_db)]) -> FinalizationService:
    return FinalizationService(db)


Findings = Annotated[FindingService, Depends(get_finding_service)]
Finalization = Annotated[FinalizationService, Depends(get_finalization_service)]


def _control_for(finding: Finding) -> Any:
    """The control id shown beside a finding.

    Read from the evaluation's own joined ControlDefinition rather than via the
    ScopedControl, so it stays correct even for a finding whose scope row was
    never linked — and so it always names the exact control version that was
    evaluated.
    """
    return finding.evaluation.control if finding.evaluation else None


@router.get("/api/audits/{audit_id}/findings", response_model=list[FindingResponse])
def list_findings(
    audit_id: uuid.UUID,
    actor: CurrentActor,
    service: Findings,
    db: Annotated[DBSession, Depends(get_db)],
    status: Annotated[FindingStatus | None, Query()] = None,
) -> list[FindingResponse]:
    """The review queue.

    `system_result` and `auditor_decision` come back as separate top-level
    fields, and a gate-unverified finding carries an explicit flag — both are
    contract guarantees, not UI conventions (04_API_CONTRACT.md, Security
    Notes).
    """
    findings = service.list_for_audit(audit_id, actor, status=status)
    return [FindingResponse.of(f, _control_for(f)) for f in findings]


@router.get("/api/findings/{finding_id}", response_model=FindingResponse)
def get_finding(
    finding_id: uuid.UUID,
    actor: CurrentActor,
    service: Findings,
    db: Annotated[DBSession, Depends(get_db)],
) -> FindingResponse:
    finding = service.get(finding_id, actor)
    return FindingResponse.of(finding, _control_for(finding))


@router.patch(
    "/api/findings/{finding_id}/review",
    response_model=FindingResponse,
    responses={
        400: {"model": ErrorResponse, "description": "VALIDATION_ERROR"},
        403: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "AUDIT_FINALIZED"},
    },
)
def review_finding(
    finding_id: uuid.UUID,
    payload: FindingReviewRequest,
    actor: CurrentActor,
    service: Findings,
    db: Annotated[DBSession, Depends(get_db)],
) -> FindingResponse:
    """Record a human decision: approve, reject, or request more evidence.

    The Finding update and its FindingHistory row are written in the service
    inside one transaction; this handler's single commit is what makes them
    atomic (03_DATA_MODEL.md §8.3).
    """
    finding = service.review(
        finding_id,
        actor,
        action=payload.action,
        auditor_decision=payload.auditor_decision,
        note=payload.note,
    )
    db.commit()
    return FindingResponse.of(finding, _control_for(finding))


@router.get("/api/findings/{finding_id}/history", response_model=list[FindingHistoryEntry])
def finding_history(
    finding_id: uuid.UUID, actor: CurrentActor, service: Findings
) -> list[FindingHistoryEntry]:
    """The append-only record of every decision on this finding, including
    Reviewer overrides of an Auditor's earlier action."""
    return [FindingHistoryEntry.model_validate(h) for h in service.history(finding_id, actor)]


# --- Finalization ------------------------------------------------------------


@router.get(
    "/api/audits/{audit_id}/finalization-readiness",
    response_model=FinalizationReadiness,
)
def finalization_readiness(
    audit_id: uuid.UUID, actor: CurrentActor, service: Finalization
) -> FinalizationReadiness:
    """What still blocks finalization, so the Reviewer can see their remaining
    work without having to attempt the action and read a 409."""
    blockers = service.check_blockers(audit_id, actor)
    return FinalizationReadiness(
        ready=not blockers,
        blocking_requirements=[
            BlockingRequirement(
                scoped_control_id=b.scoped_control_id,
                control_id=b.control_id,
                reason=b.reason,
            )
            for b in blockers
        ],
    )


@router.post(
    "/api/audits/{audit_id}/finalize",
    response_model=FinalizeResponse,
    responses={
        403: {"model": ErrorResponse, "description": "FORBIDDEN — Reviewer role only"},
        409: {"model": ErrorResponse, "description": "UNRESOLVED_FINDINGS | ALREADY_FINALIZED"},
    },
)
def finalize_audit(
    audit_id: uuid.UUID,
    actor: CurrentActor,
    service: Finalization,
    db: Annotated[DBSession, Depends(get_db)],
) -> FinalizeResponse:
    """Reviewer-only, and never automatic.

    The role check lives in the service, not in a route-level gate, so the rule
    holds for any caller. 04_API_CONTRACT.md requires the 403 "even if an
    Auditor somehow gets a finalize button rendered client-side" — the client's
    view of its own permissions is not part of this decision.
    """
    report = service.finalize(audit_id, actor)
    db.commit()
    return FinalizeResponse(report_id=report.id, audit_status="finalized")


@router.get("/api/audits/{audit_id}/report")
def get_report(
    audit_id: uuid.UUID,
    actor: CurrentActor,
    service: Finalization,
    format: Annotated[Literal["json", "pdf"], Query()] = "json",
) -> Response:
    """Retrieve the finalized report snapshot, or its PDF export.

    The PDF is rendered from the stored snapshot rather than from live tables,
    so re-downloading an old report always produces the same document.
    """
    report = service.get_report(audit_id, actor)

    if format == "pdf":
        pdf = render_report_pdf(report.snapshot_data)
        client_name = report.snapshot_data["audit"]["client_name"]
        safe_name = "".join(c for c in client_name if c.isalnum() or c in " -_").strip()
        return Response(
            content=pdf,
            media_type="application/pdf",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="PCI-DSS-Report-{safe_name or "audit"}.pdf"'
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )

    from fastapi.responses import JSONResponse

    return JSONResponse(content=ReportResponse.model_validate(report).model_dump(mode="json"))
