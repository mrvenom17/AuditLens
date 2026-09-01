"""Evidence discovery — retrieval only, never judgment (02_ARCHITECTURE.md §7.5).

This module was `matching.py`, and it used to ask an LLM "does this evidence
satisfy this requirement?" and write the answer into a Finding. That path is
gone. It was the exact mechanism this architecture exists to remove: the model
that made it fast was the model that made it hallucinate.

What survives is retrieval, demoted to its honest role. pgvector answers *"where
might relevant evidence be"* — for an auditor browsing an audit's documents, and
as a navigation aid in the review UI. It never answers *"is this compliant"*.

The renaming is deliberate. A module called `matching` that returns candidate
requirements invites someone to re-add a judgment step "while they're in here";
a module called `discovery` that returns candidate locations does not.

**No function in this file may write to, or return anything that is written to,
`ControlEvaluation.result`.** Compliance results come from `rule_engine.py`
alone, evaluated over provenanced facts.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.models.corpus import ControlDefinition
from app.models.evidence import EvidenceChunk
from app.models.scoping import ScopedControl
from app.pipelines.embedding import EmbeddingUnavailableError, get_embedding_client

logger = logging.getLogger(__name__)


@dataclass
class RetrievedMatch:
    """A control whose text resembles some of this document's chunks.

    A *navigational* result. The similarity score says these texts are related;
    it says nothing about compliance, and no caller may treat it as though it
    did (05_SECURITY.md §10.1, RAG-poisoning row: a poisoning attempt can at
    most waste an auditor's time, because retrieval cannot produce a result).
    """

    scoped_control: ScopedControl
    control: ControlDefinition
    chunks: list[EvidenceChunk]
    similarity: float


def retrieve_matches(
    db: DBSession,
    *,
    audit_id: uuid.UUID,
    chunks: list[EvidenceChunk],
    top_k: int = 3,
    min_similarity: float = 0.25,
) -> list[RetrievedMatch]:
    """Find which confirmed controls this document's chunks relate to.

    The candidate set is this audit's confirmed ScopedControls and nothing else
    — retrieval never reaches across audits (05_SECURITY.md §10.5, RAG
    isolation), and never into the full corpus.
    """
    embedded = [c for c in chunks if c.embedding is not None]
    if not embedded:
        return []

    candidates = db.execute(
        select(ScopedControl, ControlDefinition)
        .join(ControlDefinition, ScopedControl.control_definition_id == ControlDefinition.id)
        .where(
            ScopedControl.audit_id == audit_id,
            ScopedControl.confirmed.is_(True),
            ControlDefinition.embedding.is_not(None),
        )
    ).all()
    if not candidates:
        return []

    matches: list[RetrievedMatch] = []
    for scoped, control in candidates:
        scored = sorted(
            (
                (_cosine_similarity(chunk.embedding, control.embedding), chunk)
                for chunk in embedded
                if chunk.embedding is not None and control.embedding is not None
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )
        best = [(score, chunk) for score, chunk in scored[:top_k] if score >= min_similarity]
        if best:
            matches.append(
                RetrievedMatch(
                    scoped_control=scoped,
                    control=control,
                    chunks=[chunk for _, chunk in best],
                    similarity=best[0][0],
                )
            )

    matches.sort(key=lambda m: m.similarity, reverse=True)
    return matches


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Both operands come from the same normalised model, so the dot product is
    the cosine. Computed here rather than in SQL because the candidate set is
    already narrowed to one audit's confirmed scope — a few dozen vectors,
    not the whole corpus."""
    return sum(x * y for x, y in zip(a, b, strict=True))


def embed_chunks(texts: list[str]) -> list[list[float]] | None:
    """Embed chunk texts, or None if the embedding service is unavailable.

    None is a deferral, not a failure: 02_ARCHITECTURE.md §7.6 requires that
    extraction still completes and is stored when embedding is down. Note that
    embeddings being unavailable no longer blocks *evaluation* — facts and rules
    need no vectors — so a discovery outage now degrades navigation only.
    """
    try:
        return get_embedding_client().embed(texts)
    except EmbeddingUnavailableError:
        logger.warning("Embedding unavailable; evidence discovery deferred")
        return None
