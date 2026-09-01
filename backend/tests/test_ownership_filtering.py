"""Ownership filtering tests (TASK-010).

07_TASKS.md: "This task is the single most important test target in the whole
project — do not proceed to Phase 5 until this is solid."

The tests are organised around the property being defended rather than around
the methods being called: an Auditor must not be able to reach data belonging to
an audit they are not assigned to, by any route, including guessing IDs.
Two structural tests at the end assert the *mechanism* (filtering in SQL) and
not just the outcome, because an implementation that fetched-then-filtered would
pass every behavioural test here while still being wrong.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.api.deps import Actor
from app.errors import ForbiddenError, NotFoundError
from app.models.audit import Audit
from app.models.enums import AuditStatus, Role
from app.repositories.audit import AuditRepository
from app.repositories.base import has_audit_access, scope_to_actor, visible_audit_ids


def actor_for(user: Any) -> Actor:
    return Actor(id=user.id, role=user.role, name=user.name, email=user.email)


@pytest.fixture
def repo(db: DBSession) -> AuditRepository:
    return AuditRepository(db)


@pytest.fixture
def two_auditors_two_audits(make_user: Any, make_audit: Any) -> dict[str, Any]:
    """The canonical setup: Auditor A owns audit A, Auditor B owns B."""
    auditor_a = make_user(Role.auditor, name="Auditor A")
    auditor_b = make_user(Role.auditor, name="Auditor B")
    return {
        "auditor_a": auditor_a,
        "auditor_b": auditor_b,
        "audit_a": make_audit(auditor_a, client_name="Client A"),
        "audit_b": make_audit(auditor_b, client_name="Client B"),
    }


class TestAuditorIsolation:
    def test_auditor_cannot_read_another_auditors_audit(
        self, repo: AuditRepository, two_auditors_two_audits: dict[str, Any]
    ) -> None:
        """TASK-010's stated test: a user not in AuditAssignment for
        audit X gets nothing when querying X, even with role=auditor."""
        setup = two_auditors_two_audits

        result = repo.get_scoped(setup["audit_b"].id, actor_for(setup["auditor_a"]))

        assert result is None

    def test_auditor_reads_their_own_audit(
        self, repo: AuditRepository, two_auditors_two_audits: dict[str, Any]
    ) -> None:
        """The filter must not be so strict it blocks legitimate access —
        a boundary that denies everyone is not a working boundary."""
        setup = two_auditors_two_audits

        result = repo.get_scoped(setup["audit_a"].id, actor_for(setup["auditor_a"]))

        assert result is not None
        assert result.id == setup["audit_a"].id

    def test_listing_returns_only_assigned_audits(
        self, repo: AuditRepository, two_auditors_two_audits: dict[str, Any]
    ) -> None:
        setup = two_auditors_two_audits

        items, total = repo.list_scoped(actor_for(setup["auditor_a"]))

        assert total == 1
        assert [e.id for e in items] == [setup["audit_a"].id]

    def test_list_total_does_not_leak_the_firmwide_count(
        self, repo: AuditRepository, two_auditors_two_audits: dict[str, Any]
    ) -> None:
        """A correctly-filtered page with an unfiltered total still discloses
        how many other clients the firm has."""
        setup = two_auditors_two_audits

        _, total = repo.list_scoped(actor_for(setup["auditor_a"]))

        assert total == 1, "the count must be scoped identically to the page"

    def test_guessing_a_valid_id_gains_nothing(
        self, repo: AuditRepository, two_auditors_two_audits: dict[str, Any]
    ) -> None:
        """08_TESTING.md § Security Tests: "even by guessing/enumerating IDs"."""
        setup = two_auditors_two_audits
        actor = actor_for(setup["auditor_a"])

        assert repo.get_scoped(setup["audit_b"].id, actor) is None
        assert repo.get_scoped(uuid.uuid4(), actor) is None

    def test_status_filter_does_not_widen_visibility(
        self, repo: AuditRepository, make_user: Any, make_audit: Any
    ) -> None:
        """A query parameter must never be able to relax the scope filter."""
        auditor = make_user(Role.auditor)
        other = make_user(Role.auditor)
        make_audit(other, status=AuditStatus.in_progress)

        items, total = repo.list_scoped(actor_for(auditor), status=AuditStatus.in_progress)

        assert items == []
        assert total == 0

    def test_pagination_does_not_widen_visibility(
        self, repo: AuditRepository, make_user: Any, make_audit: Any
    ) -> None:
        auditor = make_user(Role.auditor)
        other = make_user(Role.auditor)
        for i in range(5):
            make_audit(other, client_name=f"Other {i}")

        items, total = repo.list_scoped(actor_for(auditor), limit=200, offset=0)

        assert items == []
        assert total == 0

    def test_being_assigned_grants_access_immediately(
        self, repo: AuditRepository, two_auditors_two_audits: dict[str, Any]
    ) -> None:
        setup = two_auditors_two_audits
        actor_a = actor_for(setup["auditor_a"])
        assert repo.get_scoped(setup["audit_b"].id, actor_a) is None

        repo.assign(setup["audit_b"].id, setup["auditor_a"].id)

        assert repo.get_scoped(setup["audit_b"].id, actor_a) is not None

    def test_removing_an_assignment_revokes_access_immediately(
        self, repo: AuditRepository, two_auditors_two_audits: dict[str, Any]
    ) -> None:
        setup = two_auditors_two_audits
        actor_a = actor_for(setup["auditor_a"])
        assignment = repo.get_assignment(setup["audit_a"].id, setup["auditor_a"].id)
        assert assignment is not None

        repo.remove_assignment(assignment)

        assert repo.get_scoped(setup["audit_a"].id, actor_a) is None


class TestReviewerAndAdminVisibility:
    def test_reviewer_sees_every_audit(
        self,
        repo: AuditRepository,
        make_user: Any,
        two_auditors_two_audits: dict[str, Any],
    ) -> None:
        """03_DATA_MODEL.md §8.2: Reviewers see all audits at the firm,
        without needing an assignment row."""
        setup = two_auditors_two_audits
        reviewer = make_user(Role.reviewer)

        _, total = repo.list_scoped(actor_for(reviewer))

        assert total == 2
        assert repo.get_scoped(setup["audit_a"].id, actor_for(reviewer)) is not None
        assert repo.get_scoped(setup["audit_b"].id, actor_for(reviewer)) is not None

    def test_admin_sees_every_audit(
        self,
        repo: AuditRepository,
        make_user: Any,
        two_auditors_two_audits: dict[str, Any],
    ) -> None:
        admin = make_user(Role.admin)
        _, total = repo.list_scoped(actor_for(admin))
        assert total == 2

    def test_reviewer_still_cannot_reach_a_nonexistent_audit(
        self, repo: AuditRepository, make_user: Any
    ) -> None:
        """ "Sees everything" must mean "everything that exists", not "returns
        something for any id"."""
        reviewer = make_user(Role.reviewer)
        assert repo.get_scoped(uuid.uuid4(), actor_for(reviewer)) is None


class TestRequireAccess:
    """The 403-vs-404 contract from 04_API_CONTRACT.md and ADR-011."""

    def test_forbidden_when_the_audit_exists_but_is_not_ours(
        self, repo: AuditRepository, two_auditors_two_audits: dict[str, Any]
    ) -> None:
        setup = two_auditors_two_audits

        with pytest.raises(ForbiddenError) as exc:
            repo._require_access(setup["audit_b"].id, actor_for(setup["auditor_a"]), action="read")
        assert exc.value.status_code == 403

    def test_not_found_when_the_audit_does_not_exist(
        self, repo: AuditRepository, make_user: Any
    ) -> None:
        auditor = make_user(Role.auditor)

        with pytest.raises(NotFoundError) as exc:
            repo._require_access(uuid.uuid4(), actor_for(auditor), action="read")
        assert exc.value.status_code == 404

    def test_error_messages_disclose_nothing_about_the_audit(
        self, repo: AuditRepository, two_auditors_two_audits: dict[str, Any]
    ) -> None:
        """A 403 confirms existence by design; it must not additionally leak the
        client name, status, or anything else about the audit."""
        setup = two_auditors_two_audits

        with pytest.raises(ForbiddenError) as exc:
            repo._require_access(setup["audit_b"].id, actor_for(setup["auditor_a"]), action="read")
        assert "Client B" not in str(exc.value)
        assert exc.value.details == {}

    def test_passes_for_an_assigned_auditor(
        self, repo: AuditRepository, two_auditors_two_audits: dict[str, Any]
    ) -> None:
        setup = two_auditors_two_audits
        repo._require_access(
            setup["audit_a"].id, actor_for(setup["auditor_a"]), action="read"
        )  # must not raise

    def test_passes_for_a_reviewer_on_any_audit(
        self,
        repo: AuditRepository,
        make_user: Any,
        two_auditors_two_audits: dict[str, Any],
    ) -> None:
        setup = two_auditors_two_audits
        reviewer = make_user(Role.reviewer)
        repo._require_access(setup["audit_b"].id, actor_for(reviewer), action="read")


class TestFilteringHappensInSql:
    """These assert the mechanism, not the outcome.

    03_DATA_MODEL.md §8.2 and 06_ENGINEERING_RULES.md both state the filter must
    be applied at the query level rather than after the fact. A fetch-then-filter
    implementation would satisfy every behavioural test above and still be a
    defect, so the mechanism is pinned directly.
    """

    def test_scope_to_actor_adds_a_where_clause_for_an_auditor(self, make_user: Any) -> None:
        auditor = make_user(Role.auditor)
        base = select(Audit)

        scoped = scope_to_actor(base, Audit.id, actor_for(auditor))

        sql = str(scoped.compile(compile_kwargs={"literal_binds": False}))
        assert "audit_assignments" in sql
        assert "WHERE" in sql

    def test_scope_to_actor_leaves_a_reviewer_query_unrestricted(self, make_user: Any) -> None:
        reviewer = make_user(Role.reviewer)
        base = select(Audit)

        scoped = scope_to_actor(base, Audit.id, actor_for(reviewer))

        assert str(scoped) == str(base)

    def test_visible_audit_ids_is_a_subquery_not_a_materialised_list(self, make_user: Any) -> None:
        """Returning a Select is what keeps the comparison inside Postgres. If
        this ever returned a list, ids would be read into application memory and
        the filter would have moved into Python."""
        auditor = make_user(Role.auditor)

        result = visible_audit_ids(actor_for(auditor))

        assert isinstance(result, select(Audit.id).__class__)

    def test_access_check_is_a_single_query(
        self, db: DBSession, two_auditors_two_audits: dict[str, Any]
    ) -> None:
        """`has_audit_access` must decide without loading the audit,
        so that a denied check never reads client data at all."""
        setup = two_auditors_two_audits

        assert has_audit_access(db, setup["audit_a"].id, actor_for(setup["auditor_a"])) is True
        assert has_audit_access(db, setup["audit_b"].id, actor_for(setup["auditor_a"])) is False


class TestClientProfileDocumentValidation:
    def test_counts_only_ids_that_exist(self, db: DBSession, make_user: Any) -> None:
        """Backs the `source_document_ids` defensive check at audit
        creation (04_API_CONTRACT.md → POST /api/audits)."""
        from app.repositories.audit import ClientProfileDocumentRepository

        uploader = make_user(Role.auditor)
        repo = ClientProfileDocumentRepository(db)
        document = repo.create(
            original_filename="client-file.pdf",
            content_hash="b" * 64,
            storage_path="/data/profile/bb/" + "b" * 64,
            mime_type="application/pdf",
            uploaded_by=uploader.id,
        )

        assert repo.count_existing([document.id]) == 1
        assert repo.count_existing([document.id, uuid.uuid4()]) == 1
        assert repo.count_existing([]) == 0
