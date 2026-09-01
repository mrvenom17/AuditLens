"""Audit and assignment data access.

Every read here is scoped through `AuditScopedRepository`. Where a method
looks like it fetches by primary key, note that the primary key is combined with
the scope filter in one statement — a resource ID in a URL is never sufficient
authorization on its own (03_DATA_MODEL.md §8.2).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.models.audit import Audit, AuditAssignment, ClientProfileDocument
from app.models.enums import AuditStatus, EntityType, FindingStatus, MerchantLevel
from app.repositories.base import AuditScopedRepository

if TYPE_CHECKING:
    from app.api.deps import Actor


class AuditRepository(AuditScopedRepository):
    def create(
        self,
        *,
        client_name: str,
        entity_type: EntityType,
        merchant_level: MerchantLevel | None,
        annual_transaction_volume: int | None,
        existing_saq_type: str | None,
        tech_stack_summary: str | None,
        company_profile: dict[str, Any],
        created_by: uuid.UUID,
    ) -> Audit:
        audit = Audit(
            client_name=client_name,
            entity_type=entity_type,
            merchant_level=merchant_level,
            annual_transaction_volume=annual_transaction_volume,
            existing_saq_type=existing_saq_type,
            tech_stack_summary=tech_stack_summary,
            company_profile=company_profile,
            status=AuditStatus.intake,
            created_by=created_by,
        )
        self._db.add(audit)
        self._db.flush()
        return audit

    def get_scoped(self, audit_id: uuid.UUID, actor: Actor) -> Audit | None:
        """Fetch by id *and* access in a single statement.

        This is the method that makes the ownership rule cheap to obey: there is
        no unscoped `get` on this repository to reach for by accident.
        """
        stmt = self._scoped(select(Audit).where(Audit.id == audit_id), Audit.id, actor)
        return self._db.scalar(stmt)

    def list_scoped(
        self,
        actor: Actor,
        *,
        status: AuditStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Audit], int]:
        stmt = select(Audit)
        count_stmt = select(func.count()).select_from(Audit)

        if status is not None:
            stmt = stmt.where(Audit.status == status)
            count_stmt = count_stmt.where(Audit.status == status)

        # The count is scoped identically to the page. A total computed without
        # the filter would disclose how many audits exist firm-wide.
        stmt = self._scoped(stmt, Audit.id, actor)
        count_stmt = self._scoped(count_stmt, Audit.id, actor)

        stmt = stmt.order_by(Audit.created_at.desc()).limit(limit).offset(offset)
        total = int(self._db.scalar(count_stmt) or 0)
        return list(self._db.scalars(stmt).all()), total

    def set_status(self, audit: Audit, status: AuditStatus) -> None:
        audit.status = status
        self._db.flush()

    def counts(self, audit_id: uuid.UUID) -> dict[str, int]:
        """The queue summary 04_API_CONTRACT.md requires on the detail response.

        No scope filter here on purpose: the caller has already passed the
        access check for this audit, and adding a second filter would imply
        the id could arrive unchecked — which it cannot, because every route
        reaches this through `AuditService.get`.
        """
        from app.models.evidence import EvidenceDocument
        from app.models.finding import Finding
        from app.models.scoping import EvidenceRequest, ScopedControl

        def _count(model: Any, *conditions: Any) -> int:
            # `model` is any audit-scoped ORM class; every one of them has
            # an `audit_id` column by construction (03_DATA_MODEL.md §8.1).
            stmt = (
                select(func.count())
                .select_from(model)
                .where(model.audit_id == audit_id, *conditions)
            )
            return int(self._db.scalar(stmt) or 0)

        return {
            "scoped_controls": _count(ScopedControl),
            "confirmed_requirements": _count(ScopedControl, ScopedControl.confirmed.is_(True)),
            "evidence_requests": _count(EvidenceRequest),
            "evidence_documents": _count(EvidenceDocument),
            "findings_total": _count(Finding),
            "findings_pending_review": _count(
                Finding, Finding.status == FindingStatus.pending_review
            ),
            "findings_approved": _count(Finding, Finding.status == FindingStatus.approved),
            "findings_rejected": _count(Finding, Finding.status == FindingStatus.rejected),
            "findings_needing_more_evidence": _count(
                Finding, Finding.status == FindingStatus.needs_more_evidence
            ),
        }

    # --- Assignments ---------------------------------------------------------

    def assign(self, audit_id: uuid.UUID, user_id: uuid.UUID) -> AuditAssignment:
        assignment = AuditAssignment(audit_id=audit_id, user_id=user_id)
        self._db.add(assignment)
        self._db.flush()
        return assignment

    def get_assignment(self, audit_id: uuid.UUID, user_id: uuid.UUID) -> AuditAssignment | None:
        return self._db.scalar(
            select(AuditAssignment).where(
                AuditAssignment.audit_id == audit_id,
                AuditAssignment.user_id == user_id,
            )
        )

    def list_assignments(self, audit_id: uuid.UUID) -> list[AuditAssignment]:
        return list(
            self._db.scalars(
                select(AuditAssignment).where(AuditAssignment.audit_id == audit_id)
            ).all()
        )

    def remove_assignment(self, assignment: AuditAssignment) -> None:
        """The one deletion in the system.

        Permitted because an assignment is an access-control record, not an
        audit record — the actions the user took while assigned remain attributed
        to them via `created_by`, `uploaded_by`, `reviewed_by` and FindingHistory,
        none of which this touches.
        """
        self._db.delete(assignment)
        self._db.flush()


class ClientProfileDocumentRepository:
    """Firm-internal client-file documents (ADR-011 item 6).

    Not audit-scoped: these are the firm's own records, readable by any
    authenticated staff member in this single-tenant deployment.
    """

    def __init__(self, db: DBSession) -> None:
        self._db = db

    def create(
        self,
        *,
        original_filename: str,
        content_hash: str,
        storage_path: str,
        mime_type: str,
        uploaded_by: uuid.UUID,
    ) -> ClientProfileDocument:
        document = ClientProfileDocument(
            original_filename=original_filename,
            content_hash=content_hash,
            storage_path=storage_path,
            mime_type=mime_type,
            uploaded_by=uploaded_by,
        )
        self._db.add(document)
        self._db.flush()
        return document

    def get(self, document_id: uuid.UUID) -> ClientProfileDocument | None:
        return self._db.get(ClientProfileDocument, document_id)

    def count_existing(self, document_ids: list[uuid.UUID]) -> int:
        """Used to validate `source_document_ids` at audit creation.

        04_API_CONTRACT.md describes this as a defensive check against
        ID-guessing rather than a cross-tenant boundary — there is one firm.
        """
        if not document_ids:
            return 0
        return int(
            self._db.scalar(
                select(func.count())
                .select_from(ClientProfileDocument)
                .where(ClientProfileDocument.id.in_(document_ids))
            )
            or 0
        )
