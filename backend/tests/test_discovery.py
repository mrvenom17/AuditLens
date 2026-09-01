"""Evidence discovery tests.

Retrieval is scoped to the audit's own confirmed controls and nothing else
(05_SECURITY.md §10.5, RAG isolation).

**What this file no longer tests, deliberately.** The prior revision covered
confidence thresholds, LLM-failure draft generation, and a worker pass that
turned model output into Findings. That whole path was removed by TASK-110 —
retrieval no longer produces a compliance judgment of any kind, so there is no
confidence to threshold and no model answer to degrade from. A similarity score
is now navigational only. The behaviour that replaced it is covered by
`test_rule_engine.py`, `test_evidence_gate.py` and `tests/adversarial/`.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
from sqlalchemy.orm import Session as DBSession

from app.config.settings import settings
from app.models.enums import AuditStatus, Role
from app.models.evidence import EvidenceChunk, EvidenceDocument
from app.pipelines import discovery
from app.pipelines.embedding import EmbeddingUnavailableError, set_embedding_client
from app.pipelines.llm import set_llm_client

DIMENSIONS = settings.EMBEDDING_DIMENSIONS


def unit_vector(axis: int) -> list[float]:
    """A one-hot vector. Two of these are orthogonal (cosine 0) unless they
    share an axis (cosine 1), which makes similarity in these tests exact
    rather than approximate."""
    vector = [0.0] * DIMENSIONS
    vector[axis % DIMENSIONS] = 1.0
    return vector


class FakeEmbedding:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        if self._raises is not None:
            raise self._raises
        return [unit_vector(0) for _ in texts]


@pytest.fixture(autouse=True)
def _reset_clients() -> Any:
    yield
    set_llm_client(None)
    set_embedding_client(None)


@pytest.fixture
def audit_with_evidence(db: DBSession, make_user: Any, make_audit: Any) -> dict[str, Any]:
    auditor = make_user(Role.auditor, password="correct-horse-battery-staple")
    audit = make_audit(auditor, status=AuditStatus.in_progress)
    document = EvidenceDocument(
        audit_id=audit.id,
        original_filename="firewall.pdf",
        content_hash="c" * 64,
        storage_path="unused-in-this-test-no-file-is-read",
        mime_type="application/pdf",
        size_bytes=1024,
        uploaded_by=auditor.id,
        extraction_status="complete",
        extracted_text="Firewall ruleset denies all inbound traffic by default.",
        matching_status="pending",
    )
    db.add(document)
    db.flush()
    return {"auditor": auditor, "audit": audit, "document": document}


def add_chunk(
    db: DBSession, document: EvidenceDocument, *, index: int, location: str, axis: int
) -> EvidenceChunk:
    chunk = EvidenceChunk(
        evidence_document_id=document.id,
        chunk_index=index,
        content=f"Evidence content for {location}.",
        location=location,
        embedding=unit_vector(axis),
    )
    db.add(chunk)
    db.flush()
    return chunk


class TestRetrievalScoping:
    """TASK-018: "Retrieval scoped only to the audit's confirmed
    ScopedControl set, never the full corpus.\""""

    def test_only_confirmed_requirements_are_candidates(
        self,
        db: DBSession,
        make_scoped_requirement: Any,
        make_requirement: Any,
        audit_with_evidence: dict[str, Any],
    ) -> None:
        setup = audit_with_evidence
        confirmed_req = make_requirement(control_id="1.2.1")
        confirmed_req.embedding = unit_vector(0)
        unconfirmed_req = make_requirement(control_id="1.2.2")
        unconfirmed_req.embedding = unit_vector(0)  # identical, so only scope differs
        db.flush()

        make_scoped_requirement(setup["audit"], confirmed=True, requirement=confirmed_req)
        make_scoped_requirement(setup["audit"], confirmed=False, requirement=unconfirmed_req)
        chunk = add_chunk(db, setup["document"], index=0, location="page 1", axis=0)

        matches = discovery.retrieve_matches(db, audit_id=setup["audit"].id, chunks=[chunk])

        assert [m.control.control_id for m in matches] == ["1.2.1"]

    def test_another_audits_scope_is_never_a_candidate(
        self,
        db: DBSession,
        make_user: Any,
        make_audit: Any,
        make_scoped_requirement: Any,
        make_requirement: Any,
        audit_with_evidence: dict[str, Any],
    ) -> None:
        """The retrieval query is audit-bounded, so one client's evidence
        can never be matched against another client's scope."""
        setup = audit_with_evidence
        other_auditor = make_user(Role.auditor)
        other_audit = make_audit(other_auditor)
        other_req = make_requirement(control_id="9.9.1", family=9)
        other_req.embedding = unit_vector(0)
        db.flush()
        make_scoped_requirement(other_audit, confirmed=True, requirement=other_req)

        chunk = add_chunk(db, setup["document"], index=0, location="page 1", axis=0)
        matches = discovery.retrieve_matches(db, audit_id=setup["audit"].id, chunks=[chunk])

        assert matches == []

    def test_dissimilar_requirements_fall_below_the_threshold(
        self,
        db: DBSession,
        make_scoped_requirement: Any,
        make_requirement: Any,
        audit_with_evidence: dict[str, Any],
    ) -> None:
        """Matching everything in scope to everything uploaded would bury the
        auditor in findings that cite irrelevant evidence."""
        setup = audit_with_evidence
        requirement = make_requirement(control_id="1.2.1")
        requirement.embedding = unit_vector(5)  # orthogonal to the chunk
        db.flush()
        make_scoped_requirement(setup["audit"], confirmed=True, requirement=requirement)
        chunk = add_chunk(db, setup["document"], index=0, location="page 1", axis=0)

        matches = discovery.retrieve_matches(db, audit_id=setup["audit"].id, chunks=[chunk])

        assert matches == []

    def test_one_document_can_match_several_clauses(
        self,
        db: DBSession,
        make_scoped_requirement: Any,
        make_requirement: Any,
        audit_with_evidence: dict[str, Any],
    ) -> None:
        """01_REQUIREMENTS.md Edge Cases and TASK-019's required
        multi-clause-from-one-document case: "one firewall config screenshot may
        cover several network-security requirements"."""
        setup = audit_with_evidence
        for control_id in ("1.2.1", "1.3.1", "1.4.1"):
            requirement = make_requirement(control_id=control_id)
            requirement.embedding = unit_vector(0)
            db.flush()
            make_scoped_requirement(setup["audit"], confirmed=True, requirement=requirement)
        chunk = add_chunk(db, setup["document"], index=0, location="page 1", axis=0)

        matches = discovery.retrieve_matches(db, audit_id=setup["audit"].id, chunks=[chunk])

        assert sorted(m.control.control_id for m in matches) == ["1.2.1", "1.3.1", "1.4.1"]

    def test_unembedded_chunks_yield_no_matches(
        self,
        db: DBSession,
        make_scoped_requirement: Any,
        make_requirement: Any,
        audit_with_evidence: dict[str, Any],
    ) -> None:
        setup = audit_with_evidence
        requirement = make_requirement(control_id="1.2.1")
        requirement.embedding = unit_vector(0)
        db.flush()
        make_scoped_requirement(setup["audit"], confirmed=True, requirement=requirement)

        chunk = EvidenceChunk(
            evidence_document_id=setup["document"].id,
            chunk_index=0,
            content="Not yet embedded.",
            location="page 1",
            embedding=None,
        )
        db.add(chunk)
        db.flush()

        assert discovery.retrieve_matches(db, audit_id=setup["audit"].id, chunks=[chunk]) == []


class TestDiscoveryIsNotJudgment:
    """The property that replaced the removed judgment path.

    These assertions are about module structure rather than behaviour, because
    the guarantee itself is structural: discovery cannot produce a compliance
    result, since there is no longer any function that returns one.
    """

    def test_discovery_exposes_no_finding_generation(self) -> None:
        """`generate_finding` and `DraftFinding` are gone. If either comes back,
        an LLM is judging compliance again and this test is the alarm."""
        assert not hasattr(discovery, "generate_finding")
        assert not hasattr(discovery, "DraftFinding")

    def test_discovery_never_imports_the_llm_client(self) -> None:
        """Retrieval needs embeddings, never a language model."""
        source = pathlib.Path(discovery.__file__).read_text() if discovery.__file__ else ""
        assert "pipelines.llm" not in source
        assert "get_llm_client" not in source

    def test_a_similarity_score_is_not_a_result(
        self,
        db: DBSession,
        make_scoped_requirement: Any,
        make_requirement: Any,
        audit_with_evidence: dict[str, Any],
    ) -> None:
        """A perfect similarity match still carries no verdict — the match
        object exposes a score and chunks, and nothing resembling PASS/FAIL."""
        setup = audit_with_evidence
        requirement = make_requirement(control_id="1.2.1")
        requirement.embedding = unit_vector(0)
        db.flush()
        make_scoped_requirement(setup["audit"], confirmed=True, requirement=requirement)
        chunk = add_chunk(db, setup["document"], index=0, location="page 1", axis=0)

        match = discovery.retrieve_matches(db, audit_id=setup["audit"].id, chunks=[chunk])[0]

        assert match.similarity == pytest.approx(1.0)
        for attribute in ("result", "status", "suggested_status", "confidence"):
            assert not hasattr(match, attribute)


class TestEmbeddingDegradation:
    def test_embedding_failure_defers_rather_than_raising(self) -> None:
        """A discovery outage degrades navigation only. It no longer blocks
        evaluation, because the rule engine needs no vectors."""
        set_embedding_client(FakeEmbedding(raises=EmbeddingUnavailableError("down")))
        assert discovery.embed_chunks(["anything"]) is None
