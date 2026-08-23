"""Background pipeline worker (TASK-017, TASK-018, TASK-019).

02_ARCHITECTURE.md §7.1 puts this in a separate process on the same host, and
§7.5 describes its shape: claim documents in `processing`, extract, embed,
retrieve, call the LLM, and write Findings *through the service layer* so the
business rules are enforced in one place rather than duplicated here.

ADR-013: the `evidence_documents` table is the queue. There is no broker.

Failure handling is the point of this module, not an afterthought. Every stage
sets an explicit status:

* extraction failure  → `extraction_failed`, with a message safe to show an
  auditor. No Finding is created (01_REQUIREMENTS.md: "no partial/garbled data
  proceeds to matching").
* embedding failure   → `matching_status = deferred`. The document stays stored
  and extracted, and matching is retried on a later pass
  (02_ARCHITECTURE.md §7.6).
* LLM failure         → a Finding is still created, with nulls and
  `needs_manual_review = true`, never dropped.
* stuck in processing → swept to failed after a timeout, so no row waits forever.

Run with:  python -m app.pipelines.worker
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import UTC, datetime, timedelta
from types import FrameType

from sqlalchemy.orm import Session as DBSession

from app.config.settings import settings
from app.db.session import session_scope
from app.logging_setup import configure_logging
from app.models.enums import ExtractionStatus
from app.models.evidence import EvidenceDocument
from app.pipelines import extraction, matching
from app.repositories.evidence import EvidenceChunkRepository, EvidenceDocumentRepository
from app.services import file_storage
from app.services.finding import FindingService

logger = logging.getLogger(__name__)

_shutdown = False


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    """Finish the document in flight, then stop.

    Killing mid-document would leave a row in `processing` for the sweep to
    fail, losing work that had already succeeded.
    """
    global _shutdown
    logger.info("Received signal %d; finishing current work then shutting down", signum)
    _shutdown = True


def process_extraction(db: DBSession, document: EvidenceDocument) -> None:
    """Extract text from one document and record the outcome."""
    try:
        content = file_storage.read_stored(document.storage_path)
    except (OSError, ValueError):
        logger.exception("Could not read stored file for document %s", document.id)
        _mark_extraction_failed(
            document, "The stored file could not be read. Please re-upload this document."
        )
        return

    result = extraction.extract(content, document.mime_type)

    if not result.success:
        _mark_extraction_failed(document, result.error or "This document could not be read.")
        logger.info("extraction.failed document=%s", document.id)
        return

    document.extracted_text = result.full_text
    document.extraction_status = ExtractionStatus.complete
    document.extraction_error = None
    document.extraction_completed_at = datetime.now(UTC)
    db.flush()
    logger.info("extraction.complete document=%s sections=%d", document.id, len(result.sections))

    _store_chunks(db, document, result)


def _mark_extraction_failed(document: EvidenceDocument, message: str) -> None:
    document.extraction_status = ExtractionStatus.extraction_failed
    document.extraction_error = message
    document.extraction_completed_at = datetime.now(UTC)
    # Matching is not attempted on a failed extraction — garbled text would
    # produce a confident-looking finding drawn from nothing.
    document.matching_status = "skipped"


def _store_chunks(
    db: DBSession, document: EvidenceDocument, result: extraction.ExtractionResult
) -> None:
    """Chunk, embed, and store. Embedding failure defers rather than fails."""
    pieces = extraction.chunk_sections(result.sections)
    if not pieces:
        document.matching_status = "skipped"
        db.flush()
        return

    vectors = matching.embed_chunks([text for _, text in pieces])
    if vectors is None:
        # 02_ARCHITECTURE.md §7.6: extraction still completes and is stored;
        # matching is deferred and retried on a schedule.
        document.matching_status = "deferred"
        document.matching_error = "Embedding service unavailable; matching will be retried."
        db.flush()
        logger.warning("matching.deferred document=%s reason=embedding_unavailable", document.id)
        return

    EvidenceChunkRepository(db).replace_for_document(
        document.id,
        [
            (index, text, location, vector)
            for index, ((location, text), vector) in enumerate(zip(pieces, vectors, strict=True))
        ],
    )
    document.matching_status = "pending"
    document.matching_error = None
    db.flush()


def process_matching(db: DBSession, document: EvidenceDocument) -> None:
    """Retrieve candidate clauses and generate draft Findings for one document."""
    chunks = EvidenceChunkRepository(db).list_for_document(document.id)

    if not chunks or all(c.embedding is None for c in chunks):
        # Chunks exist but were never embedded (an earlier deferral). Retry the
        # embedding step rather than concluding there is nothing to match.
        result_sections = [
            extraction.ExtractedSection(location=c.location, text=c.content) for c in chunks
        ]
        if result_sections:
            vectors = matching.embed_chunks([s.text for s in result_sections])
            if vectors is None:
                document.matching_status = "deferred"
                document.matching_attempts += 1
                db.flush()
                return
            EvidenceChunkRepository(db).replace_for_document(
                document.id,
                [
                    (index, section.text, section.location, vector)
                    for index, (section, vector) in enumerate(
                        zip(result_sections, vectors, strict=True)
                    )
                ],
            )
            chunks = EvidenceChunkRepository(db).list_for_document(document.id)
        else:
            document.matching_status = "skipped"
            db.flush()
            return

    matches = matching.retrieve_matches(db, engagement_id=document.engagement_id, chunks=chunks)

    if not matches:
        # Nothing in the engagement's confirmed scope resembles this document.
        # That is a real outcome, not an error — the auditor may have uploaded
        # something out of scope, or the scope may not be confirmed yet.
        document.matching_status = "no_match"
        db.flush()
        logger.info("matching.no_match document=%s", document.id)
        return

    findings = FindingService(db)
    created = 0
    for match in matches:
        draft = matching.generate_finding(match, document.id)
        findings.create_draft(document.engagement_id, draft)
        created += 1

    document.matching_status = "complete"
    document.matching_error = None
    db.flush()
    logger.info("matching.complete document=%s findings=%d", document.id, created)


def run_once() -> int:
    """One pass of the loop. Returns how many documents were handled.

    Each document gets its own transaction, so one poisonous file cannot roll
    back the work already done on the others in the batch.
    """
    handled = 0

    with session_scope() as db:
        stuck = EvidenceDocumentRepository(db).sweep_stuck_extractions(
            timedelta(minutes=settings.EXTRACTION_STUCK_TIMEOUT_MINUTES)
        )
        if stuck:
            logger.warning("Swept %d document(s) stuck in processing", len(stuck))

    while True:
        with session_scope() as db:
            claimed = EvidenceDocumentRepository(db).claim_for_extraction(limit=1)
            if not claimed:
                break
            document = claimed[0]
        # A second transaction so the claim is committed before the slow work
        # starts — otherwise a crash mid-extraction would release the claim and
        # the same document would be picked up again forever.
        with session_scope() as db:
            document = db.merge(document)
            try:
                process_extraction(db, document)
            except Exception:
                logger.exception("Unhandled error extracting document %s", document.id)
                _mark_extraction_failed(
                    document,
                    "An unexpected error occurred while reading this document. "
                    "Please review it manually.",
                )
        handled += 1
        if _shutdown:
            return handled

    while True:
        with session_scope() as db:
            claimed = EvidenceDocumentRepository(db).claim_for_matching(limit=1)
            if not claimed:
                break
            document = claimed[0]
            document.matching_status = "in_progress"

        with session_scope() as db:
            document = db.merge(document)
            try:
                process_matching(db, document)
            except Exception:
                logger.exception("Unhandled error matching document %s", document.id)
                document.matching_status = "deferred"
                document.matching_attempts += 1
                document.matching_error = "Matching failed and will be retried."
        handled += 1
        if _shutdown:
            return handled

    return handled


def main() -> int:
    configure_logging(settings.ENVIRONMENT)
    settings.validate_for_environment()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "AuditLens worker starting (poll interval %.1fs)", settings.WORKER_POLL_INTERVAL_SECONDS
    )
    while not _shutdown:
        try:
            handled = run_once()
        except Exception:
            # A database outage must not kill the worker — it should keep
            # trying, because the queue is durable and the work is still there.
            logger.exception("Worker pass failed; retrying after the poll interval")
            handled = 0
        if handled == 0:
            time.sleep(settings.WORKER_POLL_INTERVAL_SECONDS)

    logger.info("AuditLens worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
