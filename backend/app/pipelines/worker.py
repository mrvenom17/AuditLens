"""Background pipeline worker (TASK-017, TASK-018, TASK-019).

02_ARCHITECTURE.md §7.1 puts this in a separate process on the same host, and
§7.5 describes its shape: claim documents in `processing`, extract, chunk, embed
(for discovery only), extract structured facts with provenance, then run the
deterministic rule engine and the Evidence Gate — writing Findings *through the
service layer* so the business rules are enforced in one place.

The LLM is not on this path. It is called once, at the very end, to draft a
plain-language explanation of a result that has already been determined.

ADR-013: the `evidence_documents` table is the queue. There is no broker.

The `matching_status` column keeps its name as the second-stage tracker even
though the stage now evaluates rather than matches — renaming a status column
across the schema, repositories and tests would be churn with no behavioural
payoff. ponytail: name retained deliberately; rename if the column ever gains a
second reader.

Failure handling is the point of this module, not an afterthought. Every stage
sets an explicit status:

* extraction failure  → `extraction_failed`, with a message safe to show an
  auditor. No Finding is created (01_REQUIREMENTS.md: "no partial/garbled data
  proceeds to matching").
* embedding failure   → evidence *discovery* is degraded. Evaluation is
  unaffected: the rule engine needs no vectors (TASK-110).
* LLM failure         → the Finding still exists with its facts, rule and
  result; only the plain-language explanation is missing.
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
from app.pipelines import discovery, extraction
from app.repositories.evidence import EvidenceChunkRepository, EvidenceDocumentRepository
from app.services import file_storage
from app.services.evaluation import EvaluationService
from app.services.finding import FindingService
from app.services.genai_service import draft_explanation

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

    vectors = discovery.embed_chunks([text for _, text in pieces])
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


def process_evaluation(db: DBSession, document: EvidenceDocument) -> None:
    """Extract facts from one document, then re-evaluate the audit's controls.

    This replaces the old LLM-judgment step entirely (TASK-110). The sequence is
    now: pull structured facts out of the extracted text with provenance, run the
    deterministic rule engine and the Evidence Gate over them, and wrap each
    result in a Finding awaiting human review. No model is consulted about
    whether anything is compliant, here or anywhere downstream.

    Embeddings are computed too, but only for evidence *discovery* — an
    embedding outage no longer affects evaluation at all, because facts and rules
    need no vectors.
    """
    _refresh_embeddings(db, document)

    evaluations = EvaluationService(db)
    fact_count = evaluations.extract_facts_for_document(document)

    findings = FindingService(db)
    created = 0
    for summary in evaluations.evaluate_audit(document.audit_id):
        # Only the freshest evaluation per control becomes a Finding; earlier
        # ones stay on the record as history (03_DATA_MODEL.md: evaluations are
        # append-only, never edited in place).
        finding = findings.create_for_evaluation(
            document.audit_id,
            summary.evaluation,
            scoped_control_id=summary.scoped_control_id,
            ai_explanation=None,
        )
        # Explanation drafting runs last and its failure is inert: the Finding
        # already exists with its facts, rule and result. Prose is a courtesy.
        finding.ai_explanation = draft_explanation(summary.evaluation, summary.control)
        created += 1

    document.matching_status = "complete"
    document.matching_error = None
    db.flush()
    logger.info(
        "evaluation.complete document=%s facts=%d findings=%d",
        document.id,
        fact_count,
        created,
    )


def _refresh_embeddings(db: DBSession, document: EvidenceDocument) -> None:
    """Ensure this document's chunks are embedded, for discovery only.

    A failure here is logged and tolerated. It used to block finding generation;
    now it blocks nothing that determines a result.
    """
    chunks = EvidenceChunkRepository(db).list_for_document(document.id)
    if not chunks or any(c.embedding is not None for c in chunks):
        return
    vectors = discovery.embed_chunks([c.content for c in chunks])
    if vectors is None:
        logger.warning("discovery.deferred document=%s reason=embedding_unavailable", document.id)
        return
    EvidenceChunkRepository(db).replace_for_document(
        document.id,
        [
            (index, chunk.content, chunk.location, vector)
            for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
        ],
    )


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
                process_evaluation(db, document)
            except Exception:
                logger.exception("Unhandled error evaluating document %s", document.id)
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
