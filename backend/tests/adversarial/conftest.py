"""Shared machinery for the adversarial suite.

Two decisions here matter more than the rest:

* **The LLM is wired to explode by default.** Every test in this package runs
  with any LLM or embedding call raising `ConnectionError`. That inverts the
  usual arrangement: instead of proving the deterministic path works when the
  model is absent as a special case, absence is the baseline and a test must opt
  in to having a model at all. If any of these tests ever starts depending on
  one, it fails loudly rather than quietly passing for the wrong reason.

* **The pipeline is run for real.** Documents are written to storage as bytes,
  extracted by the real extractor, scanned by the real fact service, and
  evaluated by the real engine and gate. Nothing between upload and result is
  stubbed, because a mocked pipeline cannot demonstrate that a prompt-injection
  payload has no path to the result — it can only demonstrate that the mock had
  none.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.orm import Session as DBSession

from app.models.audit import Audit
from app.models.corpus import ControlDefinition
from app.models.enums import (
    AuditStatus,
    EvaluationMode,
    ExtractionStatus,
    Role,
    ScopeSource,
)
from app.models.evidence import EvidenceDocument
from app.models.scoping import ScopedControl
from app.pipelines.embedding import set_embedding_client
from app.pipelines.llm import set_llm_client
from app.services import file_storage
from app.services.evaluation import EvaluationService
from tests import testcompany as tc


class ExplodingClient:
    """Any use of a model in this package is a bug, so make it impossible to
    miss rather than merely absent."""

    def complete(self, **kwargs: Any) -> None:
        raise ConnectionError(
            "An LLM call was made on a path that must be deterministic. "
            "This is the failure the whole architecture exists to prevent."
        )

    def embed(self, texts: list[str]) -> None:
        raise ConnectionError("An embedding call was made on a deterministic path.")


@pytest.fixture(autouse=True)
def _no_model_available() -> Iterator[None]:
    """00_PRODUCT.md §5.6, LLM-unavailable row — applied to every test here."""
    set_llm_client(ExplodingClient())
    set_embedding_client(ExplodingClient())
    yield
    set_llm_client(None)
    set_embedding_client(None)


@pytest.fixture
def frozen_controls(db: DBSession) -> dict[str, ControlDefinition]:
    """The eight Level 0 controls, loaded from the real corpus file.

    Loaded rather than hand-built on purpose: these tests must exercise the rules
    that actually ship, so an error in the authored corpus fails here instead of
    in production.
    """
    from app.corpus.loader import load_corpus_file

    data = load_corpus_file()
    frozen = set(data["level_0_deterministic_controls"])
    controls: dict[str, ControlDefinition] = {}

    for row in data["requirements"]:
        if row["control_id"] not in frozen:
            continue
        control = ControlDefinition(
            control_id=row["control_id"],
            requirement_family=row["requirement_family"],
            name=row["name"],
            requirement_text=row["requirement_text"],
            evaluation_mode=EvaluationMode(row["evaluation_mode"]),
            evidence_requirements=row["evidence_requirements"],
            facts=row["facts"],
            rules=row["rules"],
            # Mirrored from the shipped file, not defaulted — the point of
            # loading the real corpus is that an authoring error fails here
            # rather than in production, and a fixture that drops half the
            # authored fields cannot do that.
            applicability_conditions=row.get("applicability_conditions") or [],
            assessment_procedures=row.get("assessment_procedures") or [],
            freshness_window_days=row["freshness_window_days"],
            corpus_version=data["corpus_version"],
        )
        db.add(control)
        controls[row["control_id"]] = control

    db.flush()
    assert len(controls) == 8, "the frozen Level 0 control set should be exactly eight controls"
    return controls


@pytest.fixture
def test_audit(db: DBSession, make_user: Any, make_audit: Any, frozen_controls: dict) -> Audit:
    """An audit with every frozen control in confirmed scope.

    Flagged `test_company` so this fabricated data stays structurally
    distinguishable from real client work everywhere it appears
    (01_REQUIREMENTS.md § Audit Creation).
    """
    auditor = make_user(Role.auditor)
    audit = make_audit(auditor, status=AuditStatus.in_progress, client_name="ACME Payments (TEST)")
    audit.test_company = True
    for control in frozen_controls.values():
        db.add(
            ScopedControl(
                audit_id=audit.id,
                control_definition_id=control.id,
                source=ScopeSource.manual,
                confirmed=True,
            )
        )
    db.flush()
    return audit


@pytest.fixture
def upload(db: DBSession, test_audit: Audit) -> Any:
    """Store a fixture document and register it as extracted evidence.

    Writes real bytes through `file_storage.store`, so the content hash is a
    genuine SHA-256 of the file the gate will later re-read — which is what makes
    the tampering test meaningful rather than simulated.
    """

    def _upload(document: tc.TestDocument, *, audit: Audit | None = None) -> EvidenceDocument:
        target = audit or test_audit
        content = document.content()
        content_hash, storage_path = file_storage.store(content, subdirectory="evidence")
        row = EvidenceDocument(
            audit_id=target.id,
            original_filename=document.filename,
            content_hash=content_hash,
            storage_path=storage_path,
            mime_type="application/pdf",
            size_bytes=len(content),
            uploaded_by=target.created_by,
            extraction_status=ExtractionStatus.complete,
            matching_status="pending",
        )
        db.add(row)
        db.flush()
        return row

    return _upload


@pytest.fixture
def run_pipeline(db: DBSession, test_audit: Audit) -> Any:
    """Extract facts from the given documents, then evaluate every scoped control.

    This is the real `EvaluationService`, not a test double — the same code the
    worker runs.
    """

    def _run(documents: list[EvidenceDocument], *, audit: Audit | None = None) -> dict[str, Any]:
        target = audit or test_audit
        service = EvaluationService(db)
        for document in documents:
            service.extract_facts_for_document(document)
        summaries = service.evaluate_audit(target.id)
        return {s.control.control_id: s.evaluation for s in summaries}

    return _run
