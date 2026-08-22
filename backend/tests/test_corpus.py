"""Corpus ingestion tests (TASK-006).

TASK-006 asks for a row count matching the expected clause count and a
spot-check of clause IDs against the published standard. The count assertion is
written against the corpus file's own contents plus structural invariants rather
than a hardcoded number — see `test_corpus_granularity_is_documented` for why
the "~78 base requirements" figure in 07_TASKS.md and the row count here differ.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.corpus.loader import CORPUS_FILE, ingest, load_corpus_file
from app.models.corpus import PCIRequirement


class TestCorpusFile:
    def test_file_is_structurally_valid(self) -> None:
        data = load_corpus_file()
        assert data["corpus_version"] == "v4.0.1-summary"
        assert len(data["requirements"]) > 0

    def test_all_twelve_requirement_families_are_covered(self) -> None:
        data = load_corpus_file()
        families = {row["requirement_family"] for row in data["requirements"]}
        assert families == set(range(1, 13))

    def test_corpus_version_marks_the_text_as_summary_not_the_standard(self) -> None:
        """ADR-010: nothing may cite the skeleton as if it were the Council's
        text. The version string is the mechanism that keeps that visible in
        every Finding and every Report snapshot."""
        data = load_corpus_file()
        assert "summary" in data["corpus_version"]
        assert "copyright" in data["source_note"].lower() or "NOT" in data["source_note"]

    def test_corpus_granularity_is_documented(self) -> None:
        """07_TASKS.md estimates "~78 base requirements". PCI DSS numbers
        clauses at two levels: base requirements (x.y) and the defined
        requirements beneath them (x.y.z). Matching evidence needs the finer
        level, so rows are stored at x.y.z. This test pins both counts so a
        future corpus swap that changes granularity fails loudly rather than
        silently changing what "scope" means."""
        data = load_corpus_file()
        defined = len(data["requirements"])
        base = len({".".join(r["clause_id"].split(".")[:2]) for r in data["requirements"]})
        assert defined > base
        assert 50 <= base <= 90, f"base-requirement count {base} is outside the expected range"

    @pytest.mark.parametrize(
        ("clause_id", "family", "title_fragment"),
        [
            ("1.2.1", 1, "Configuration standards"),
            ("3.3.1", 3, "Sensitive authentication data is not retained"),
            ("6.3.3", 6, "security patches"),
            ("8.3.6", 8, "minimum level of complexity"),
            ("10.5.1", 10, "12 months"),
            ("12.10.1", 12, "incident response plan"),
        ],
    )
    def test_spot_check_known_clauses(
        self, clause_id: str, family: int, title_fragment: str
    ) -> None:
        """TASK-006: spot-check a handful of clause IDs against the published
        standard's structure."""
        data = load_corpus_file()
        row = next(r for r in data["requirements"] if r["clause_id"] == clause_id)
        assert row["requirement_family"] == family
        assert title_fragment.lower() in row["title"].lower()

    def test_rejects_a_clause_whose_id_contradicts_its_family(self, tmp_path: Any) -> None:
        """A clause filed under the wrong family would be retrieved for the
        wrong engagements, so the loader refuses it rather than warning."""
        bad = {
            "corpus_version": "v4.0.1-test",
            "requirements": [
                {
                    "clause_id": "1.2.1",
                    "requirement_family": 3,
                    "title": "Mismatched",
                    "full_text": "Body.",
                }
            ],
        }
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError, match="does not belong to requirement family"):
            load_corpus_file(path)

    def test_rejects_duplicate_clause_ids(self, tmp_path: Any) -> None:
        bad = {
            "corpus_version": "v4.0.1-test",
            "requirements": [
                {"clause_id": "1.2.1", "requirement_family": 1, "title": "A", "full_text": "B"},
                {"clause_id": "1.2.1", "requirement_family": 1, "title": "C", "full_text": "D"},
            ],
        }
        path = tmp_path / "dup.json"
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError, match="Duplicate clause_id"):
            load_corpus_file(path)


class TestCorpusIngest:
    def test_ingest_loads_every_clause(self, db: Session) -> None:
        expected = len(load_corpus_file()["requirements"])
        inserted, skipped = ingest(db)

        assert inserted == expected
        assert skipped == 0
        stored = db.scalar(select(func.count()).select_from(PCIRequirement))
        assert stored == expected

    def test_ingest_is_idempotent(self, db: Session) -> None:
        """A corpus load must be safe to re-run — a deployment that applies it
        twice must not double the corpus."""
        first_inserted, _ = ingest(db)
        second_inserted, second_skipped = ingest(db)

        assert second_inserted == 0
        assert second_skipped == first_inserted

    def test_corpus_is_queryable_by_clause_id_and_family(self, db: Session) -> None:
        """TASK-006 acceptance criterion."""
        ingest(db)

        by_clause = db.scalar(select(PCIRequirement).where(PCIRequirement.clause_id == "1.2.1"))
        assert by_clause is not None
        assert by_clause.requirement_family == 1

        family_three = db.scalars(
            select(PCIRequirement).where(PCIRequirement.requirement_family == 3)
        ).all()
        assert len(family_three) > 0
        assert all(r.clause_id.startswith("3.") for r in family_three)

    def test_reingesting_under_a_new_version_does_not_touch_existing_rows(
        self, db: Session, tmp_path: Any
    ) -> None:
        """03_DATA_MODEL.md → PCIRequirement lifecycle. This is the property
        that lets a finalized report keep meaning what it meant."""
        ingest(db)
        original = db.scalar(
            select(PCIRequirement).where(
                PCIRequirement.clause_id == "1.2.1",
                PCIRequirement.corpus_version == "v4.0.1-summary",
            )
        )
        assert original is not None
        original_text = original.full_text

        updated = json.loads(CORPUS_FILE.read_text())
        updated["corpus_version"] = "v4.0.2-summary"
        updated["requirements"] = [updated["requirements"][0]]
        updated["requirements"][0]["full_text"] = "Revised wording for the new version."
        path = tmp_path / "next.json"
        path.write_text(json.dumps(updated))

        ingest(db, path)

        db.refresh(original)
        assert original.full_text == original_text

        new_row = db.scalar(
            select(PCIRequirement).where(PCIRequirement.corpus_version == "v4.0.2-summary")
        )
        assert new_row is not None
        assert new_row.full_text == "Revised wording for the new version."
