"""Shared test fixtures.

08_TESTING.md § Test Data Strategy requires each test to run against a fresh or
transaction-rolled-back database with no shared mutable state, and factory
helpers for User (per role), Engagement (per status) and Finding (per state).

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
from app.models.corpus import PCIRequirement
from app.models.engagement import Engagement, EngagementAssignment
from app.models.enums import (
    ComplianceStatus,
    EngagementStatus,
    EntityType,
    FindingStatus,
    MerchantLevel,
    Role,
    ScopeSource,
)
from app.models.finding import Finding
from app.models.scoping import ScopedRequirement
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
def make_engagement(db: Session) -> Any:
    def _make(
        creator: User,
        *,
        status: EngagementStatus = EngagementStatus.intake,
        client_name: str = "Test Client Ltd",
        entity_type: EntityType = EntityType.merchant,
        merchant_level: MerchantLevel | None = MerchantLevel.four,
        assign: bool = True,
    ) -> Engagement:
        engagement = Engagement(
            client_name=client_name,
            entity_type=entity_type,
            merchant_level=merchant_level,
            status=status,
            created_by=creator.id,
        )
        db.add(engagement)
        db.flush()
        if assign:
            db.add(EngagementAssignment(engagement_id=engagement.id, user_id=creator.id))
            db.flush()
        return engagement

    return _make


@pytest.fixture
def make_requirement(db: Session) -> Any:
    """A corpus clause. Uses the fixed fixture set described in 08_TESTING.md,
    not the full corpus load."""
    counter = {"n": 0}

    def _make(
        clause_id: str | None = None,
        *,
        family: int = 1,
        title: str = "Install and maintain network security controls",
        full_text: str = "Network security controls are in place between networks.",
    ) -> PCIRequirement:
        counter["n"] += 1
        requirement = PCIRequirement(
            clause_id=clause_id or f"1.{counter['n']}.1",
            requirement_family=family,
            title=title,
            full_text=full_text,
            corpus_version="v4.0.1-test",
        )
        db.add(requirement)
        db.flush()
        return requirement

    return _make


@pytest.fixture
def make_scoped_requirement(db: Session, make_requirement: Any) -> Any:
    def _make(
        engagement: Engagement,
        *,
        confirmed: bool = True,
        source: ScopeSource = ScopeSource.ai_suggested,
        requirement: PCIRequirement | None = None,
        gap_acknowledged: bool = False,
    ) -> ScopedRequirement:
        scoped = ScopedRequirement(
            engagement_id=engagement.id,
            pci_requirement_id=(requirement or make_requirement()).id,
            source=source,
            confirmed=confirmed,
            gap_acknowledged=gap_acknowledged,
        )
        db.add(scoped)
        db.flush()
        return scoped

    return _make


@pytest.fixture
def make_finding(db: Session, make_scoped_requirement: Any) -> Any:
    def _make(
        engagement: Engagement,
        *,
        status: FindingStatus = FindingStatus.draft,
        ai_suggested_status: ComplianceStatus | None = ComplianceStatus.satisfied,
        ai_confidence: float | None = 0.85,
        needs_manual_review: bool = False,
        reviewed_by: User | None = None,
        final_status: ComplianceStatus | None = None,
        scoped_requirement: ScopedRequirement | None = None,
    ) -> Finding:
        scoped = scoped_requirement or make_scoped_requirement(engagement)
        finding = Finding(
            engagement_id=engagement.id,
            scoped_requirement_id=scoped.id,
            citations=[],
            evidence_document_ids=[],
            ai_suggested_status=ai_suggested_status,
            ai_confidence=ai_confidence,
            ai_rationale="Test rationale.",
            needs_manual_review=needs_manual_review,
            status=status,
            final_status=final_status,
            reviewed_by=reviewed_by.id if reviewed_by else None,
            reviewed_at=datetime.now(UTC) if reviewed_by else None,
        )
        db.add(finding)
        db.flush()
        return finding

    return _make
