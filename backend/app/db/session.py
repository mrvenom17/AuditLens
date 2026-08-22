"""Engine and session management.

Sessions are synchronous. SQLAlchemy's sync API under FastAPI's threadpool is
adequate at the documented scale (02_ARCHITECTURE.md §7.9: 5-20 users) and keeps
the repository layer far simpler than the async equivalent, which matters more
here than throughput does.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import settings

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,  # a self-hosted Postgres restart shouldn't wedge the pool
    pool_size=5,
    max_overflow=10,
    echo=False,  # never True: statements carry evidence text (05_SECURITY.md §10.7)
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency. One session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for non-request callers (worker, seed scripts).

    Commits on success, rolls back on any exception, and always closes. The
    exception is re-raised — errors are never swallowed here
    (06_ENGINEERING_RULES.md § Error Handling).
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
