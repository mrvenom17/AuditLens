"""Health endpoints (02_ARCHITECTURE.md §7.8, 09_DEPLOYMENT.md § Health Checks)."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded"]
    database: bool
    worker_queue: bool


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness — the process is responsive. Touches nothing external."""
    return HealthResponse(status="ok")


@router.get("/health/ready", response_model=ReadinessResponse)
def readiness(response: Response, db: Session = Depends(get_db)) -> ReadinessResponse:
    """Readiness — database reachable and the worker queue table queryable.

    Deliberately does *not* check the LLM or embedding service: 09_DEPLOYMENT.md
    requires those to be surfaced on an admin status page rather than block
    readiness, because the app must stay up when they degrade
    (02_ARCHITECTURE.md §7.6).
    """
    database_ok = False
    queue_ok = False
    try:
        db.execute(text("SELECT 1"))
        database_ok = True
    except Exception:
        logger.exception("Readiness check: database unreachable")

    if database_ok:
        try:
            # The queue is a table (ADR-013), so "queue reachable" means the
            # worker's claim query can run.
            from app.models.evidence import EvidenceDocument

            db.execute(select(EvidenceDocument.id).limit(1))
            queue_ok = True
        except Exception:
            logger.exception("Readiness check: worker queue table unreachable")

    ready = database_ok and queue_ok
    if not ready:
        response.status_code = 503
    return ReadinessResponse(
        status="ready" if ready else "degraded",
        database=database_ok,
        worker_queue=queue_ok,
    )
