"""Secret-hygiene tests (TASK-023).

08_TESTING.md requirement-to-test map: "No secrets in logs — a log-output test
that scans for known secret patterns after a full request cycle."

05_SECURITY.md §10.7 lists what must never be logged: `password_hash`, session
tokens/cookies, LLM/embedding API keys, full request/response bodies containing
`extracted_text` or `tech_stack_summary`, and full LLM prompts or completions
(metadata only — duration, status, token count).

The tests here run real request cycles and inspect everything that reached the
logging system, rather than asserting that particular call sites behave — a
leak is most likely to come from a call site nobody thought about.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as DBSession

from app.logging_setup import SecretRedactingFilter
from app.models.enums import AuditStatus, Role
from tests import filefixtures as ff

PASSWORD = "correct-horse-battery-staple"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Values planted in the requests below. If any appears in log output, something
# is logging a payload it should not be.
SENSITIVE_TECH_STACK = "CANARY-TECHSTACK-Magento-on-AWS-with-Stripe-tokenisation"
SENSITIVE_CLIENT = "CANARY-CLIENT-Northwind"


def login(client: TestClient, user: Any) -> None:
    assert (
        client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD}).status_code
        == 200
    )


def captured(caplog: pytest.LogCaptureFixture) -> str:
    """Everything that reached the logging system during the test."""
    return "\n".join(
        [record.getMessage() for record in caplog.records]
        + [str(record.args) for record in caplog.records if record.args]
    )


class TestNoSecretsInLogsAfterAFullRequestCycle:
    def test_login_does_not_log_the_password_or_the_hash(
        self, api_client: TestClient, make_user: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        user = make_user(Role.auditor, password=PASSWORD)

        with caplog.at_level(logging.DEBUG):
            api_client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD})

        output = captured(caplog)
        assert PASSWORD not in output
        assert user.password_hash not in output
        assert "$argon2" not in output

    def test_login_does_not_log_the_email_in_clear(
        self, api_client: TestClient, make_user: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """02_ARCHITECTURE.md §7.8 requires every authentication attempt be
        logged. Recording which account was targeted is the point — recording
        it in clear would turn the log file into a roster of the firm's staff,
        so the subject is hashed."""
        user = make_user(Role.auditor, password=PASSWORD)

        with caplog.at_level(logging.DEBUG):
            api_client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD})

        output = captured(caplog)
        assert user.email not in output
        # The attempt is still recorded, just against a fingerprint.
        assert "auth.attempt" in output

    def test_failed_login_does_not_log_the_attempted_password(
        self, api_client: TestClient, make_user: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed attempt is the most tempting thing to log verbosely, and the
        submitted password is often a *correct* password for another system."""
        user = make_user(Role.auditor, password=PASSWORD)

        with caplog.at_level(logging.DEBUG):
            api_client.post(
                "/api/auth/login",
                json={"email": user.email, "password": "hunter2-someone-elses-password"},
            )

        assert "hunter2-someone-elses-password" not in captured(caplog)

    def test_session_token_is_never_logged(
        self, api_client: TestClient, make_user: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        from app.services.auth import SESSION_COOKIE_NAME

        user = make_user(Role.auditor, password=PASSWORD)
        login(api_client, user)
        token = api_client.cookies.get(SESSION_COOKIE_NAME)
        assert token

        with caplog.at_level(logging.DEBUG):
            api_client.get("/api/auth/me")

        assert token not in captured(caplog)

    def test_audit_creation_does_not_log_sensitive_profile_fields(
        self, api_client: TestClient, make_user: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`tech_stack_summary` is Sensitive (03_DATA_MODEL.md §8.4) and
        05_SECURITY.md §10.7 names it explicitly."""
        auditor = make_user(Role.auditor, password=PASSWORD)
        login(api_client, auditor)

        with caplog.at_level(logging.DEBUG):
            response = api_client.post(
                "/api/audits",
                json={
                    "client_name": SENSITIVE_CLIENT,
                    "entity_type": "merchant",
                    "merchant_level": "2",
                    "tech_stack_summary": SENSITIVE_TECH_STACK,
                },
            )
        assert response.status_code == 201

        assert SENSITIVE_TECH_STACK not in captured(caplog)

    def test_evidence_upload_does_not_log_document_content(
        self,
        api_client: TestClient,
        make_user: Any,
        make_audit: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """`extracted_text` is Sensitive and is the largest concentration of
        client-confidential material in the system."""
        secret_content = "CANARY-EVIDENCE-root-password-is-hunter2"
        auditor = make_user(Role.auditor, password=PASSWORD)
        audit = make_audit(auditor, status=AuditStatus.in_progress)
        login(api_client, auditor)

        with caplog.at_level(logging.DEBUG):
            response = api_client.post(
                f"/api/audits/{audit.id}/evidence-documents",
                files={"file": ("e.pdf", ff.valid_pdf(secret_content), "application/pdf")},
            )
        assert response.status_code == 201

        assert secret_content not in captured(caplog)

    def test_extraction_logs_metadata_not_extracted_text(
        self,
        api_client: TestClient,
        db: DBSession,
        make_user: Any,
        make_audit: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import uuid

        from app.models.evidence import EvidenceDocument
        from app.pipelines.worker import process_extraction

        secret_content = "CANARY-EXTRACTED-cardholder-database-credentials"
        auditor = make_user(Role.auditor, password=PASSWORD)
        audit = make_audit(auditor, status=AuditStatus.in_progress)
        login(api_client, auditor)
        created = api_client.post(
            f"/api/audits/{audit.id}/evidence-documents",
            files={"file": ("e.pdf", ff.valid_pdf(secret_content), "application/pdf")},
        ).json()
        document = db.get(EvidenceDocument, uuid.UUID(created["id"]))
        assert document is not None

        with caplog.at_level(logging.DEBUG):
            process_extraction(db, document)

        output = captured(caplog)
        assert secret_content not in output
        # The event is still observable, just by its metadata.
        assert "extraction.complete" in output
        assert document.extracted_text is not None
        assert secret_content in document.extracted_text

    def test_llm_calls_log_metadata_only(
        self,
        db: DBSession,
        make_user: Any,
        make_audit: Any,
        make_deterministic_control: Any,
        make_evaluation: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """05_SECURITY.md §10.7: "full LLM prompts or completions (log only
        metadata: duration, status, token count)".

        The call site moved with the retrofit — the LLM no longer judges
        compliance, it drafts an explanation of an already-determined result —
        but the rule is unchanged, and the evidence values in that prompt are
        still client configuration detail.
        """
        from app.pipelines.llm import LLMResponse, set_llm_client
        from app.services.genai_service import draft_explanation

        secret_evidence = "CANARY-PROMPT-internal-network-10.0.0.0-8-admin-creds"

        class RecordingLLM:
            def complete(
                self, *, system: str, prompt: str, timeout: float, max_tokens: int = 2048
            ) -> LLMResponse:
                assert secret_evidence in prompt, "the test's premise requires it be sent"
                return LLMResponse(
                    text="CANARY-COMPLETION-the-model-said-this",
                    input_tokens=100,
                    output_tokens=20,
                )

        set_llm_client(RecordingLLM())
        try:
            auditor = make_user(Role.auditor)
            audit = make_audit(auditor, status=AuditStatus.in_progress)
            control = make_deterministic_control()
            evaluation = make_evaluation(
                audit,
                control,
                evidence_locations=[
                    {
                        "fact": "minimum_password_length",
                        "value": secret_evidence,
                        "location": "page 1",
                    }
                ],
            )
            with caplog.at_level(logging.DEBUG):
                explanation = draft_explanation(evaluation, control)
        finally:
            set_llm_client(None)

        output = captured(caplog)
        assert secret_evidence not in output, "the prompt reached the logs"
        assert "CANARY-COMPLETION-the-model-said-this" not in output, "the completion did"
        # The explanation is stored on the Finding, where it belongs — the rule
        # is about logs, not about the record.
        assert explanation == "CANARY-COMPLETION-the-model-said-this"

    def test_a_500_response_exposes_no_internals(self, db: DBSession) -> None:
        """02_ARCHITECTURE.md §7.7: the client only ever sees a generic message
        and a reference id. Stack traces, SQL, and driver detail stay
        server-side.

        The failure is injected through the `get_db` dependency because that is
        where a real database outage would surface, and its exception message is
        exactly the kind that carries an internal hostname and a username.
        """
        from collections.abc import Iterator

        from app.db.session import get_db
        from app.main import app

        leaky_message = (
            "psycopg.OperationalError: connection to server at 10.0.0.5 "
            'failed: FATAL: password authentication failed for user "auditlens_app"'
        )

        def exploding_db() -> Iterator[DBSession]:
            raise RuntimeError(leaky_message)
            yield  # pragma: no cover — unreachable, satisfies the generator protocol

        app.dependency_overrides[get_db] = exploding_db
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/health/ready")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 500
        body = response.json()
        assert body["error"]["code"] == "INTERNAL_ERROR"
        assert body["error"]["request_id"]
        assert body["error"]["request_id"] in body["error"]["message"]

        assert "psycopg" not in response.text
        assert "10.0.0.5" not in response.text
        assert "auditlens_app" not in response.text
        assert "Traceback" not in response.text
        assert "OperationalError" not in response.text


class TestRedactionBackstop:
    """`SecretRedactingFilter` is the second line of defence: call sites log
    metadata by construction, and this scrubs anything that still looks like a
    credential. Tested directly because it must work for the log line nobody
    anticipated."""

    @pytest.mark.parametrize(
        ("raw", "must_not_contain"),
        [
            ("key sk-ant-api03-AAAABBBBCCCCDDDDEEEE", "sk-ant-api03-AAAABBBBCCCCDDDDEEEE"),
            (
                "hash $argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$aGFzaA",
                "c29tZXNhbHQ",
            ),
            (
                "postgresql+psycopg://auditlens:s3cretpw@db:5432/auditlens",
                "s3cretpw",
            ),
            ("Cookie: auditlens_session=abc123XYZtoken", "abc123XYZtoken"),
            ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9", "eyJhbGciOiJIUzI1NiJ9"),
        ],
    )
    def test_credential_shaped_values_are_redacted(
        self, raw: str, must_not_contain: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        logger = logging.getLogger("test.redaction")
        logger.addFilter(SecretRedactingFilter())

        with caplog.at_level(logging.INFO, logger="test.redaction"):
            logger.info(raw)

        rendered = "\n".join(r.getMessage() for r in caplog.records)
        assert must_not_contain not in rendered
        assert "REDACTED" in rendered or "***" in rendered

    def test_redaction_leaves_ordinary_messages_intact(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Over-redaction would make the logs useless for the operational
        alerting 09_DEPLOYMENT.md asks for."""
        logger = logging.getLogger("test.redaction.clean")
        logger.addFilter(SecretRedactingFilter())

        with caplog.at_level(logging.INFO, logger="test.redaction.clean"):
            logger.info("extraction.complete document=%s sections=%d", "abc-123", 4)

        assert "extraction.complete document=abc-123 sections=4" in caplog.records[0].getMessage()

    def test_sqlalchemy_statement_logging_is_off(self) -> None:
        """SQLAlchemy echoes bound parameters at INFO, and those parameters
        include password hashes and extracted evidence text. This stays at
        WARNING in every environment, not just production."""
        from app.logging_setup import configure_logging

        for environment in ("local", "test", "production"):
            configure_logging(environment)
            assert logging.getLogger("sqlalchemy.engine").level >= logging.WARNING

    def test_engine_is_not_configured_to_echo(self) -> None:
        from app.db.session import engine

        assert engine.echo is False


class TestNoSecretsInVersionControl:
    """05_SECURITY.md §10.6: never hardcoded, never committed.

    Run against `git ls-files`, so it inspects what is actually tracked rather
    than what happens to be on disk.
    """

    def _tracked_files(self) -> list[str]:
        result = subprocess.run(
            ["git", "ls-files"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return [line for line in result.stdout.splitlines() if line]

    def test_no_env_file_is_tracked(self) -> None:
        tracked = self._tracked_files()
        offenders = [
            path
            for path in tracked
            if Path(path).name == ".env" or Path(path).name.startswith(".env.")
            if Path(path).name != ".env.example"
        ]
        assert not offenders, f"Environment files are tracked in git: {offenders}"

    def test_env_example_contains_only_placeholders(self) -> None:
        """09_DEPLOYMENT.md: "Never use real secrets in this table or in
        `.env.example` — placeholders only.\""""
        example = (REPO_ROOT / "backend" / ".env.example").read_text()

        for line in example.splitlines():
            if not line.strip() or line.strip().startswith("#") or "=" not in line:
                continue
            _, _, value = line.partition("=")
            value = value.strip()
            if not value:
                continue
            assert not re.match(r"^sk-[A-Za-z0-9_\-]{16,}$", value), f"real-looking key: {line}"
            # Any secret-bearing variable must obviously be a placeholder.
            if any(token in line for token in ("SECRET", "API_KEY", "PASSWORD")):
                assert "CHANGE_ME" in value, f"{line} does not look like a placeholder"

    def test_no_api_key_patterns_are_committed(self) -> None:
        key_pattern = re.compile(r"sk-[A-Za-z0-9_\-]{20,}")
        offenders: list[str] = []

        for relative in self._tracked_files():
            path = REPO_ROOT / relative
            if not path.is_file() or path.suffix in {".png", ".jpg", ".pdf", ".zip"}:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            # This module necessarily contains the pattern it searches for.
            if path.name in {"test_secret_hygiene.py", "logging_setup.py", "ci.yml"}:
                continue
            if key_pattern.search(content):
                offenders.append(relative)

        assert not offenders, f"Files matching an API-key pattern: {offenders}"

    def test_no_hardcoded_connection_strings_with_credentials(self) -> None:
        """A connection string carrying a real password is the most commonly
        committed secret in a project of this shape."""
        pattern = re.compile(r"postgresql(?:\+\w+)?://[^:\s\"']+:[^@\s\"']+@")
        # Values that are self-evidently not credentials: explicit placeholders,
        # the documented example format, the local-dev compose password (which
        # only ever reaches a loopback-bound container), and the CI password
        # (which guards an ephemeral database created and dropped per run).
        allowed_placeholders = (
            "CHANGE_ME",
            "user:pass",  # the format example in 09_DEPLOYMENT.md
            "auditlens:auditlens",  # the rejected development default
            "auditlens_local_dev",
            "ci_password",
            "app:realpw",  # a fixture in this test module's sibling test
            "***",
        )
        offenders: list[str] = []

        for relative in self._tracked_files():
            path = REPO_ROOT / relative
            if not path.is_file() or path.suffix not in {".py", ".toml", ".yml", ".yaml", ".md"}:
                continue
            if path.name in {"test_secret_hygiene.py", "logging_setup.py"}:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for match in pattern.finditer(content):
                if not any(token in match.group(0) for token in allowed_placeholders):
                    offenders.append(f"{relative}: {match.group(0)}")

        assert not offenders, f"Connection strings with embedded credentials: {offenders}"

    def test_settings_defaults_are_not_usable_in_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The development defaults exist so tests and tooling can import
        settings. `validate_for_environment` is what stops them reaching
        production, so it has to actually reject them."""
        from app.config.settings import DEV_SESSION_SECRET, Settings

        for var in ("DATABASE_URL", "SESSION_SECRET", "LLM_API_KEY", "CORS_ALLOWED_ORIGIN"):
            monkeypatch.delenv(var, raising=False)

        # `_env_file=None` so this tests the *defaults*, not whatever the
        # developer happens to have in their local .env.
        defaults = Settings(_env_file=None, ENVIRONMENT="production")
        with pytest.raises(RuntimeError) as exc:
            defaults.validate_for_environment()

        message = str(exc.value)
        assert "SESSION_SECRET" in message
        assert "DATABASE_URL" in message
        assert "LLM_API_KEY" in message
        # The failure names the variables, never their values.
        assert DEV_SESSION_SECRET not in message
        assert "auditlens:auditlens" not in message

    def test_a_fully_configured_production_environment_validates(self) -> None:
        """The guard must not be so strict that a correct deployment cannot
        start — a startup check that always fails gets disabled."""
        from app.config.settings import Settings

        production = Settings(
            _env_file=None,
            ENVIRONMENT="production",
            DATABASE_URL="postgresql+psycopg://app:realpw@db.internal:5432/auditlens",
            SESSION_SECRET="x" * 48,
            LLM_API_KEY="sk-ant-real-key-value-goes-here",
            CORS_ALLOWED_ORIGIN="https://auditlens.example.tld",
        )

        production.validate_for_environment()  # must not raise
