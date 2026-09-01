"""`ControlEvaluation.result` has no API write path (05_SECURITY.md §10.3).

08_TESTING.md § Security Tests: "no code path allows an API request body to set
`ControlEvaluation.result` — a specific test that attempts this via the
Finding-review endpoint and via any other endpoint touching evaluations,
confirming the field is unreachable."

This is the property four separate documents state independently, and the one
that is hardest to get right by convention alone. So it is tested three ways:

1. **Structurally** — no Pydantic model reachable from any route accepts the
   field, so there is no name a client could send that would bind to it.
2. **Behaviourally** — sending it anyway, at every endpoint that touches an
   evaluation, changes nothing.
3. **By construction** — the repository that writes it exposes no update method.

A permission check would be a weaker guarantee than this: permissions can be
misconfigured, and an Admin would still have to be trusted. A field with no
writer cannot be written by anyone.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as DBSession

from app.main import app
from app.models.enums import EvaluationResult, Role
from tests import testcompany as tc

PASSWORD = "correct-horse-battery-staple"

# Any of these arriving in a request body would be a red flag.
FORBIDDEN_FIELDS = {
    "result",
    "system_result",
    "gate_status",
    "engine_version",
    "facts_used",
    "rules_used",
    "llm_involved",
    # Added with the applicability and strength work: all four are machine
    # determinations, and none has a legitimate external writer either.
    "evidence_strength",
    "strength_factors",
    "applicability_status",
    "applicability_evidence",
}


class TestNoRequestModelExposesTheField:
    def test_no_route_accepts_a_field_that_could_set_a_result(self) -> None:
        """Walks every registered route's request body schema.

        Checked against the live OpenAPI schema rather than by grepping source,
        so a model added in future is covered the moment it is wired to a route.
        """
        schema = app.openapi()
        components = schema.get("components", {}).get("schemas", {})

        offenders: list[str] = []
        for path, operations in schema.get("paths", {}).items():
            for method, operation in operations.items():
                body = operation.get("requestBody")
                if not body:
                    continue
                for content in body.get("content", {}).values():
                    ref = content.get("schema", {}).get("$ref", "")
                    name = ref.rsplit("/", 1)[-1]
                    properties = components.get(name, {}).get("properties", {})
                    for field in properties:
                        if field in FORBIDDEN_FIELDS:
                            offenders.append(f"{method.upper()} {path} → {name}.{field}")

        assert not offenders, (
            f"A request body can reach a field that determines compliance truth: {offenders}"
        )

    def test_the_review_schema_has_no_system_result_field(self) -> None:
        """04_API_CONTRACT.md → PATCH /api/findings/{id}/review, Security Notes:
        "there is no field name that could even be misused to overwrite it"."""
        from app.schemas.finding import FindingReviewRequest

        assert set(FindingReviewRequest.model_fields) == {
            "action",
            "auditor_decision",
            "note",
        }

    def test_the_evaluate_endpoint_takes_no_body_at_all(self) -> None:
        """Its entire contract is mechanical inputs, mechanical output — no
        confidence threshold, no "let AI decide ties", no LLM parameter."""
        schema = app.openapi()
        operation = schema["paths"]["/api/audits/{audit_id}/evaluate"]["post"]
        assert "requestBody" not in operation


class TestSendingItAnywayChangesNothing:
    def test_the_review_endpoint_ignores_an_injected_result(
        self,
        db: DBSession,
        api_client: TestClient,
        make_user: Any,
        upload: Any,
        run_pipeline: Any,
        test_audit: Any,
    ) -> None:
        from app.services.finding import FindingService

        results = run_pipeline([upload(tc.PASSWORD_CONFIG)])
        evaluation = results["8.3.6"]
        assert evaluation.result == EvaluationResult.PASS

        finding = FindingService(db).create_for_evaluation(test_audit.id, evaluation)
        db.commit()

        reviewer = make_user(Role.reviewer, password=PASSWORD)
        api_client.post("/api/auth/login", json={"email": reviewer.email, "password": PASSWORD})

        response = api_client.patch(
            f"/api/findings/{finding.id}/review",
            json={
                "action": "approve",
                "result": "FAIL",
                "system_result": "FAIL",
                "gate_status": "VERIFIED",
                "engine_version": "attacker-1.0",
                "llm_involved": False,
            },
        )

        assert response.status_code == 200
        assert response.json()["system_result"] == "PASS"

        db.expire_all()
        db.refresh(evaluation)
        assert evaluation.result == EvaluationResult.PASS
        assert evaluation.engine_version != "attacker-1.0"

    def test_the_evaluate_endpoint_ignores_an_injected_body(
        self,
        db: DBSession,
        api_client: TestClient,
        make_user: Any,
        upload: Any,
        test_audit: Any,
    ) -> None:
        """A body sent to an endpoint that declares none is discarded by the
        framework — asserted rather than assumed, because "it probably ignores
        it" is not a security control."""
        from app.models.audit import AuditAssignment

        upload(tc.PASSWORD_CONFIG)
        reviewer = make_user(Role.reviewer, password=PASSWORD)
        db.add(AuditAssignment(audit_id=test_audit.id, user_id=reviewer.id))
        db.commit()

        api_client.post("/api/auth/login", json={"email": reviewer.email, "password": PASSWORD})
        response = api_client.post(
            f"/api/audits/{test_audit.id}/evaluate",
            json={"result": "PASS", "confidence_threshold": 0.0, "let_ai_decide": True},
        )

        assert response.status_code == 200
        for evaluation in response.json()["evaluations"]:
            # Whatever the injected body asked for, the results are whatever the
            # rules produced — and every one is stamped as LLM-free.
            assert evaluation["llm_involved"] is False
            assert evaluation["engine_version"] != "attacker-1.0"


class TestOnlyTheEngineWrites:
    def test_the_repository_exposes_no_update_or_delete(self) -> None:
        """Append-only by construction: a re-evaluation is a new row. The
        absence of a mutation method is the cheapest way to keep that true."""
        from app.repositories.evaluation import ControlEvaluationRepository

        methods = {name for name in dir(ControlEvaluationRepository) if not name.startswith("_")}
        assert not {"update", "delete", "set_result", "save"} & methods

    def test_the_finding_service_never_assigns_to_a_result(self) -> None:
        """The review path writes `auditor_decision` and nothing else that could
        be confused with the machine's answer."""
        import ast
        import pathlib

        from app.services import finding as finding_service

        tree = ast.parse(pathlib.Path(finding_service.__file__).read_text())
        assigned: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute):
                        assigned.add(target.attr)

        assert "result" not in assigned
        assert "auditor_decision" in assigned
