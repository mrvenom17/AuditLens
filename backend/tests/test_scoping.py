"""Scope-matching tests (TASK-013, TASK-014).

TASK-013 requires the timeout/fallback path be tested explicitly with a mocked
LLM client, and 08_TESTING.md's requirement-to-test map lists "LLM timeout
degrades gracefully, never 500s" as a critical item.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.corpus.loader import ingest
from app.models.enums import EngagementStatus, Role, ScopeSource
from app.models.scoping import ScopedRequirement
from app.pipelines.llm import LLMError, LLMResponse, LLMTimeoutError, set_llm_client
from app.services.scoping import reset_rate_limits

PASSWORD = "correct-horse-battery-staple"


def login(client: TestClient, user: Any) -> None:
    assert (
        client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD}).status_code
        == 200
    )


class FakeLLM:
    """Records what it was asked, so tests can assert on prompt contents."""

    def __init__(self, payload: Any = None, *, raises: Exception | None = None) -> None:
        self._payload = payload
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    def complete(
        self, *, system: str, prompt: str, timeout: float, max_tokens: int = 2048
    ) -> LLMResponse:
        self.calls.append({"system": system, "prompt": prompt, "timeout": timeout})
        if self._raises is not None:
            raise self._raises
        return LLMResponse(text=json.dumps(self._payload), input_tokens=100, output_tokens=50)


@pytest.fixture(autouse=True)
def _reset_clients() -> Any:
    reset_rate_limits()
    yield
    set_llm_client(None)
    reset_rate_limits()


@pytest.fixture
def corpus(db: DBSession) -> None:
    ingest(db)


def suggestion_payload(*clause_ids: str, saq: str = "D", ambiguous: bool = False) -> dict[str, Any]:
    return {
        "saq_type": saq,
        "ambiguous_entity_type": ambiguous,
        "requirements": [
            {"clause_id": c, "rationale": f"Applies because of clause {c}."} for c in clause_ids
        ],
    }


class TestScopeSuggestionHappyPath:
    def test_proposes_requirements_none_confirmed(
        self, api_client: TestClient, make_user: Any, make_engagement: Any, corpus: None
    ) -> None:
        """01_REQUIREMENTS.md acceptance criterion: the response includes a
        proposed SAQ type and at least one requirement family, none confirmed."""
        set_llm_client(FakeLLM(suggestion_payload("1.2.1", "3.3.1", "8.3.6", saq="A")))
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor)
        login(api_client, auditor)

        response = api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["manual_scoping_required"] is False
        assert body["saq_type"] == "A"
        assert len(body["proposed_requirements"]) == 3
        assert all(r["confirmed"] is False for r in body["proposed_requirements"])
        assert {r["requirement_family"] for r in body["proposed_requirements"]} == {1, 3, 8}

    def test_suggestions_are_marked_ai_suggested(
        self, api_client: TestClient, make_user: Any, make_engagement: Any, corpus: None
    ) -> None:
        set_llm_client(FakeLLM(suggestion_payload("1.2.1")))
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor)
        login(api_client, auditor)

        body = api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion").json()

        assert body["proposed_requirements"][0]["source"] == "ai_suggested"
        assert body["proposed_requirements"][0]["rationale"]

    def test_success_advances_the_engagement_to_scoping(
        self,
        api_client: TestClient,
        db: DBSession,
        make_user: Any,
        make_engagement: Any,
        corpus: None,
    ) -> None:
        set_llm_client(FakeLLM(suggestion_payload("1.2.1")))
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor)
        login(api_client, auditor)

        api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion")

        db.refresh(engagement)
        assert engagement.status == EngagementStatus.scoping

    def test_only_profile_fields_reach_the_llm(
        self, api_client: TestClient, make_user: Any, make_engagement: Any, corpus: None
    ) -> None:
        """TASK-013 security requirement: only structured profile fields are
        sent at this step — confirm no accidental inclusion of anything else."""
        fake = FakeLLM(suggestion_payload("1.2.1"))
        set_llm_client(fake)
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor, client_name="Northwind Retail")
        login(api_client, auditor)

        api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion")

        prompt = fake.calls[0]["prompt"]
        assert "merchant" in prompt
        # The client's name is identifying information with no bearing on which
        # clauses apply, so it is not sent.
        assert "Northwind Retail" not in prompt

    def test_uses_the_interactive_timeout(
        self, api_client: TestClient, make_user: Any, make_engagement: Any, corpus: None
    ) -> None:
        """02_ARCHITECTURE.md §7.6: 8 seconds on the interactive path."""
        fake = FakeLLM(suggestion_payload("1.2.1"))
        set_llm_client(fake)
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor)
        login(api_client, auditor)

        api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion")

        assert fake.calls[0]["timeout"] == 8.0

    def test_hallucinated_clause_ids_are_dropped(
        self, api_client: TestClient, make_user: Any, make_engagement: Any, corpus: None
    ) -> None:
        """A scope row must reference a real corpus clause. A suggestion for a
        clause that does not exist is discarded rather than stored, because a
        ScopedRequirement pointing at nothing would break every downstream step."""
        set_llm_client(FakeLLM(suggestion_payload("1.2.1", "99.99.99", "not-a-clause")))
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor)
        login(api_client, auditor)

        body = api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion").json()

        assert [r["clause_id"] for r in body["proposed_requirements"]] == ["1.2.1"]

    def test_ambiguous_entity_type_is_surfaced(
        self, api_client: TestClient, make_user: Any, make_engagement: Any, corpus: None
    ) -> None:
        """01_REQUIREMENTS.md Edge Cases: propose the broader scope and flag it
        rather than guessing narrow."""
        set_llm_client(FakeLLM(suggestion_payload("1.2.1", ambiguous=True)))
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor)
        login(api_client, auditor)

        body = api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion").json()

        assert body["ambiguous_entity_type"] is True


class TestScopeSuggestionDegradation:
    """01_REQUIREMENTS.md acceptance criterion and 08_TESTING.md's critical map:
    an LLM timeout returns 200 with `manual_scoping_required: true`, never 500."""

    @pytest.mark.parametrize(
        "failure",
        [
            LLMTimeoutError("timed out"),
            LLMError("LLM request failed with status 503"),
            LLMError("The model did not return parseable JSON."),
        ],
    )
    def test_llm_failure_degrades_to_manual_scoping(
        self,
        api_client: TestClient,
        make_user: Any,
        make_engagement: Any,
        corpus: None,
        failure: Exception,
    ) -> None:
        set_llm_client(FakeLLM(raises=failure))
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor)
        login(api_client, auditor)

        response = api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion")

        assert response.status_code == 200, "must never be a 500 for an LLM-unavailable case"
        assert response.json()["manual_scoping_required"] is True
        assert response.json()["proposed_requirements"] == []

    def test_degraded_run_leaves_the_engagement_in_intake(
        self,
        api_client: TestClient,
        db: DBSession,
        make_user: Any,
        make_engagement: Any,
        corpus: None,
    ) -> None:
        """01_REQUIREMENTS.md: "engagement remains in intake"."""
        set_llm_client(FakeLLM(raises=LLMTimeoutError("timed out")))
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor)
        login(api_client, auditor)

        api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion")

        db.refresh(engagement)
        assert engagement.status == EngagementStatus.intake

    def test_malformed_llm_response_shape_degrades_rather_than_crashing(
        self, api_client: TestClient, make_user: Any, make_engagement: Any, corpus: None
    ) -> None:
        set_llm_client(FakeLLM({"unexpected": "shape"}))
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor)
        login(api_client, auditor)

        response = api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion")

        assert response.status_code == 200
        assert response.json()["manual_scoping_required"] is True

    def test_manual_scoping_still_works_after_a_degraded_run(
        self, api_client: TestClient, make_user: Any, make_engagement: Any, corpus: None
    ) -> None:
        """The point of degrading rather than failing: the auditor can still do
        the work, just without the assist."""
        set_llm_client(FakeLLM(raises=LLMTimeoutError("timed out")))
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor)
        login(api_client, auditor)
        api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion")

        added = api_client.post(
            f"/api/engagements/{engagement.id}/scoped-requirements",
            json={"clause_id": "1.2.1", "rationale": "Scoped manually."},
        )

        assert added.status_code == 201
        assert added.json()["source"] == "manual"
        assert added.json()["confirmed"] is False

    def test_a_fully_manual_engagement_reaches_in_progress(
        self,
        api_client: TestClient,
        db: DBSession,
        make_user: Any,
        make_engagement: Any,
        corpus: None,
    ) -> None:
        """Regression, found by the TASK-024 end-to-end run rather than by any
        unit test.

        An engagement scoped entirely by hand — the documented path when the LLM
        is unavailable — used to sit in `intake` forever, because only a
        *successful* AI suggestion advanced it to `scoping`. Since `intake` has
        no edge to `in_progress`, finalization then failed with
        INVALID_STATUS_TRANSITION no matter how much work had been done. The
        whole mandated fallback was a dead end, and every step of it returned
        2xx right up to the last one.
        """
        set_llm_client(FakeLLM(raises=LLMTimeoutError("timed out")))
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor)
        login(api_client, auditor)

        api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion")
        db.refresh(engagement)
        assert engagement.status == EngagementStatus.intake, "degraded run stays in intake"

        added = api_client.post(
            f"/api/engagements/{engagement.id}/scoped-requirements",
            json={"clause_id": "1.2.1"},
        ).json()
        db.refresh(engagement)
        assert engagement.status == EngagementStatus.scoping, "adding scope begins scoping"

        api_client.patch(f"/api/scoped-requirements/{added['id']}", json={"confirmed": True})
        db.refresh(engagement)
        assert engagement.status == EngagementStatus.in_progress, (
            "confirming scope must reach in_progress, or the engagement can never finalize"
        )

    def test_a_manually_scoped_engagement_can_be_finalized(
        self,
        api_client: TestClient,
        db: DBSession,
        make_user: Any,
        make_engagement: Any,
        corpus: None,
    ) -> None:
        """The end the previous test's path has to reach. Asserted separately
        because "status is in_progress" is a proxy; "can actually be signed off"
        is the property that matters."""
        from app.models.engagement import EngagementAssignment

        set_llm_client(FakeLLM(raises=LLMTimeoutError("timed out")))
        auditor = make_user(Role.auditor, password=PASSWORD)
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        engagement = make_engagement(auditor)
        db.add(EngagementAssignment(engagement_id=engagement.id, user_id=reviewer.id))
        db.flush()

        login(api_client, auditor)
        api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion")
        added = api_client.post(
            f"/api/engagements/{engagement.id}/scoped-requirements",
            json={"clause_id": "1.2.1"},
        ).json()
        api_client.patch(f"/api/scoped-requirements/{added['id']}", json={"confirmed": True})

        login(api_client, reviewer)
        api_client.patch(
            f"/api/scoped-requirements/{added['id']}/gap",
            json={"gap_acknowledged": True, "gap_note": "No evidence supplied in time."},
        )
        response = api_client.post(f"/api/engagements/{engagement.id}/finalize")

        assert response.status_code == 200, response.text
        assert response.json()["engagement_status"] == "finalized"


class TestScopeSuggestionPreconditions:
    def test_missing_profile_fields_returns_409(
        self,
        api_client: TestClient,
        db: DBSession,
        make_user: Any,
        make_engagement: Any,
        corpus: None,
    ) -> None:
        set_llm_client(FakeLLM(suggestion_payload("1.2.1")))
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor)
        engagement.merchant_level = None  # merchant without a level
        db.flush()
        login(api_client, auditor)

        response = api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "MISSING_PROFILE_FIELDS"
        assert response.json()["error"]["missing_fields"] == ["merchant_level"]

    def test_unassigned_auditor_is_forbidden(
        self, api_client: TestClient, make_user: Any, make_engagement: Any, corpus: None
    ) -> None:
        set_llm_client(FakeLLM(suggestion_payload("1.2.1")))
        owner = make_user(Role.auditor, password=PASSWORD)
        intruder = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(owner)
        login(api_client, intruder)

        response = api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion")

        assert response.status_code == 403

    def test_rate_limited_after_the_hourly_cap(
        self, api_client: TestClient, make_user: Any, make_engagement: Any, corpus: None
    ) -> None:
        """04_API_CONTRACT.md: capped per user to prevent runaway LLM cost."""
        from app.config.settings import settings

        set_llm_client(FakeLLM(suggestion_payload("1.2.1")))
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor)
        login(api_client, auditor)

        for _ in range(settings.SCOPE_SUGGESTION_PER_HOUR):
            api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion")

        response = api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "RATE_LIMITED"


class TestReRunIdempotency:
    def test_rerun_replaces_unconfirmed_suggestions(
        self,
        api_client: TestClient,
        db: DBSession,
        make_user: Any,
        make_engagement: Any,
        corpus: None,
    ) -> None:
        """04_API_CONTRACT.md: re-running replaces prior ai_suggested,
        confirmed=false rows."""
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor)
        login(api_client, auditor)

        set_llm_client(FakeLLM(suggestion_payload("1.2.1", "1.2.2")))
        api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion")

        set_llm_client(FakeLLM(suggestion_payload("3.3.1")))
        body = api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion").json()

        assert [r["clause_id"] for r in body["proposed_requirements"]] == ["3.3.1"]
        rows = db.scalars(
            select(ScopedRequirement).where(ScopedRequirement.engagement_id == engagement.id)
        ).all()
        assert {r.requirement.clause_id for r in rows} == {"3.3.1"}

    def test_rerun_never_touches_confirmed_rows(
        self,
        api_client: TestClient,
        db: DBSession,
        make_user: Any,
        make_engagement: Any,
        corpus: None,
    ) -> None:
        """The safety property that matters: a confirmed row is a human
        decision, and re-running the AI must not discard it."""
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor)
        login(api_client, auditor)

        set_llm_client(FakeLLM(suggestion_payload("1.2.1", "1.2.2")))
        first = api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion").json()
        keep = next(r for r in first["proposed_requirements"] if r["clause_id"] == "1.2.1")
        api_client.patch(f"/api/scoped-requirements/{keep['id']}", json={"confirmed": True})

        set_llm_client(FakeLLM(suggestion_payload("3.3.1")))
        api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion")

        rows = db.scalars(
            select(ScopedRequirement).where(ScopedRequirement.engagement_id == engagement.id)
        ).all()
        by_clause = {r.requirement.clause_id: r for r in rows}
        assert "1.2.1" in by_clause, "a confirmed requirement was destroyed by a re-run"
        assert by_clause["1.2.1"].confirmed is True
        assert "1.2.2" not in by_clause
        assert "3.3.1" in by_clause

    def test_rerun_does_not_duplicate_a_manually_added_clause(
        self, api_client: TestClient, make_user: Any, make_engagement: Any, corpus: None
    ) -> None:
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor)
        login(api_client, auditor)
        api_client.post(
            f"/api/engagements/{engagement.id}/scoped-requirements",
            json={"clause_id": "1.2.1"},
        )

        set_llm_client(FakeLLM(suggestion_payload("1.2.1")))
        api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion")

        listed = api_client.get(f"/api/engagements/{engagement.id}/scoped-requirements").json()
        assert [r["clause_id"] for r in listed] == ["1.2.1"]
        assert listed[0]["source"] == "manual"


class TestConfirmation:
    def test_confirming_sets_the_flag_and_advances_the_engagement(
        self,
        api_client: TestClient,
        db: DBSession,
        make_user: Any,
        make_engagement: Any,
        corpus: None,
    ) -> None:
        set_llm_client(FakeLLM(suggestion_payload("1.2.1")))
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor)
        login(api_client, auditor)
        proposed = api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion").json()[
            "proposed_requirements"
        ][0]

        response = api_client.patch(
            f"/api/scoped-requirements/{proposed['id']}", json={"confirmed": True}
        )

        assert response.status_code == 200
        assert response.json()["confirmed"] is True
        db.refresh(engagement)
        assert engagement.status == EngagementStatus.in_progress

    def test_nothing_is_confirmed_without_an_explicit_human_action(
        self,
        api_client: TestClient,
        db: DBSession,
        make_user: Any,
        make_engagement: Any,
        corpus: None,
    ) -> None:
        """01_REQUIREMENTS.md, Explicitly Forbidden Behavior — the system must
        never mark a ScopedRequirement confirmed without a human action."""
        set_llm_client(FakeLLM(suggestion_payload("1.2.1", "3.3.1", "8.3.6")))
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor)
        login(api_client, auditor)

        api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion")

        rows = db.scalars(
            select(ScopedRequirement).where(ScopedRequirement.engagement_id == engagement.id)
        ).all()
        assert rows
        assert all(r.confirmed is False for r in rows)
        assert all(r.source == ScopeSource.ai_suggested for r in rows)

    def test_unconfirming_is_possible(
        self, api_client: TestClient, make_user: Any, make_engagement: Any, corpus: None
    ) -> None:
        set_llm_client(FakeLLM(suggestion_payload("1.2.1")))
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor)
        login(api_client, auditor)
        proposed = api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion").json()[
            "proposed_requirements"
        ][0]
        api_client.patch(f"/api/scoped-requirements/{proposed['id']}", json={"confirmed": True})

        response = api_client.patch(
            f"/api/scoped-requirements/{proposed['id']}", json={"confirmed": False}
        )

        assert response.status_code == 200
        assert response.json()["confirmed"] is False

    def test_unassigned_auditor_cannot_confirm(
        self, api_client: TestClient, make_user: Any, make_engagement: Any, corpus: None
    ) -> None:
        set_llm_client(FakeLLM(suggestion_payload("1.2.1")))
        owner = make_user(Role.auditor, password=PASSWORD)
        intruder = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(owner)
        login(api_client, owner)
        proposed = api_client.post(f"/api/engagements/{engagement.id}/scope-suggestion").json()[
            "proposed_requirements"
        ][0]

        login(api_client, intruder)
        response = api_client.patch(
            f"/api/scoped-requirements/{proposed['id']}", json={"confirmed": True}
        )

        assert response.status_code == 403

    def test_unknown_scoped_requirement_gives_404(
        self, api_client: TestClient, make_user: Any
    ) -> None:
        auditor = make_user(Role.auditor, password=PASSWORD)
        login(api_client, auditor)

        response = api_client.patch(
            f"/api/scoped-requirements/{uuid.uuid4()}", json={"confirmed": True}
        )

        assert response.status_code == 404


class TestGapAcknowledgement:
    def test_reviewer_can_acknowledge_a_gap_with_a_note(
        self,
        api_client: TestClient,
        make_user: Any,
        make_engagement: Any,
        make_scoped_requirement: Any,
    ) -> None:
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        engagement = make_engagement(reviewer)
        scoped = make_scoped_requirement(engagement)
        login(api_client, reviewer)

        response = api_client.patch(
            f"/api/scoped-requirements/{scoped.id}/gap",
            json={"gap_acknowledged": True, "gap_note": "Client could not produce the artifact."},
        )

        assert response.status_code == 200
        assert response.json()["gap_acknowledged"] is True
        assert response.json()["gap_note"] == "Client could not produce the artifact."

    def test_auditor_cannot_acknowledge_a_gap(
        self,
        api_client: TestClient,
        make_user: Any,
        make_engagement: Any,
        make_scoped_requirement: Any,
    ) -> None:
        """This is the flag that permits finalizing without evidence, so it is
        Reviewer-only regardless of who is assigned."""
        auditor = make_user(Role.auditor, password=PASSWORD)
        engagement = make_engagement(auditor)
        scoped = make_scoped_requirement(engagement)
        login(api_client, auditor)

        response = api_client.patch(
            f"/api/scoped-requirements/{scoped.id}/gap",
            json={"gap_acknowledged": True, "gap_note": "Trust me."},
        )

        assert response.status_code == 403

    def test_acknowledging_without_a_note_is_rejected(
        self,
        api_client: TestClient,
        make_user: Any,
        make_engagement: Any,
        make_scoped_requirement: Any,
    ) -> None:
        """An unexplained gap in a signed report is worse than no gap flag."""
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        engagement = make_engagement(reviewer)
        scoped = make_scoped_requirement(engagement)
        login(api_client, reviewer)

        response = api_client.patch(
            f"/api/scoped-requirements/{scoped.id}/gap",
            json={"gap_acknowledged": True, "gap_note": "   "},
        )

        assert response.status_code == 400
