"""Evidence-request generation tests (TASK-015).

TASK-015 requires verifying that no duplicate requests are generated for
requirements already covered. 01_REQUIREMENTS.md § Evidence Request Generation
adds the LLM fallback ("this feature must never fail outright") and the
409 NO_CONFIRMED_SCOPE precondition, and states as Explicitly Forbidden
Behavior that the system must never dispatch any external communication.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.models.enums import AuditStatus, EvidenceRequestStatus, Role
from app.models.scoping import EvidenceRequest
from app.pipelines.llm import LLMError, LLMResponse, LLMTimeoutError, set_llm_client

PASSWORD = "correct-horse-battery-staple"


def login(client: TestClient, user: Any) -> None:
    assert (
        client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD}).status_code
        == 200
    )


class FakeLLM:
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
        return LLMResponse(text=json.dumps(self._payload), input_tokens=10, output_tokens=5)


@pytest.fixture(autouse=True)
def _reset_llm() -> Any:
    yield
    set_llm_client(None)


@pytest.fixture
def scoped_audit(make_user: Any, make_audit: Any) -> dict[str, Any]:
    auditor = make_user(Role.auditor, password=PASSWORD)
    return {
        "auditor": auditor,
        "audit": make_audit(auditor, status=AuditStatus.in_progress),
    }


def drafted(*clause_ids: str) -> dict[str, Any]:
    return {
        "requests": [
            {"control_id": c, "description": f"Please provide the artifact for {c}."}
            for c in clause_ids
        ]
    }


class TestPreconditions:
    def test_no_confirmed_scope_returns_409(
        self, api_client: TestClient, scoped_audit: dict[str, Any]
    ) -> None:
        """04_API_CONTRACT.md: 409 NO_CONFIRMED_SCOPE, with guidance to complete
        scoping first."""
        login(api_client, scoped_audit["auditor"])

        response = api_client.post(
            f"/api/audits/{scoped_audit['audit'].id}/evidence-requests/generate"
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "NO_CONFIRMED_SCOPE"
        # 04_API_CONTRACT.md asks for "guidance to complete scoping first" — the
        # message has to say what to do next, not merely that something is wrong.
        message = response.json()["error"]["message"].lower()
        assert "confirm" in message
        assert "requirement" in message

    def test_unconfirmed_scope_alone_does_not_satisfy_the_precondition(
        self,
        api_client: TestClient,
        make_scoped_requirement: Any,
        scoped_audit: dict[str, Any],
    ) -> None:
        """A proposal the auditor never accepted is not scope."""
        make_scoped_requirement(scoped_audit["audit"], confirmed=False)
        login(api_client, scoped_audit["auditor"])

        response = api_client.post(
            f"/api/audits/{scoped_audit['audit'].id}/evidence-requests/generate"
        )

        assert response.status_code == 409

    def test_unassigned_auditor_is_forbidden(
        self,
        api_client: TestClient,
        make_user: Any,
        make_scoped_requirement: Any,
        scoped_audit: dict[str, Any],
    ) -> None:
        make_scoped_requirement(scoped_audit["audit"], confirmed=True)
        intruder = make_user(Role.auditor, password=PASSWORD)
        login(api_client, intruder)

        response = api_client.post(
            f"/api/audits/{scoped_audit['audit'].id}/evidence-requests/generate"
        )

        assert response.status_code == 403


class TestGeneration:
    def test_one_draft_request_per_confirmed_requirement(
        self,
        api_client: TestClient,
        db: DBSession,
        make_requirement: Any,
        make_scoped_requirement: Any,
        scoped_audit: dict[str, Any],
    ) -> None:
        clause_ids = ["1.2.1", "3.3.1", "8.3.6"]
        for control_id in clause_ids:
            requirement = make_requirement(control_id=control_id, family=int(control_id[0]))
            make_scoped_requirement(scoped_audit["audit"], confirmed=True, requirement=requirement)
        set_llm_client(FakeLLM(drafted(*clause_ids)))
        login(api_client, scoped_audit["auditor"])

        response = api_client.post(
            f"/api/audits/{scoped_audit['audit'].id}/evidence-requests/generate"
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["created"]) == 3
        assert sorted(r["control_id"] for r in body["created"]) == clause_ids
        assert all(r["status"] == "draft" for r in body["created"])

    def test_generated_requests_are_always_draft_never_sent(
        self,
        api_client: TestClient,
        db: DBSession,
        make_scoped_requirement: Any,
        scoped_audit: dict[str, Any],
    ) -> None:
        """ADR-004 and 01_REQUIREMENTS.md Explicitly Forbidden Behavior: "The
        system must never dispatch an email, message, or any external
        communication as part of this feature.\""""
        make_scoped_requirement(scoped_audit["audit"], confirmed=True)
        set_llm_client(FakeLLM(drafted("1.1.1")))
        login(api_client, scoped_audit["auditor"])

        api_client.post(f"/api/audits/{scoped_audit['audit'].id}/evidence-requests/generate")

        rows = db.scalars(select(EvidenceRequest)).all()
        assert rows
        assert all(r.status == EvidenceRequestStatus.draft for r in rows)

    def test_the_service_module_contains_no_outbound_send_path(self) -> None:
        """Asserted structurally so no future edit can quietly add one.

        ADR-004 records that sending was deliberately deferred, not forgotten —
        this is what keeps that decision from eroding.
        """
        from pathlib import Path

        source = (
            Path(__file__).resolve().parent.parent / "app" / "services" / "evidence_request.py"
        ).read_text()

        for forbidden in ("smtplib", "sendmail", "send_mail", "EmailMessage", "requests.post"):
            assert forbidden not in source

    def test_descriptions_are_plain_language_not_just_a_clause_number(
        self,
        api_client: TestClient,
        make_requirement: Any,
        make_scoped_requirement: Any,
        scoped_audit: dict[str, Any],
    ) -> None:
        """01_REQUIREMENTS.md: "a plain-language description of what's needed
        (not just the clause number)"."""
        requirement = make_requirement(control_id="1.2.1")
        make_scoped_requirement(scoped_audit["audit"], confirmed=True, requirement=requirement)
        set_llm_client(
            FakeLLM(
                {
                    "requests": [
                        {
                            "control_id": "1.2.1",
                            "description": "Please export your firewall rule set as a PDF.",
                        }
                    ]
                }
            )
        )
        login(api_client, scoped_audit["auditor"])

        body = api_client.post(
            f"/api/audits/{scoped_audit['audit'].id}/evidence-requests/generate"
        ).json()

        assert body["created"][0]["description"] == (
            "Please export your firewall rule set as a PDF."
        )
        assert body["created"][0]["description_source"] == "llm"


class TestNoDuplicates:
    """TASK-015: "Verify no duplicate requests generated for already-satisfied
    requirements." 01_REQUIREMENTS.md Edge Cases: re-running "only drafts
    requests for genuinely still-missing items"."""

    def test_rerunning_creates_nothing_for_requirements_already_requested(
        self,
        api_client: TestClient,
        db: DBSession,
        make_requirement: Any,
        make_scoped_requirement: Any,
        scoped_audit: dict[str, Any],
    ) -> None:
        for control_id in ("1.2.1", "3.3.1"):
            requirement = make_requirement(control_id=control_id, family=int(control_id[0]))
            make_scoped_requirement(scoped_audit["audit"], confirmed=True, requirement=requirement)
        set_llm_client(FakeLLM(drafted("1.2.1", "3.3.1")))
        login(api_client, scoped_audit["auditor"])
        url = f"/api/audits/{scoped_audit['audit'].id}/evidence-requests/generate"
        assert len(api_client.post(url).json()["created"]) == 2

        second = api_client.post(url).json()

        assert second["created"] == []
        assert second["skipped_already_requested"] == 2
        assert db.scalar(select(func.count()).select_from(EvidenceRequest)) == 2

    def test_a_newly_confirmed_requirement_gets_a_request_on_rerun(
        self,
        api_client: TestClient,
        db: DBSession,
        make_requirement: Any,
        make_scoped_requirement: Any,
        scoped_audit: dict[str, Any],
    ) -> None:
        """The mixed case from 01_REQUIREMENTS.md's acceptance criterion: with
        some requirements already covered, only the genuinely-missing ones
        produce new rows."""
        audit = scoped_audit["audit"]
        first = make_requirement(control_id="1.2.1")
        make_scoped_requirement(audit, confirmed=True, requirement=first)
        set_llm_client(FakeLLM(drafted("1.2.1")))
        login(api_client, scoped_audit["auditor"])
        url = f"/api/audits/{audit.id}/evidence-requests/generate"
        api_client.post(url)

        second = make_requirement(control_id="3.3.1", family=3)
        make_scoped_requirement(audit, confirmed=True, requirement=second)
        set_llm_client(FakeLLM(drafted("1.2.1", "3.3.1")))

        result = api_client.post(url).json()

        assert len(result["created"]) == 1
        assert result["created"][0]["control_id"] == "3.3.1"
        assert result["skipped_already_requested"] == 1
        assert db.scalar(select(func.count()).select_from(EvidenceRequest)) == 2

    def test_thirty_of_forty_requirements_produce_thirty_requests(
        self,
        api_client: TestClient,
        db: DBSession,
        make_requirement: Any,
        make_scoped_requirement: Any,
        scoped_audit: dict[str, Any],
    ) -> None:
        """01_REQUIREMENTS.md acceptance criterion, at its stated scale: "Given
        a confirmed scope with 40 requirements and evidence already on file for
        10 ... exactly 30 draft EvidenceRequest rows are created"."""
        from app.repositories.scoping import EvidenceRequestRepository

        audit = scoped_audit["audit"]
        scoped_rows = []
        for index in range(40):
            requirement = make_requirement(control_id=f"1.{index + 1}.1")
            scoped_rows.append(
                make_scoped_requirement(audit, confirmed=True, requirement=requirement)
            )

        # Ten already have a request on file.
        repo = EvidenceRequestRepository(db)
        for scoped in scoped_rows[:10]:
            repo.create(
                audit_id=audit.id,
                scoped_control_id=scoped.id,
                description="Already requested.",
                description_source="template",
            )

        set_llm_client(FakeLLM(raises=LLMTimeoutError("use templates")))
        login(api_client, scoped_audit["auditor"])

        result = api_client.post(f"/api/audits/{audit.id}/evidence-requests/generate").json()

        assert len(result["created"]) == 30
        assert result["skipped_already_requested"] == 10
        assert db.scalar(select(func.count()).select_from(EvidenceRequest)) == 40


class TestLLMFallback:
    """01_REQUIREMENTS.md External Dependencies: the description "falls back to
    a template-based description referencing the raw clause text if the LLM call
    fails — this feature must never fail outright"."""

    @pytest.mark.parametrize(
        "failure",
        [
            LLMTimeoutError("timed out"),
            LLMError("LLM request failed with status 503"),
            LLMError("The model did not return parseable JSON."),
        ],
    )
    def test_requests_are_still_created_from_templates(
        self,
        api_client: TestClient,
        make_requirement: Any,
        make_scoped_requirement: Any,
        scoped_audit: dict[str, Any],
        failure: Exception,
    ) -> None:
        requirement = make_requirement(
            control_id="1.2.1", name="Configuration standards for NSC rulesets"
        )
        make_scoped_requirement(scoped_audit["audit"], confirmed=True, requirement=requirement)
        set_llm_client(FakeLLM(raises=failure))
        login(api_client, scoped_audit["auditor"])

        response = api_client.post(
            f"/api/audits/{scoped_audit['audit'].id}/evidence-requests/generate"
        )

        assert response.status_code == 200, "this feature must never fail outright"
        body = response.json()
        assert len(body["created"]) == 1
        assert body["llm_available"] is False
        assert body["created"][0]["description_source"] == "template"
        # The fallback text is specific to the clause, not a generic placeholder.
        assert "1.2.1" in body["created"][0]["description"]
        assert "Configuration standards" in body["created"][0]["description"]

    def test_a_malformed_response_shape_also_falls_back(
        self,
        api_client: TestClient,
        make_scoped_requirement: Any,
        scoped_audit: dict[str, Any],
    ) -> None:
        make_scoped_requirement(scoped_audit["audit"], confirmed=True)
        set_llm_client(FakeLLM({"unexpected": "shape"}))
        login(api_client, scoped_audit["auditor"])

        response = api_client.post(
            f"/api/audits/{scoped_audit['audit'].id}/evidence-requests/generate"
        )

        assert response.status_code == 200
        assert response.json()["llm_available"] is False

    def test_a_partial_llm_response_fills_the_gaps_from_templates(
        self,
        api_client: TestClient,
        make_requirement: Any,
        make_scoped_requirement: Any,
        scoped_audit: dict[str, Any],
    ) -> None:
        """A model that answers for two of three requirements must not leave the
        third without a request — that would be a silent gap in the checklist."""
        for control_id in ("1.2.1", "3.3.1", "8.3.6"):
            requirement = make_requirement(control_id=control_id, family=int(control_id[0]))
            make_scoped_requirement(scoped_audit["audit"], confirmed=True, requirement=requirement)
        set_llm_client(FakeLLM(drafted("1.2.1", "3.3.1")))
        login(api_client, scoped_audit["auditor"])

        body = api_client.post(
            f"/api/audits/{scoped_audit['audit'].id}/evidence-requests/generate"
        ).json()

        assert len(body["created"]) == 3
        descriptions = {r["control_id"]: r["description"] for r in body["created"]}
        assert "artifact for 1.2.1" in descriptions["1.2.1"]
        assert "8.3.6" in descriptions["8.3.6"]


class TestRequestEditing:
    def test_auditor_can_edit_a_draft_description(
        self,
        api_client: TestClient,
        make_scoped_requirement: Any,
        scoped_audit: dict[str, Any],
    ) -> None:
        """01_REQUIREMENTS.md Success Output: the list is "editable by the
        auditor" before they send it themselves."""
        make_scoped_requirement(scoped_audit["audit"], confirmed=True)
        set_llm_client(FakeLLM(drafted("1.1.1")))
        login(api_client, scoped_audit["auditor"])
        created = api_client.post(
            f"/api/audits/{scoped_audit['audit'].id}/evidence-requests/generate"
        ).json()["created"][0]

        response = api_client.patch(
            f"/api/evidence-requests/{created['id']}",
            json={"description": "Reworded for the client contact."},
        )

        assert response.status_code == 200
        assert response.json()["description"] == "Reworded for the client contact."

    def test_marking_sent_externally_is_a_note_to_self(
        self,
        api_client: TestClient,
        db: DBSession,
        make_scoped_requirement: Any,
        scoped_audit: dict[str, Any],
    ) -> None:
        """ADR-004: `sent_externally` is the auditor's own record that they sent
        it through their channel. The system does not verify delivery and never
        claims to."""
        make_scoped_requirement(scoped_audit["audit"], confirmed=True)
        set_llm_client(FakeLLM(drafted("1.1.1")))
        login(api_client, scoped_audit["auditor"])
        created = api_client.post(
            f"/api/audits/{scoped_audit['audit'].id}/evidence-requests/generate"
        ).json()["created"][0]

        response = api_client.patch(
            f"/api/evidence-requests/{created['id']}", json={"status": "sent_externally"}
        )

        assert response.status_code == 200
        assert response.json()["status"] == "sent_externally"

    def test_unassigned_auditor_cannot_edit(
        self,
        api_client: TestClient,
        make_user: Any,
        make_scoped_requirement: Any,
        scoped_audit: dict[str, Any],
    ) -> None:
        make_scoped_requirement(scoped_audit["audit"], confirmed=True)
        set_llm_client(FakeLLM(drafted("1.1.1")))
        login(api_client, scoped_audit["auditor"])
        created = api_client.post(
            f"/api/audits/{scoped_audit['audit'].id}/evidence-requests/generate"
        ).json()["created"][0]

        intruder = make_user(Role.auditor, password=PASSWORD)
        login(api_client, intruder)
        response = api_client.patch(
            f"/api/evidence-requests/{created['id']}", json={"description": "Injected."}
        )

        assert response.status_code == 404

    def test_listing_is_ownership_filtered(
        self,
        api_client: TestClient,
        make_user: Any,
        make_scoped_requirement: Any,
        scoped_audit: dict[str, Any],
    ) -> None:
        make_scoped_requirement(scoped_audit["audit"], confirmed=True)
        intruder = make_user(Role.auditor, password=PASSWORD)
        login(api_client, intruder)

        response = api_client.get(f"/api/audits/{scoped_audit['audit'].id}/evidence-requests")

        assert response.status_code == 403
