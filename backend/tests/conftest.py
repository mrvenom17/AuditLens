"""Shared test fixtures.

08_TESTING.md § Test Data Strategy requires each test to run against a fresh or
transaction-rolled-back database with no shared mutable state, and factory
helpers for User (per role), Audit (per status) and Finding (per state).

The schema is built once per session by running the real Alembic migrations
rather than `Base.metadata.create_all`, so the migrations themselves are
exercised on every test run — a schema that only exists in the models is a
schema that has never been deployed.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from app.config.settings import settings
from app.models.audit import Audit, AuditAssignment
from app.models.corpus import ControlDefinition
from app.models.enums import (
    AuditStatus,
    EntityType,
    EvaluationMode,
    EvaluationResult,
    FactValueType,
    FindingStatus,
    GateStatus,
    MerchantLevel,
    Role,
    ScopeSource,
    VerificationStatus,
)
from app.models.evaluation import ControlEvaluation, EvidenceFact
from app.models.finding import Finding
from app.models.scoping import ScopedControl
from app.models.user import User

BACKEND_DIR = Path(__file__).resolve().parent.parent
TEST_DB_NAME = "auditlens_test"


def _test_database_url() -> str:
    # `str(URL)` renders the password as "***" — this must be the real string,
    # since it is handed to the Alembic subprocess as DATABASE_URL.
    return (
        make_url(settings.DATABASE_URL)
        .set(database=TEST_DB_NAME)
        .render_as_string(hide_password=False)
    )


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """Create the test database, migrate it, and drop it afterwards."""
    admin_url = make_url(settings.DATABASE_URL).set(database="postgres")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    admin.dispose()

    result = subprocess.run(  # noqa: S603
        [str(BACKEND_DIR / ".venv" / "bin" / "alembic"), "upgrade", "head"],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": _test_database_url()},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"Test database migration failed:\n{result.stderr}")

    test_engine = create_engine(_test_database_url())
    yield test_engine
    test_engine.dispose()

    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    """One connection per test, wrapped in a transaction that is always rolled
    back — no test can leak state into another."""
    conn = engine.connect()
    transaction = conn.begin()
    try:
        yield conn
    finally:
        transaction.rollback()
        conn.close()


@pytest.fixture
def db(connection: Connection) -> Iterator[Session]:
    """A Session bound to the rolled-back connection.

    `join_transaction_mode="create_savepoint"` lets application code call
    `commit()` normally — those commits land on a savepoint inside the outer
    transaction, which the fixture then discards. Without it, service-layer code
    under test would have to behave differently than it does in production.
    """
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def isolated_file_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point evidence storage at a per-test temporary directory.

    Autouse because a test that writes a real file into the configured storage
    root would leave it there — uploaded evidence is append-only by design, so
    nothing in the application will ever clean it up.
    """
    storage = tmp_path / "evidence-storage"
    storage.mkdir()
    monkeypatch.setattr(settings, "FILE_STORAGE_PATH", str(storage))
    return storage


@pytest.fixture
def api_client(db: Session) -> Iterator[TestClient]:
    """A TestClient whose requests run inside the test's own transaction.

    Overriding `get_db` to yield the test session is what lets an HTTP test and
    the factory fixtures see the same uncommitted rows, and lets the whole
    request be rolled back afterwards. `commit()` inside a request lands on a
    savepoint (see the `db` fixture), so route code behaves exactly as it does
    in production.
    """
    from app.db.session import get_db
    from app.main import app

    def _override_get_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


# --- Factories ---------------------------------------------------------------
# Deliberately plain functions rather than a factory library: they are called
# from tests only, the argument lists are short, and a dependency here would buy
# nothing (06_ENGINEERING_RULES.md § Dependency Rules).


def _unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@testfirm.example"


@pytest.fixture
def make_user(db: Session) -> Any:
    def _make(
        role: Role = Role.auditor,
        *,
        is_active: bool = True,
        password: str = "correct-horse-battery-staple",
        email: str | None = None,
        name: str = "Test User",
    ) -> User:
        from app.auth.password import hash_password

        user = User(
            email=email or _unique_email(role.value),
            password_hash=hash_password(password),
            name=name,
            role=role,
            is_active=is_active,
        )
        db.add(user)
        db.flush()
        return user

    return _make


@pytest.fixture
def make_audit(db: Session) -> Any:
    def _make(
        creator: User,
        *,
        status: AuditStatus = AuditStatus.intake,
        client_name: str = "Test Client Ltd",
        entity_type: EntityType = EntityType.merchant,
        merchant_level: MerchantLevel | None = MerchantLevel.four,
        assign: bool = True,
    ) -> Audit:
        audit = Audit(
            client_name=client_name,
            entity_type=entity_type,
            merchant_level=merchant_level,
            status=status,
            created_by=creator.id,
        )
        db.add(audit)
        db.flush()
        if assign:
            db.add(AuditAssignment(audit_id=audit.id, user_id=creator.id))
            db.flush()
        return audit

    return _make


@pytest.fixture
def make_requirement(db: Session) -> Any:
    """A corpus clause. Uses the fixed fixture set described in 08_TESTING.md,
    not the full corpus load."""
    counter = {"n": 0}

    def _make(
        control_id: str | None = None,
        *,
        family: int = 1,
        name: str = "Install and maintain network security controls",
        requirement_text: str = "Network security controls are in place between networks.",
        evaluation_mode: EvaluationMode = EvaluationMode.HUMAN_ASSISTED,
        facts: list[dict[str, Any]] | None = None,
        rules: list[dict[str, Any]] | None = None,
        freshness_window_days: int | None = None,
    ) -> ControlDefinition:
        counter["n"] += 1
        requirement = ControlDefinition(
            control_id=control_id or f"1.{counter['n']}.1",
            requirement_family=family,
            name=name,
            requirement_text=requirement_text,
            evaluation_mode=evaluation_mode,
            facts=facts or [],
            rules=rules or [],
            freshness_window_days=freshness_window_days,
            corpus_version="v4.0.1-test",
        )
        db.add(requirement)
        db.flush()
        return requirement

    return _make


@pytest.fixture
def make_deterministic_control(make_requirement: Any) -> Any:
    """A control the rule engine can actually execute.

    Defaults to the frozen corpus's password-length control, which is the
    simplest possible shape: one integer fact, one comparison.
    """

    def _make(
        control_id: str = "8.3.6",
        *,
        fact_name: str = "minimum_password_length",
        value_type: FactValueType = FactValueType.integer,
        operator: str = ">=",
        expected: Any = 12,
        freshness_window_days: int | None = None,
    ) -> ControlDefinition:
        return make_requirement(
            control_id,
            family=int(control_id.split(".")[0]),
            name="Minimum password length",
            requirement_text="Passwords are at least 12 characters.",
            evaluation_mode=EvaluationMode.DETERMINISTIC,
            facts=[{"name": fact_name, "type": value_type.value}],
            rules=[{"fact": fact_name, "operator": operator, "expected": expected}],
            freshness_window_days=freshness_window_days,
        )

    return _make


@pytest.fixture
def make_evaluation(db: Session) -> Any:
    """A ControlEvaluation written directly, for tests about what happens
    *after* the engine runs. Tests of the engine itself call it directly."""

    def _make(
        audit: Audit,
        control: ControlDefinition,
        *,
        result: EvaluationResult = EvaluationResult.PASS,
        gate_status: GateStatus = GateStatus.VERIFIED,
        gate_checks_failed: list[str] | None = None,
        evaluation_mode: EvaluationMode = EvaluationMode.DETERMINISTIC,
        engine_version: str = "1.0.0",
        llm_involved: bool = False,
        evidence_locations: list[dict[str, Any]] | None = None,
        contradictions: list[dict[str, Any]] | None = None,
        stale: bool = False,
    ) -> ControlEvaluation:
        evaluation = ControlEvaluation(
            audit_id=audit.id,
            control_definition_id=control.id,
            result=result,
            evaluation_mode=evaluation_mode,
            facts_used=[],
            rules_used=control.rules or [],
            evidence_locations=evidence_locations or [],
            contradictions=contradictions,
            stale=stale,
            gate_status=gate_status,
            gate_checks_failed=gate_checks_failed or [],
            engine_version=engine_version,
            llm_involved=llm_involved,
        )
        db.add(evaluation)
        db.flush()
        return evaluation

    return _make


@pytest.fixture
def make_fact(db: Session) -> Any:
    def _make(
        audit: Audit,
        control: ControlDefinition,
        document: Any,
        *,
        name: str = "minimum_password_length",
        value: str = "14",
        value_type: FactValueType = FactValueType.integer,
        page: int | None = 1,
        line: int | None = None,
        cell: str | None = None,
        source_hash: str | None = None,
        observed_at: datetime | None = None,
        verification_status: VerificationStatus = VerificationStatus.VERIFIED,
    ) -> EvidenceFact:
        fact = EvidenceFact(
            audit_id=audit.id,
            control_definition_id=control.id,
            document_id=document.id,
            name=name,
            value=value,
            value_type=value_type,
            page=page,
            line=line,
            cell=cell,
            source_hash=source_hash or document.content_hash,
            observed_at=observed_at,
            extractor_version="test-1.0.0",
            verification_status=verification_status,
        )
        db.add(fact)
        db.flush()
        return fact

    return _make


@pytest.fixture
def make_scoped_requirement(db: Session, make_requirement: Any) -> Any:
    def _make(
        audit: Audit,
        *,
        confirmed: bool = True,
        source: ScopeSource = ScopeSource.ai_suggested,
        requirement: ControlDefinition | None = None,
        gap_acknowledged: bool = False,
    ) -> ScopedControl:
        scoped = ScopedControl(
            audit_id=audit.id,
            control_definition_id=(requirement or make_requirement()).id,
            source=source,
            confirmed=confirmed,
            gap_acknowledged=gap_acknowledged,
        )
        db.add(scoped)
        db.flush()
        return scoped

    return _make


@pytest.fixture
def make_finding(db: Session, make_scoped_requirement: Any, make_evaluation: Any) -> Any:
    """A Finding wrapping a ControlEvaluation.

    `system_result` and `auditor_decision` are separate parameters here for the
    same reason they are separate columns: a test must be able to construct the
    case where they disagree, because that is the case the product's audit trail
    exists to preserve.
    """

    def _make(
        audit: Audit,
        *,
        status: FindingStatus = FindingStatus.pending_review,
        system_result: EvaluationResult = EvaluationResult.PASS,
        gate_status: GateStatus = GateStatus.VERIFIED,
        auditor_decision: EvaluationResult | None = None,
        reviewed_by: User | None = None,
        scoped_control: ScopedControl | None = None,
        ai_explanation: str | None = None,
        evaluation: ControlEvaluation | None = None,
    ) -> Finding:
        scoped = scoped_control or make_scoped_requirement(audit)
        if evaluation is None:
            evaluation = make_evaluation(
                audit,
                scoped.control,
                result=system_result,
                gate_status=gate_status,
            )
        finding = Finding(
            audit_id=audit.id,
            control_evaluation_id=evaluation.id,
            scoped_control_id=scoped.id,
            ai_explanation=ai_explanation,
            status=status,
            auditor_decision=auditor_decision,
            reviewed_by=reviewed_by.id if reviewed_by else None,
            reviewed_at=datetime.now(UTC) if reviewed_by else None,
        )
        db.add(finding)
        db.flush()
        return finding

    return _make
