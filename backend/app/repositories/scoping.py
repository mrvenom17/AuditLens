"""ScopedControl and EvidenceRequest data access, and corpus lookups."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.orm import Session as DBSession

from app.models.corpus import ControlDefinition
from app.models.enums import ApplicabilityStatus, EvidenceRequestStatus, ScopeSource
from app.models.scoping import EvidenceRequest, ScopedControl
from app.repositories.base import AuditScopedRepository

if TYPE_CHECKING:
    from app.api.deps import Actor


class CorpusRepository:
    """Firm-wide reference data — not audit-scoped, so no Actor is needed
    (03_DATA_MODEL.md → ControlDefinition: Ownership Rules N/A)."""

    def __init__(self, db: DBSession) -> None:
        self._db = db

    def current_version(self) -> str | None:
        """The newest corpus version present, used when a new audit is
        scoped. Past audits keep citing whatever version they used."""
        return self._db.scalar(
            select(ControlDefinition.corpus_version)
            .order_by(ControlDefinition.corpus_version.desc())
            .limit(1)
        )

    def list_by_version(self, version: str) -> list[ControlDefinition]:
        return list(
            self._db.scalars(
                select(ControlDefinition)
                .where(ControlDefinition.corpus_version == version)
                .order_by(ControlDefinition.requirement_family, ControlDefinition.control_id)
            ).all()
        )

    def get_by_clause_ids(self, clause_ids: list[str], version: str) -> list[ControlDefinition]:
        if not clause_ids:
            return []
        return list(
            self._db.scalars(
                select(ControlDefinition).where(
                    ControlDefinition.control_id.in_(clause_ids),
                    ControlDefinition.corpus_version == version,
                )
            ).all()
        )

    def get(self, requirement_id: uuid.UUID) -> ControlDefinition | None:
        return self._db.get(ControlDefinition, requirement_id)


class ScopedRequirementRepository(AuditScopedRepository):
    def list_for_audit(
        self, audit_id: uuid.UUID, actor: Actor, *, confirmed_only: bool = False
    ) -> list[ScopedControl]:
        stmt = select(ScopedControl).where(ScopedControl.audit_id == audit_id)
        if confirmed_only:
            stmt = stmt.where(ScopedControl.confirmed.is_(True))
        stmt = self._scoped(stmt, ScopedControl.audit_id, actor)
        return list(self._db.scalars(stmt.order_by(ScopedControl.created_at)).all())

    def get_scoped(self, scoped_id: uuid.UUID, actor: Actor) -> ScopedControl | None:
        """Fetch by id *and* audit access in one statement — the id alone
        is never authorization (03_DATA_MODEL.md §8.2)."""
        stmt = self._scoped(
            select(ScopedControl).where(ScopedControl.id == scoped_id),
            ScopedControl.audit_id,
            actor,
        )
        return self._db.scalar(stmt)

    def exists_unscoped(self, scoped_id: uuid.UUID) -> bool:
        """Existence probe for the 403-vs-404 split. Selects the id only, so no
        audit content is read for an unauthorized caller."""
        return (
            self._db.scalar(select(ScopedControl.id).where(ScopedControl.id == scoped_id))
            is not None
        )

    def create(
        self,
        *,
        audit_id: uuid.UUID,
        control_definition_id: uuid.UUID,
        source: ScopeSource,
        rationale: str | None,
        confirmed: bool = False,
        applicability_status: ApplicabilityStatus = ApplicabilityStatus.UNDETERMINED,
        applicability_evidence: list[dict[str, Any]] | None = None,
    ) -> ScopedControl:
        scoped = ScopedControl(
            audit_id=audit_id,
            control_definition_id=control_definition_id,
            source=source,
            rationale=rationale,
            confirmed=confirmed,
            applicability_status=applicability_status,
            applicability_evidence=applicability_evidence,
        )
        self._db.add(scoped)
        self._db.flush()
        return scoped

    def list_all_for_audit(self, audit_id: uuid.UUID) -> list[ScopedControl]:
        """Every scope row for an audit, unfiltered by actor.

        Internal read for the applicability pass, which runs as the system rather
        than as a user. Callers reach it only from a context that has already
        established access to the audit.
        """
        return list(
            self._db.scalars(select(ScopedControl).where(ScopedControl.audit_id == audit_id)).all()
        )

    def existing_requirement_ids(self, audit_id: uuid.UUID) -> set[uuid.UUID]:
        return set(
            self._db.scalars(
                select(ScopedControl.control_definition_id).where(
                    ScopedControl.audit_id == audit_id
                )
            ).all()
        )

    def clear_unconfirmed_suggestions(self, audit_id: uuid.UUID) -> int:
        """Re-running scope suggestion replaces prior `ai_suggested,
        confirmed=false` rows and never touches rows already confirmed
        (04_API_CONTRACT.md → scope-suggestion, Idempotency).

        The `confirmed=false` predicate is the entire safety property here: a
        confirmed row represents a human decision, and deleting one would
        silently discard it.
        """
        # `Session.execute` is typed as returning Result; a DELETE always
        # yields a CursorResult, which is where `rowcount` lives.
        result = cast(
            "CursorResult[Any]",
            self._db.execute(
                delete(ScopedControl).where(
                    ScopedControl.audit_id == audit_id,
                    ScopedControl.source == ScopeSource.ai_suggested,
                    ScopedControl.confirmed.is_(False),
                )
            ),
        )
        self._db.flush()
        return int(result.rowcount or 0)

    def count_confirmed(self, audit_id: uuid.UUID) -> int:
        return int(
            self._db.scalar(
                select(func.count())
                .select_from(ScopedControl)
                .where(
                    ScopedControl.audit_id == audit_id,
                    ScopedControl.confirmed.is_(True),
                )
            )
            or 0
        )


class EvidenceRequestRepository(AuditScopedRepository):
    def list_for_audit(self, audit_id: uuid.UUID, actor: Actor) -> list[EvidenceRequest]:
        stmt = self._scoped(
            select(EvidenceRequest).where(EvidenceRequest.audit_id == audit_id),
            EvidenceRequest.audit_id,
            actor,
        )
        return list(self._db.scalars(stmt.order_by(EvidenceRequest.created_at)).all())

    def get_scoped(self, request_id: uuid.UUID, actor: Actor) -> EvidenceRequest | None:
        stmt = self._scoped(
            select(EvidenceRequest).where(EvidenceRequest.id == request_id),
            EvidenceRequest.audit_id,
            actor,
        )
        return self._db.scalar(stmt)

    def create(
        self,
        *,
        audit_id: uuid.UUID,
        scoped_control_id: uuid.UUID,
        description: str,
        description_source: str,
    ) -> EvidenceRequest:
        request = EvidenceRequest(
            audit_id=audit_id,
            scoped_control_id=scoped_control_id,
            description=description,
            status=EvidenceRequestStatus.draft,
            description_source=description_source,
        )
        self._db.add(request)
        self._db.flush()
        return request

    def scoped_requirement_ids_with_requests(self, audit_id: uuid.UUID) -> set[uuid.UUID]:
        """Used to avoid duplicating requests on re-generation
        (01_REQUIREMENTS.md § Evidence Request Generation, Edge Cases)."""
        return set(
            self._db.scalars(
                select(EvidenceRequest.scoped_control_id).where(
                    EvidenceRequest.audit_id == audit_id
                )
            ).all()
        )
