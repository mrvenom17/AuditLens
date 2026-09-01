"""Audit-scoped query filtering — the system's ownership boundary (TASK-010).

05_SECURITY.md §10.1 rates cross-audit data access as the only *Critical*
threat in the model, and 03_DATA_MODEL.md §8.2 specifies exactly how it is
prevented: every audit-scoped query joins against `AuditAssignment`
(or checks for reviewer/admin) *in the query itself*, never by fetching rows and
filtering them in Python afterwards.

Everything in this module exists to make that the path of least resistance. A
repository method that forgets to scope does not silently return another
client's data — it fails to compile into a query at all, because
`scope_to_actor` is the only way to build the WHERE clause and it requires an
`Actor`.

Why filtering in Python is not an acceptable alternative, stated once here so it
is not re-litigated at each call site: a post-fetch filter means the rows were
already read, so any code path that logs, counts, paginates, aggregates, or
raises an exception before the filter runs leaks them. The filter must be in the
predicate the database evaluates.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, exists, select
from sqlalchemy.orm import Session as DBSession

from app.errors import ForbiddenError, NotFoundError
from app.logging_setup import log_admin_audit_access, log_authz_denial
from app.models.audit import Audit, AuditAssignment

if TYPE_CHECKING:
    from app.api.deps import Actor


def visible_audit_ids(actor: Actor) -> Select[tuple[uuid.UUID]]:
    """A subquery of the audit ids this actor may see.

    Reviewers and Admins see every audit (03_DATA_MODEL.md §8.2), so their
    subquery is unrestricted. An Auditor's is restricted to their assignments.
    Returning a Select rather than a list of ids is the point: it composes into
    the caller's query and is evaluated by Postgres, so no id ever round-trips
    through application memory to be compared there.
    """
    if actor.sees_all_audits:
        return select(Audit.id)
    return select(AuditAssignment.audit_id).where(AuditAssignment.user_id == actor.id)


def scope_to_actor[S: Select[Any]](stmt: S, audit_id_column: Any, actor: Actor) -> S:
    """Restrict a statement to audits the actor may access.

    `audit_id_column` is passed explicitly rather than inferred so that
    scoping a new entity is a deliberate act by whoever adds it. Every
    audit-scoped table has such a column by construction
    (03_DATA_MODEL.md §8.1).
    """
    if actor.sees_all_audits:
        return stmt
    return stmt.where(audit_id_column.in_(visible_audit_ids(actor)))


class AuditScopedRepository:
    """Base for every repository that touches audit-owned data.

    Subclasses get `_scoped` and `_require_access`; they are expected to use one
    of them in every read and every write.
    """

    def __init__(self, db: DBSession) -> None:
        self._db = db

    def _scoped[S: Select[Any]](self, stmt: S, audit_id_column: Any, actor: Actor) -> S:
        return scope_to_actor(stmt, audit_id_column, actor)

    def _require_access(self, audit_id: uuid.UUID, actor: Actor, *, action: str) -> None:
        """Assert the actor may act on this audit, or raise.

        The 403-vs-404 split follows 04_API_CONTRACT.md → GET /api/audits/{id}
        and ADR-011: 403 means the audit exists but this actor has no
        relationship to it; 404 means no such audit. That distinction is
        deliberate for single-tenant internal software, where existence-leakage
        to a colleague is not a meaningful disclosure and a truthful 403 is far
        easier to support.

        Note the ordering: the *scoped* check runs first and returns without
        reading any audit column. Only if it fails does the existence probe
        run, and that probe selects a literal rather than any row data — so no
        audit content is ever read for an actor who turns out to lack
        access.
        """
        if has_audit_access(self._db, audit_id, actor):
            if actor.is_admin:
                log_admin_audit_access(actor_id=str(actor.id), audit_id=str(audit_id))
            return

        audit_exists = self._db.scalar(select(exists().where(Audit.id == audit_id)))
        if not audit_exists:
            raise NotFoundError("Audit not found.")

        log_authz_denial(
            actor_id=str(actor.id),
            action=action,
            resource_type="audit",
            resource_id=str(audit_id),
        )
        raise ForbiddenError("You are not assigned to this audit.")


def has_audit_access(db: DBSession, audit_id: uuid.UUID, actor: Actor) -> bool:
    """Whether the actor may access this audit, decided in one query."""
    if actor.sees_all_audits:
        return bool(db.scalar(select(exists().where(Audit.id == audit_id))))
    return bool(
        db.scalar(
            select(
                exists().where(
                    AuditAssignment.audit_id == audit_id,
                    AuditAssignment.user_id == actor.id,
                )
            )
        )
    )
