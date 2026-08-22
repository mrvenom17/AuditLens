"""ScopedRequirement and EvidenceRequest data access, and corpus lookups."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.orm import Session as DBSession

from app.models.corpus import PCIRequirement
from app.models.enums import EvidenceRequestStatus, ScopeSource
from app.models.scoping import EvidenceRequest, ScopedRequirement
from app.repositories.base import EngagementScopedRepository

if TYPE_CHECKING:
    from app.api.deps import Actor


class CorpusRepository:
    """Firm-wide reference data — not engagement-scoped, so no Actor is needed
    (03_DATA_MODEL.md → PCIRequirement: Ownership Rules N/A)."""

    def __init__(self, db: DBSession) -> None:
        self._db = db

    def current_version(self) -> str | None:
        """The newest corpus version present, used when a new engagement is
        scoped. Past engagements keep citing whatever version they used."""
        return self._db.scalar(
            select(PCIRequirement.corpus_version)
            .order_by(PCIRequirement.corpus_version.desc())
            .limit(1)
        )

    def list_by_version(self, version: str) -> list[PCIRequirement]:
        return list(
            self._db.scalars(
                select(PCIRequirement)
                .where(PCIRequirement.corpus_version == version)
                .order_by(PCIRequirement.requirement_family, PCIRequirement.clause_id)
            ).all()
        )

    def get_by_clause_ids(self, clause_ids: list[str], version: str) -> list[PCIRequirement]:
        if not clause_ids:
            return []
        return list(
            self._db.scalars(
                select(PCIRequirement).where(
                    PCIRequirement.clause_id.in_(clause_ids),
                    PCIRequirement.corpus_version == version,
                )
            ).all()
        )

    def get(self, requirement_id: uuid.UUID) -> PCIRequirement | None:
        return self._db.get(PCIRequirement, requirement_id)


class ScopedRequirementRepository(EngagementScopedRepository):
    def list_for_engagement(
        self, engagement_id: uuid.UUID, actor: Actor, *, confirmed_only: bool = False
    ) -> list[ScopedRequirement]:
        stmt = select(ScopedRequirement).where(ScopedRequirement.engagement_id == engagement_id)
        if confirmed_only:
            stmt = stmt.where(ScopedRequirement.confirmed.is_(True))
        stmt = self._scoped(stmt, ScopedRequirement.engagement_id, actor)
        return list(self._db.scalars(stmt.order_by(ScopedRequirement.created_at)).all())

    def get_scoped(self, scoped_id: uuid.UUID, actor: Actor) -> ScopedRequirement | None:
        """Fetch by id *and* engagement access in one statement — the id alone
        is never authorization (03_DATA_MODEL.md §8.2)."""
        stmt = self._scoped(
            select(ScopedRequirement).where(ScopedRequirement.id == scoped_id),
            ScopedRequirement.engagement_id,
            actor,
        )
        return self._db.scalar(stmt)

    def exists_unscoped(self, scoped_id: uuid.UUID) -> bool:
        """Existence probe for the 403-vs-404 split. Selects the id only, so no
        engagement content is read for an unauthorized caller."""
        return (
            self._db.scalar(select(ScopedRequirement.id).where(ScopedRequirement.id == scoped_id))
            is not None
        )

    def create(
        self,
        *,
        engagement_id: uuid.UUID,
        pci_requirement_id: uuid.UUID,
        source: ScopeSource,
        rationale: str | None,
        confirmed: bool = False,
    ) -> ScopedRequirement:
        scoped = ScopedRequirement(
            engagement_id=engagement_id,
            pci_requirement_id=pci_requirement_id,
            source=source,
            rationale=rationale,
            confirmed=confirmed,
        )
        self._db.add(scoped)
        self._db.flush()
        return scoped

    def existing_requirement_ids(self, engagement_id: uuid.UUID) -> set[uuid.UUID]:
        return set(
            self._db.scalars(
                select(ScopedRequirement.pci_requirement_id).where(
                    ScopedRequirement.engagement_id == engagement_id
                )
            ).all()
        )

    def clear_unconfirmed_suggestions(self, engagement_id: uuid.UUID) -> int:
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
                delete(ScopedRequirement).where(
                    ScopedRequirement.engagement_id == engagement_id,
                    ScopedRequirement.source == ScopeSource.ai_suggested,
                    ScopedRequirement.confirmed.is_(False),
                )
            ),
        )
        self._db.flush()
        return int(result.rowcount or 0)

    def count_confirmed(self, engagement_id: uuid.UUID) -> int:
        return int(
            self._db.scalar(
                select(func.count())
                .select_from(ScopedRequirement)
                .where(
                    ScopedRequirement.engagement_id == engagement_id,
                    ScopedRequirement.confirmed.is_(True),
                )
            )
            or 0
        )


class EvidenceRequestRepository(EngagementScopedRepository):
    def list_for_engagement(self, engagement_id: uuid.UUID, actor: Actor) -> list[EvidenceRequest]:
        stmt = self._scoped(
            select(EvidenceRequest).where(EvidenceRequest.engagement_id == engagement_id),
            EvidenceRequest.engagement_id,
            actor,
        )
        return list(self._db.scalars(stmt.order_by(EvidenceRequest.created_at)).all())

    def get_scoped(self, request_id: uuid.UUID, actor: Actor) -> EvidenceRequest | None:
        stmt = self._scoped(
            select(EvidenceRequest).where(EvidenceRequest.id == request_id),
            EvidenceRequest.engagement_id,
            actor,
        )
        return self._db.scalar(stmt)

    def create(
        self,
        *,
        engagement_id: uuid.UUID,
        scoped_requirement_id: uuid.UUID,
        description: str,
        description_source: str,
    ) -> EvidenceRequest:
        request = EvidenceRequest(
            engagement_id=engagement_id,
            scoped_requirement_id=scoped_requirement_id,
            description=description,
            status=EvidenceRequestStatus.draft,
            description_source=description_source,
        )
        self._db.add(request)
        self._db.flush()
        return request

    def scoped_requirement_ids_with_requests(self, engagement_id: uuid.UUID) -> set[uuid.UUID]:
        """Used to avoid duplicating requests on re-generation
        (01_REQUIREMENTS.md § Evidence Request Generation, Edge Cases)."""
        return set(
            self._db.scalars(
                select(EvidenceRequest.scoped_requirement_id).where(
                    EvidenceRequest.engagement_id == engagement_id
                )
            ).all()
        )
