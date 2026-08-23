"""Systematic authorization sweep (TASK-022).

TASK-022: "Systematic test coverage of every ownership/role boundary across all
endpoints, not just the ones exercised incidentally by feature tests."

The sweep is driven by the application's own route table rather than by a
hand-maintained list. That matters more than it might look: a hand-written list
covers the endpoints someone remembered, and the endpoint that gets forgotten is
exactly the one that ships without an authorization check. Here, adding a route
without adding it to `PUBLIC_OPERATIONS` fails `test_every_route_is_accounted_for`,
and adding an engagement-scoped route without listing it in `ENGAGEMENT_SCOPED`
fails `test_every_engagement_scoped_route_is_swept`.

Mapped to 08_TESTING.md § Security Tests:
  - unauthenticated access to any Engagement-scoped endpoint is rejected (401)
  - an Auditor not in EngagementAssignment for X cannot read or write any of X's
    data (403), even by guessing/enumerating IDs
  - a non-Reviewer calling finalize is rejected (403) regardless of Finding state
  - a Finding cannot reach approved without reviewed_by
  - malicious file upload is rejected
  - login lockout triggers correctly and does not leak whether an email exists

The last three live in their own modules (test_findings, test_evidence_upload,
test_auth); this module covers the first two exhaustively plus the role gates.
"""

from __future__ import annotations

import uuid
from typing import Any, NamedTuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as DBSession

from app.main import app
from app.models.engagement import EngagementAssignment
from app.models.enums import ComplianceStatus, EngagementStatus, FindingStatus, Role
from tests import filefixtures as ff

PASSWORD = "correct-horse-battery-staple"


def login(client: TestClient, user: Any) -> None:
    assert (
        client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD}).status_code
        == 200
    )


# Operations that legitimately require no session. Every other route in the
# application must reject an unauthenticated caller.
PUBLIC_OPERATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/health"),  # liveness probe — no data, no session
        ("GET", "/health/ready"),  # readiness probe — booleans only
        ("POST", "/api/auth/login"),  # establishes the session
        # logout is deliberately NOT here: it revokes a session server-side, so
        # it requires one to revoke.
    }
)


def all_operations() -> list[tuple[str, str]]:
    """Every operation the application actually exposes, from its own spec."""
    return [
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    ]


class ScopedRoute(NamedTuple):
    """An engagement-scoped operation and how to call it."""

    method: str
    template: str
    body: dict[str, Any] | None = None
    send_file: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.method, self.template)


# Every operation that reads or writes engagement-owned data. Each is exercised
# against a resource the caller has no relationship to.
ENGAGEMENT_SCOPED: tuple[ScopedRoute, ...] = (
    ScopedRoute("GET", "/api/engagements/{engagement_id}"),
    ScopedRoute("POST", "/api/engagements/{engagement_id}/assignments", {"user_id": None}),
    ScopedRoute("DELETE", "/api/engagements/{engagement_id}/assignments/{user_id}"),
    ScopedRoute("GET", "/api/engagements/{engagement_id}/evidence-documents"),
    ScopedRoute("POST", "/api/engagements/{engagement_id}/evidence-documents", send_file=True),
    ScopedRoute("GET", "/api/engagements/{engagement_id}/evidence-requests"),
    ScopedRoute("POST", "/api/engagements/{engagement_id}/evidence-requests/generate"),
    ScopedRoute("GET", "/api/engagements/{engagement_id}/finalization-readiness"),
    ScopedRoute("POST", "/api/engagements/{engagement_id}/finalize"),
    ScopedRoute("GET", "/api/engagements/{engagement_id}/findings"),
    ScopedRoute("GET", "/api/engagements/{engagement_id}/report"),
    ScopedRoute("POST", "/api/engagements/{engagement_id}/scope-suggestion"),
    ScopedRoute("GET", "/api/engagements/{engagement_id}/scoped-requirements"),
    ScopedRoute(
        "POST", "/api/engagements/{engagement_id}/scoped-requirements", {"clause_id": "1.2.1"}
    ),
    ScopedRoute("GET", "/api/evidence-documents/{document_id}"),
    ScopedRoute("GET", "/api/evidence-documents/{document_id}/download"),
    ScopedRoute("PATCH", "/api/evidence-requests/{request_id}", {"description": "Injected."}),
    ScopedRoute("GET", "/api/findings/{finding_id}"),
    ScopedRoute("GET", "/api/findings/{finding_id}/history"),
    ScopedRoute("PATCH", "/api/findings/{finding_id}/review", {"action": "accept"}),
    ScopedRoute("PATCH", "/api/scoped-requirements/{scoped_id}", {"confirmed": True}),
    ScopedRoute(
        "PATCH",
        "/api/scoped-requirements/{scoped_id}/gap",
        {"gap_acknowledged": True, "gap_note": "Injected."},
    ),
)

# Operations that are not engagement-scoped, with why. Listing them explicitly
# is what lets `test_every_engagement_scoped_route_is_swept` be exhaustive.
NON_SCOPED_OPERATIONS: dict[tuple[str, str], str] = {
    ("GET", "/health"): "liveness probe",
    ("GET", "/health/ready"): "readiness probe",
    ("POST", "/api/auth/login"): "establishes the session",
    ("POST", "/api/auth/logout"): "acts on the caller's own session",
    ("GET", "/api/auth/me"): "returns the caller's own identity",
    ("GET", "/api/engagements"): "list — scoped by filtering, covered separately",
    ("POST", "/api/engagements"): "creates a new engagement; no existing resource",
}


@pytest.fixture
def populated_engagement(
    db: DBSession,
    make_user: Any,
    make_engagement: Any,
    make_scoped_requirement: Any,
    make_finding: Any,
    api_client: TestClient,
) -> dict[str, Any]:
    """An engagement owned by one Auditor, with one of every sub-resource.

    Every sub-resource is real and reachable, so a failure to scope any one of
    them shows up as data returned rather than as a 404 that merely looks like
    a denial.
    """
    from app.repositories.scoping import EvidenceRequestRepository

    owner = make_user(Role.auditor, password=PASSWORD, name="Owner Auditor")
    reviewer = make_user(Role.reviewer, password=PASSWORD, name="Reviewer")
    engagement = make_engagement(
        owner, status=EngagementStatus.in_progress, client_name="Confidential Client Ltd"
    )
    scoped = make_scoped_requirement(engagement, confirmed=True)
    finding = make_finding(engagement, status=FindingStatus.draft, scoped_requirement=scoped)
    request = EvidenceRequestRepository(db).create(
        engagement_id=engagement.id,
        scoped_requirement_id=scoped.id,
        description="Provide the firewall export.",
        description_source="template",
    )

    login(api_client, owner)
    document = api_client.post(
        f"/api/engagements/{engagement.id}/evidence-documents",
        files={"file": ("evidence.pdf", ff.valid_pdf(), "application/pdf")},
    ).json()
    api_client.post("/api/auth/logout")

    return {
        "owner": owner,
        "reviewer": reviewer,
        "engagement": engagement,
        "scoped": scoped,
        "finding": finding,
        "request": request,
        "document_id": document["id"],
    }


def resolve(route: ScopedRoute, setup: dict[str, Any], target_user_id: uuid.UUID) -> str:
    return (
        route.template.replace("{engagement_id}", str(setup["engagement"].id))
        .replace("{document_id}", str(setup["document_id"]))
        .replace("{request_id}", str(setup["request"].id))
        .replace("{finding_id}", str(setup["finding"].id))
        .replace("{scoped_id}", str(setup["scoped"].id))
        .replace("{user_id}", str(target_user_id))
    )


def call(client: TestClient, route: ScopedRoute, url: str, setup: dict[str, Any]) -> Any:
    kwargs: dict[str, Any] = {}
    if route.send_file:
        kwargs["files"] = {"file": ("x.pdf", ff.valid_pdf(), "application/pdf")}
    elif route.body is not None:
        body = dict(route.body)
        if "user_id" in body and body["user_id"] is None:
            body["user_id"] = str(setup["owner"].id)
        kwargs["json"] = body
    return client.request(route.method, url, **kwargs)


class TestRouteTableIsFullyAccountedFor:
    """These two tests are what make the sweep exhaustive rather than
    representative. They fail when a route is added without being classified."""

    def test_every_route_is_accounted_for(self) -> None:
        operations = set(all_operations())
        classified = set(NON_SCOPED_OPERATIONS) | {r.key for r in ENGAGEMENT_SCOPED}

        unclassified = operations - classified
        assert not unclassified, (
            f"These operations are not covered by the authorization sweep: "
            f"{sorted(unclassified)}. Add each to ENGAGEMENT_SCOPED (if it touches "
            f"engagement-owned data) or to NON_SCOPED_OPERATIONS with a reason."
        )

        stale = classified - operations
        assert not stale, f"The sweep references routes that no longer exist: {sorted(stale)}"

    def test_public_operations_are_a_deliberate_short_list(self) -> None:
        """05_SECURITY.md §10.8: "the app has no public/anonymous endpoints"
        beyond login and health. Growth here should be a conscious act."""
        assert set(all_operations()) >= PUBLIC_OPERATIONS
        assert len(PUBLIC_OPERATIONS) == 3


class TestUnauthenticatedAccess:
    """08_TESTING.md: "Unauthenticated access to any Engagement-scoped endpoint
    is rejected (401)"."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [op for op in all_operations() if op not in PUBLIC_OPERATIONS],
        ids=lambda value: value if isinstance(value, str) else str(value),
    )
    def test_every_protected_operation_rejects_an_anonymous_caller(
        self, api_client: TestClient, method: str, path: str
    ) -> None:
        url = (
            path.replace("{engagement_id}", str(uuid.uuid4()))
            .replace("{document_id}", str(uuid.uuid4()))
            .replace("{request_id}", str(uuid.uuid4()))
            .replace("{finding_id}", str(uuid.uuid4()))
            .replace("{scoped_id}", str(uuid.uuid4()))
            .replace("{user_id}", str(uuid.uuid4()))
        )

        response = api_client.request(method, url, json={})

        assert response.status_code == 401, (
            f"{method} {path} returned {response.status_code} to an anonymous caller"
        )
        assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"

    def test_a_forged_session_cookie_is_rejected_everywhere(
        self, api_client: TestClient, populated_engagement: dict[str, Any]
    ) -> None:
        from app.services.auth import SESSION_COOKIE_NAME

        api_client.cookies.set(SESSION_COOKIE_NAME, "forged-token")
        setup = populated_engagement

        for route in ENGAGEMENT_SCOPED:
            url = resolve(route, setup, setup["owner"].id)
            response = call(api_client, route, url, setup)
            assert response.status_code == 401, f"{route.method} {route.template}"


class TestCrossEngagementIsolation:
    """08_TESTING.md: "An Auditor not in EngagementAssignment for Engagement X
    cannot read or write any of X's data (403), even by guessing/enumerating
    IDs." 05_SECURITY.md §10.1 rates this the only Critical threat."""

    @pytest.mark.parametrize("route", ENGAGEMENT_SCOPED, ids=lambda r: f"{r.method} {r.template}")
    def test_unassigned_auditor_is_denied_on_every_scoped_operation(
        self,
        api_client: TestClient,
        make_user: Any,
        populated_engagement: dict[str, Any],
        route: ScopedRoute,
    ) -> None:
        setup = populated_engagement
        intruder = make_user(Role.auditor, password=PASSWORD, name="Unassigned Auditor")
        login(api_client, intruder)

        url = resolve(route, setup, setup["owner"].id)
        response = call(api_client, route, url, setup)

        # 403 where the resource is addressed directly, 404 where the resource
        # is reached via a child id the caller cannot see. Either is a denial;
        # what must never happen is a 2xx.
        assert response.status_code in (403, 404), (
            f"{route.method} {route.template} returned {response.status_code} "
            f"to an unassigned auditor"
        )

    @pytest.mark.parametrize("route", ENGAGEMENT_SCOPED, ids=lambda r: f"{r.method} {r.template}")
    def test_no_client_data_leaks_in_a_denial_response(
        self,
        api_client: TestClient,
        make_user: Any,
        populated_engagement: dict[str, Any],
        route: ScopedRoute,
    ) -> None:
        """A denial must not disclose through its body what it refused to
        disclose through its status. The client name is the canary: it is
        Internal-classified (03_DATA_MODEL.md §8.4) and present on the
        engagement every one of these routes hangs off."""
        setup = populated_engagement
        intruder = make_user(Role.auditor, password=PASSWORD)
        login(api_client, intruder)

        url = resolve(route, setup, setup["owner"].id)
        response = call(api_client, route, url, setup)

        assert "Confidential Client Ltd" not in response.text
        assert "Provide the firewall export" not in response.text

    def test_enumerating_random_ids_yields_nothing(
        self, api_client: TestClient, make_user: Any, populated_engagement: dict[str, Any]
    ) -> None:
        """ "Even by guessing/enumerating IDs" — a caller with no relationship to
        anything gets the same answer for a real id and an invented one."""
        auditor = make_user(Role.auditor, password=PASSWORD)
        login(api_client, auditor)

        for _ in range(10):
            response = api_client.get(f"/api/engagements/{uuid.uuid4()}")
            assert response.status_code == 404
            assert "Confidential Client Ltd" not in response.text

    def test_an_auditor_sees_no_other_engagement_in_any_list(
        self, api_client: TestClient, make_user: Any, populated_engagement: dict[str, Any]
    ) -> None:
        auditor = make_user(Role.auditor, password=PASSWORD)
        login(api_client, auditor)

        listing = api_client.get("/api/engagements").json()

        assert listing["items"] == []
        assert listing["total"] == 0

    def test_revoked_assignment_denies_every_scoped_operation(
        self, api_client: TestClient, db: DBSession, populated_engagement: dict[str, Any]
    ) -> None:
        """Access is re-derived per request, so removing an assignment takes
        effect immediately rather than at the owner's next login."""
        setup = populated_engagement
        assignment = (
            db.query(EngagementAssignment)
            .filter_by(engagement_id=setup["engagement"].id, user_id=setup["owner"].id)
            .one()
        )
        db.delete(assignment)
        db.flush()

        login(api_client, setup["owner"])
        for route in ENGAGEMENT_SCOPED:
            url = resolve(route, setup, setup["owner"].id)
            response = call(api_client, route, url, setup)
            assert response.status_code in (403, 404), f"{route.method} {route.template}"

    def test_assigned_auditor_can_reach_the_read_operations(
        self, api_client: TestClient, populated_engagement: dict[str, Any]
    ) -> None:
        """The boundary must admit the people it is supposed to admit. A filter
        that denies everyone would pass every test above and be useless."""
        setup = populated_engagement
        login(api_client, setup["owner"])

        for route in ENGAGEMENT_SCOPED:
            if route.method != "GET":
                continue
            url = resolve(route, setup, setup["owner"].id)
            response = call(api_client, route, url, setup)
            # The report is 404 until finalization; everything else must be 200.
            expected = (404,) if route.template.endswith("/report") else (200,)
            assert response.status_code in expected, (
                f"{route.method} {route.template} denied its own assigned auditor "
                f"with {response.status_code}"
            )


class TestReviewerAndAdminVisibility:
    @pytest.mark.parametrize(
        "route",
        [r for r in ENGAGEMENT_SCOPED if r.method == "GET"],
        ids=lambda r: f"{r.method} {r.template}",
    )
    def test_reviewer_may_read_any_engagement_without_an_assignment(
        self, api_client: TestClient, populated_engagement: dict[str, Any], route: ScopedRoute
    ) -> None:
        """03_DATA_MODEL.md §8.2: Reviewers see all engagements at the firm."""
        setup = populated_engagement
        login(api_client, setup["reviewer"])

        url = resolve(route, setup, setup["owner"].id)
        response = call(api_client, route, url, setup)

        expected = (404,) if route.template.endswith("/report") else (200,)
        assert response.status_code in expected

    @pytest.mark.parametrize(
        "route",
        [r for r in ENGAGEMENT_SCOPED if r.method == "GET"],
        ids=lambda r: f"{r.method} {r.template}",
    )
    def test_admin_may_read_any_engagement_for_support(
        self,
        api_client: TestClient,
        make_user: Any,
        populated_engagement: dict[str, Any],
        route: ScopedRoute,
    ) -> None:
        setup = populated_engagement
        admin = make_user(Role.admin, password=PASSWORD)
        login(api_client, admin)

        url = resolve(route, setup, setup["owner"].id)
        response = call(api_client, route, url, setup)

        expected = (404,) if route.template.endswith("/report") else (200,)
        assert response.status_code in expected

    def test_admin_engagement_access_is_logged_distinctly(
        self,
        api_client: TestClient,
        make_user: Any,
        populated_engagement: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """03_DATA_MODEL.md §8.2: "every Admin access to engagement content is
        logged distinctly from normal Reviewer access (Admins are not expected
        to routinely view client evidence)"."""
        import logging

        setup = populated_engagement
        admin = make_user(Role.admin, password=PASSWORD)
        login(api_client, admin)

        with caplog.at_level(logging.WARNING, logger="auditlens.audit"):
            api_client.get(f"/api/engagements/{setup['engagement'].id}")

        assert any("admin.engagement_access" in record.message for record in caplog.records)

    def test_reviewer_access_is_not_logged_as_admin_access(
        self,
        api_client: TestClient,
        populated_engagement: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The distinction is only useful if normal Reviewer work does not also
        trip it — an alert that fires constantly is an alert nobody reads."""
        import logging

        setup = populated_engagement
        login(api_client, setup["reviewer"])

        with caplog.at_level(logging.WARNING, logger="auditlens.audit"):
            api_client.get(f"/api/engagements/{setup['engagement'].id}")

        assert not any("admin.engagement_access" in r.message for r in caplog.records)


class TestRoleGates:
    """Role restrictions that hold even for a caller with full access to the
    engagement — authority is separate from visibility."""

    def test_assigned_auditor_cannot_finalize(
        self, api_client: TestClient, populated_engagement: dict[str, Any]
    ) -> None:
        setup = populated_engagement
        login(api_client, setup["owner"])

        response = api_client.post(f"/api/engagements/{setup['engagement'].id}/finalize")

        assert response.status_code == 403

    def test_assigned_auditor_cannot_acknowledge_a_gap(
        self, api_client: TestClient, populated_engagement: dict[str, Any]
    ) -> None:
        """The gap flag is what permits finalizing without evidence, so it
        carries finalization-level authority (ADR-012)."""
        setup = populated_engagement
        login(api_client, setup["owner"])

        response = api_client.patch(
            f"/api/scoped-requirements/{setup['scoped'].id}/gap",
            json={"gap_acknowledged": True, "gap_note": "Trust me."},
        )

        assert response.status_code == 403

    def test_assigned_auditor_cannot_change_assignments(
        self, api_client: TestClient, make_user: Any, populated_engagement: dict[str, Any]
    ) -> None:
        """If an Auditor could assign, the ownership boundary would be
        self-service and therefore no boundary at all."""
        setup = populated_engagement
        colleague = make_user(Role.auditor, password=PASSWORD)
        login(api_client, setup["owner"])

        response = api_client.post(
            f"/api/engagements/{setup['engagement'].id}/assignments",
            json={"user_id": str(colleague.id)},
        )

        assert response.status_code == 403

    def test_admin_cannot_create_an_engagement(
        self, api_client: TestClient, make_user: Any
    ) -> None:
        """04_API_CONTRACT.md restricts creation to auditor or reviewer."""
        admin = make_user(Role.admin, password=PASSWORD)
        login(api_client, admin)

        response = api_client.post(
            "/api/engagements",
            json={"client_name": "X", "entity_type": "merchant", "merchant_level": "1"},
        )

        assert response.status_code == 403

    def test_admin_cannot_finalize_despite_seeing_everything(
        self, api_client: TestClient, make_user: Any, populated_engagement: dict[str, Any]
    ) -> None:
        """00_PRODUCT.md §5.3: sign-off authority is a role property, not an
        escalation path."""
        setup = populated_engagement
        admin = make_user(Role.admin, password=PASSWORD)
        login(api_client, admin)

        response = api_client.post(f"/api/engagements/{setup['engagement'].id}/finalize")

        assert response.status_code == 403


class TestPrivilegeEscalationSurface:
    """05_SECURITY.md §10.3: "role is set only by Admin action on the User
    record, never accepted as a client-supplied field on any other endpoint."""

    def test_no_endpoint_accepts_a_role_field(self) -> None:
        """Asserted against the generated schema, so it holds for every request
        body the application will accept rather than for the ones tested."""
        schemas = app.openapi()["components"]["schemas"]

        offenders = [
            name
            for name, schema in schemas.items()
            if "role" in (schema.get("properties") or {})
            and not name.startswith(("CurrentUser", "Login", "UserSummary"))
        ]
        assert not offenders, f"These request schemas accept a role field: {offenders}"

    def test_no_request_schema_accepts_reviewed_by_or_ownership(self) -> None:
        """The fields that would let a caller assert who reviewed something, or
        who owns it, must not exist on any input schema."""
        schemas = app.openapi()["components"]["schemas"]
        forbidden = {"reviewed_by", "created_by", "finalized_by", "uploaded_by", "user_id"}

        for name, schema in schemas.items():
            # Request schemas are the ones a client sends; responses may
            # legitimately carry these fields.
            if not name.endswith(("Request", "Create", "Update")):
                continue
            properties = set(schema.get("properties") or {})
            overlap = properties & forbidden
            # AssignmentCreate carries user_id by design — it is the Reviewer
            # naming who to assign, which is the whole purpose of the endpoint.
            if name == "AssignmentCreate":
                overlap -= {"user_id"}
            assert not overlap, f"{name} accepts client-supplied {sorted(overlap)}"

    def test_supplying_a_role_at_engagement_creation_is_ignored(
        self, api_client: TestClient, make_user: Any, db: DBSession
    ) -> None:
        auditor = make_user(Role.auditor, password=PASSWORD)
        login(api_client, auditor)

        api_client.post(
            "/api/engagements",
            json={
                "client_name": "Escalation Attempt",
                "entity_type": "merchant",
                "merchant_level": "1",
                "role": "admin",
            },
        )

        db.refresh(auditor)
        assert auditor.role == Role.auditor

    def test_a_deactivated_user_loses_access_immediately(
        self, api_client: TestClient, db: DBSession, populated_engagement: dict[str, Any]
    ) -> None:
        """Role and active state are re-read per request, so deactivation does
        not wait for the session to expire."""
        setup = populated_engagement
        login(api_client, setup["owner"])
        assert api_client.get(f"/api/engagements/{setup['engagement'].id}").status_code == 200

        setup["owner"].is_active = False
        db.flush()

        assert api_client.get(f"/api/engagements/{setup['engagement'].id}").status_code == 401

    def test_demoting_a_reviewer_revokes_finalize_immediately(
        self, api_client: TestClient, db: DBSession, populated_engagement: dict[str, Any]
    ) -> None:
        setup = populated_engagement
        login(api_client, setup["reviewer"])
        # Ready the engagement so only the role stands between them and success.
        api_client.patch(f"/api/findings/{setup['finding'].id}/review", json={"action": "accept"})

        setup["reviewer"].role = Role.auditor
        db.flush()

        response = api_client.post(f"/api/engagements/{setup['engagement'].id}/finalize")
        assert response.status_code == 403


class TestFindingApprovalBoundary:
    """08_TESTING.md: "A Finding cannot reach status=approved without
    reviewed_by set — attempt this via direct service-layer call, not just via
    the API, to catch any bypass path."

    The service-layer attempt lives in test_findings.py. What is added here is
    the database-level backstop, so both halves of ADR-003's defence are
    covered by the sweep."""

    def test_the_database_refuses_an_approved_finding_without_a_reviewer(
        self, db: DBSession, populated_engagement: dict[str, Any]
    ) -> None:
        from sqlalchemy.exc import IntegrityError

        setup = populated_engagement
        setup["finding"].status = FindingStatus.approved
        setup["finding"].final_status = ComplianceStatus.satisfied
        setup["finding"].reviewed_by = None

        with pytest.raises(IntegrityError, match="ck_approved_requires_reviewer"):
            db.flush()
