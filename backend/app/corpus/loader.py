"""PCI DSS corpus ingestion (TASK-006).

Loads clause rows from `pci_dss_v4_0_1.json`. Per 03_DATA_MODEL.md, a corpus
update *inserts* rows tagged with a new `corpus_version` rather than mutating
existing ones, so an engagement that ran last year still cites the text that was
in effect then. That rule is what makes this an idempotent insert-if-absent
rather than an upsert.

Under ADR-010 the shipped file carries firm-authored summaries, not the PCI
Security Standards Council's copyrighted text. Replacing it with a licensed
export is a file swap plus a re-run under a new version string — no code change.

Run with:  python -m app.corpus.loader [--embed]
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import session_scope
from app.models.corpus import PCIRequirement

logger = logging.getLogger(__name__)

CORPUS_FILE = Path(__file__).parent / "pci_dss_v4_0_1.json"


class CorpusRow(TypedDict):
    clause_id: str
    requirement_family: int
    title: str
    full_text: str


def load_corpus_file(path: Path = CORPUS_FILE) -> dict[str, Any]:
    """Read and structurally validate the corpus file.

    Validation is not ceremony: a malformed corpus would silently produce an
    engagement scoped against clauses that do not exist, and the failure would
    only surface as bad findings much later.
    """
    with path.open(encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)

    for key in ("corpus_version", "requirements"):
        if key not in data:
            raise ValueError(f"Corpus file is missing required key: {key}")

    seen: set[str] = set()
    for row in data["requirements"]:
        clause_id = row["clause_id"]
        if clause_id in seen:
            raise ValueError(f"Duplicate clause_id in corpus file: {clause_id}")
        seen.add(clause_id)

        family = row["requirement_family"]
        if not 1 <= family <= 12:
            raise ValueError(f"Clause {clause_id} has requirement_family {family}, expected 1-12")
        if clause_id.split(".")[0] != str(family):
            raise ValueError(f"Clause {clause_id} does not belong to requirement family {family}")
        if not row["title"].strip() or not row["full_text"].strip():
            raise ValueError(f"Clause {clause_id} has empty title or full_text")

    return data


def ingest(db: Session, path: Path = CORPUS_FILE) -> tuple[int, int]:
    """Insert any clause rows not already present for this corpus version.

    Returns (inserted, skipped). Safe to re-run: existing rows are left exactly
    as they are, because mutating them would rewrite history for every past
    engagement that cites them.
    """
    data = load_corpus_file(path)
    version: str = data["corpus_version"]
    effective = date.fromisoformat(data["effective_date"]) if data.get("effective_date") else None

    existing = set(
        db.scalars(
            select(PCIRequirement.clause_id).where(PCIRequirement.corpus_version == version)
        ).all()
    )

    inserted = 0
    for row in data["requirements"]:
        if row["clause_id"] in existing:
            continue
        db.add(
            PCIRequirement(
                clause_id=row["clause_id"],
                requirement_family=row["requirement_family"],
                title=row["title"],
                full_text=row["full_text"],
                corpus_version=version,
                effective_date=effective,
            )
        )
        inserted += 1

    db.flush()
    skipped = len(data["requirements"]) - inserted
    logger.info("Corpus ingest version=%s inserted=%d skipped=%d", version, inserted, skipped)
    return inserted, skipped


def embed_corpus(db: Session, version: str | None = None) -> int:
    """Compute embeddings for corpus rows that lack one.

    Separated from `ingest` because embedding needs the model loaded, which is a
    worker-side concern (ADR-009) — the corpus can be ingested on a machine that
    has no model at all.
    """
    from app.pipelines.embedding import get_embedding_client

    stmt = select(PCIRequirement).where(PCIRequirement.embedding.is_(None))
    if version:
        stmt = stmt.where(PCIRequirement.corpus_version == version)
    rows = list(db.scalars(stmt).all())
    if not rows:
        return 0

    client = get_embedding_client()
    # The title carries the clause's subject and the body its detail; embedding
    # them together retrieves better than either alone.
    texts = [f"{row.clause_id} {row.title}. {row.full_text}" for row in rows]
    vectors = client.embed(texts)
    for row, vector in zip(rows, vectors, strict=True):
        row.embedding = vector
    db.flush()
    logger.info("Embedded %d corpus rows", len(rows))
    return len(rows)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Ingest the PCI DSS corpus.")
    parser.add_argument(
        "--embed",
        action="store_true",
        help="also compute embeddings (requires the embedding model to be available)",
    )
    args = parser.parse_args()

    with session_scope() as db:
        inserted, skipped = ingest(db)
        print(f"Corpus ingest complete: {inserted} inserted, {skipped} already present.")
        if args.embed:
            embedded = embed_corpus(db)
            print(f"Embedded {embedded} rows.")


if __name__ == "__main__":
    main()
