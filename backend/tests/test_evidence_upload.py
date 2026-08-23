"""Evidence upload and extraction tests (TASK-016, TASK-017).

TASK-016 requires: reject a disguised executable, reject an oversized file,
accept a valid PDF.
TASK-017 requires: corrupt file → `extraction_failed`, not a crash;
password-protected PDF → specific rejection.
08_TESTING.md § Security Tests adds path-traversal filenames.

The controls under test are the ones 05_SECURITY.md §10.4/§10.5 specify, so the
files here are genuine rather than stubbed — a placeholder would not exercise
magic-byte inspection at all.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.models.enums import EngagementStatus, EvidenceRequestStatus, ExtractionStatus, Role
from app.models.evidence import EvidenceDocument
from app.pipelines import extraction
from app.pipelines.worker import process_extraction
from app.services import file_storage
from tests import filefixtures as ff

PASSWORD = "correct-horse-battery-staple"


def login(client: TestClient, user: Any) -> None:
    assert (
        client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD}).status_code
        == 200
    )


@pytest.fixture
def uploader(make_user: Any, make_engagement: Any) -> dict[str, Any]:
    auditor = make_user(Role.auditor, password=PASSWORD)
    return {
        "auditor": auditor,
        "engagement": make_engagement(auditor, status=EngagementStatus.in_progress),
    }


def upload(
    client: TestClient,
    engagement_id: uuid.UUID,
    content: bytes,
    filename: str,
    content_type: str = "application/pdf",
    **data: Any,
) -> Any:
    return client.post(
        f"/api/engagements/{engagement_id}/evidence-documents",
        files={"file": (filename, content, content_type)},
        data=data,
    )


class TestAcceptedFileTypes:
    def test_valid_pdf_is_accepted_and_queued_for_extraction(
        self, api_client: TestClient, uploader: dict[str, Any]
    ) -> None:
        """TASK-016 acceptance: accept a valid PDF. 04_API_CONTRACT.md: 201 with
        `extraction_status: processing` — the pipeline runs in the worker, so
        the request returns before any parsing happens."""
        login(api_client, uploader["auditor"])

        response = upload(
            api_client, uploader["engagement"].id, ff.valid_pdf(), "firewall-config.pdf"
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["extraction_status"] == "processing"
        assert body["mime_type"] == "application/pdf"
        assert body["original_filename"] == "firewall-config.pdf"
        assert len(body["content_hash"]) == 64

    @pytest.mark.parametrize(
        ("builder", "filename", "declared_type", "expected_mime"),
        [
            (ff.valid_pdf, "policy.pdf", "application/pdf", "application/pdf"),
            (
                ff.valid_docx,
                "policy.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            (
                ff.valid_xlsx,
                "rules.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
            (ff.valid_png, "screenshot.png", "image/png", "image/png"),
            (ff.valid_jpeg, "photo.jpg", "image/jpeg", "image/jpeg"),
        ],
    )
    def test_every_documented_format_is_accepted(
        self,
        api_client: TestClient,
        uploader: dict[str, Any],
        builder: Any,
        filename: str,
        declared_type: str,
        expected_mime: str,
    ) -> None:
        """01_REQUIREMENTS.md § Inputs: PDF, DOCX, XLSX, PNG, JPG."""
        login(api_client, uploader["auditor"])

        response = upload(api_client, uploader["engagement"].id, builder(), filename, declared_type)

        assert response.status_code == 201, response.text
        assert response.json()["mime_type"] == expected_mime

    def test_type_is_taken_from_content_not_from_the_declared_header(
        self, api_client: TestClient, uploader: dict[str, Any]
    ) -> None:
        """A real PNG announced as a PDF is still stored as a PNG. The declared
        content-type is attacker-controlled and contributes nothing to the
        decision (05_SECURITY.md §10.4)."""
        login(api_client, uploader["auditor"])

        response = upload(
            api_client,
            uploader["engagement"].id,
            ff.valid_png(),
            "actually-a-png.pdf",
            "application/pdf",
        )

        assert response.status_code == 201
        assert response.json()["mime_type"] == "image/png"


class TestRejectedUploads:
    def test_disguised_executable_is_rejected(
        self, api_client: TestClient, db: DBSession, uploader: dict[str, Any]
    ) -> None:
        """01_REQUIREMENTS.md acceptance criterion: "Given a .exe file renamed
        to .pdf, when uploaded, content-type inspection rejects it with 400"."""
        login(api_client, uploader["auditor"])

        response = upload(
            api_client,
            uploader["engagement"].id,
            ff.disguised_executable(),
            "quarterly-report.pdf",
            "application/pdf",
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"
        assert db.scalar(select(func.count()).select_from(EvidenceDocument)) == 0

    def test_rejected_upload_writes_nothing_to_disk(
        self, api_client: TestClient, uploader: dict[str, Any], isolated_file_storage: Path
    ) -> None:
        """01_REQUIREMENTS.md Failure Cases: "Unsupported file type → 400, no
        file stored." Validation runs before anything touches the filesystem."""
        login(api_client, uploader["auditor"])

        upload(
            api_client,
            uploader["engagement"].id,
            ff.disguised_executable(),
            "payload.pdf",
        )

        assert list(isolated_file_storage.rglob("*")) == []

    @pytest.mark.parametrize(
        ("builder", "filename", "declared_type"),
        [
            (ff.elf_executable, "report.pdf", "application/pdf"),
            (ff.shell_script, "script.pdf", "application/pdf"),
            (ff.plain_zip, "evidence.docx", "application/zip"),
            (ff.zip_containing_executable, "policy.docx", "application/octet-stream"),
            (ff.svg_with_script, "diagram.png", "image/svg+xml"),
            (ff.html_file, "page.pdf", "text/html"),
            (ff.empty_file, "empty.pdf", "application/pdf"),
        ],
    )
    def test_files_outside_the_allow_list_are_rejected(
        self,
        api_client: TestClient,
        uploader: dict[str, Any],
        builder: Any,
        filename: str,
        declared_type: str,
    ) -> None:
        """The allow-list is of document formats, not container formats.

        The two ZIP cases matter most: DOCX and XLSX share the ZIP magic bytes,
        so anything deciding on the header alone would accept both of these.
        """
        login(api_client, uploader["auditor"])

        response = upload(api_client, uploader["engagement"].id, builder(), filename, declared_type)

        assert response.status_code == 400, f"{filename} should have been rejected"
        assert response.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"

    def test_oversized_upload_is_rejected_with_413(
        self, api_client: TestClient, db: DBSession, uploader: dict[str, Any]
    ) -> None:
        """TASK-016 and 04_API_CONTRACT.md: size ≤ 25MB, `413 FILE_TOO_LARGE`."""
        login(api_client, uploader["auditor"])

        response = upload(api_client, uploader["engagement"].id, ff.oversized(26), "huge.pdf")

        assert response.status_code == 413
        assert response.json()["error"]["code"] == "FILE_TOO_LARGE"
        assert db.scalar(select(func.count()).select_from(EvidenceDocument)) == 0

    def test_a_file_just_under_the_cap_is_accepted(
        self, api_client: TestClient, uploader: dict[str, Any]
    ) -> None:
        """The boundary matters in both directions — rejecting a legitimate
        24MB evidence bundle would be a self-inflicted outage."""
        login(api_client, uploader["auditor"])
        # A real PDF padded with a trailing comment stays parseable and under cap.
        content = ff.valid_pdf() + b"\n%" + b"A" * (20 * 1024 * 1024)

        response = upload(api_client, uploader["engagement"].id, content, "large.pdf")

        assert response.status_code == 201

    def test_the_error_message_does_not_echo_the_detected_type(
        self, api_client: TestClient, uploader: dict[str, Any]
    ) -> None:
        """Naming what was detected would turn the rejection into a probe
        oracle for mapping the filter."""
        login(api_client, uploader["auditor"])

        response = upload(api_client, uploader["engagement"].id, ff.elf_executable(), "x.pdf")

        message = response.json()["error"]["message"]
        assert "ELF" not in message
        assert "executable" not in message.lower()


class TestFilenameHandling:
    @pytest.mark.parametrize(
        "hostile_name",
        [
            "../../../../etc/passwd.pdf",
            "..\\..\\windows\\system32\\config.pdf",
            "/etc/shadow.pdf",
            "....//....//evidence.pdf",
            "evidence\x00.pdf",
            "evidence\n\rinjected.pdf",
        ],
    )
    def test_path_traversal_filenames_cannot_escape_storage(
        self,
        api_client: TestClient,
        uploader: dict[str, Any],
        isolated_file_storage: Path,
        hostile_name: str,
    ) -> None:
        """05_SECURITY.md §10.4: filenames sanitized against path traversal.

        The stronger guarantee is structural: the storage path is derived
        entirely from the content hash, so the filename never participates in
        building a path. This test asserts the outcome — every written file
        lands inside the storage root regardless of what it was called.
        """
        login(api_client, uploader["auditor"])

        response = upload(api_client, uploader["engagement"].id, ff.valid_pdf(), hostile_name)

        assert response.status_code == 201
        stored_name = response.json()["original_filename"]
        assert "/" not in stored_name
        assert "\\" not in stored_name
        assert "\x00" not in stored_name

        written = [p for p in isolated_file_storage.rglob("*") if p.is_file()]
        assert written, "the file should have been stored"
        for path in written:
            assert path.resolve().is_relative_to(isolated_file_storage.resolve())

    def test_sanitize_filename_handles_edge_cases(self) -> None:
        assert file_storage.sanitize_filename("../../etc/passwd") == "passwd"
        assert file_storage.sanitize_filename("") == "unnamed"
        assert file_storage.sanitize_filename("...") == "unnamed"
        assert file_storage.sanitize_filename("a" * 400) == "a" * 255
        assert "\x00" not in file_storage.sanitize_filename("bad\x00name.pdf")


class TestUploadAuthorization:
    def test_unassigned_auditor_cannot_upload(
        self, api_client: TestClient, make_user: Any, uploader: dict[str, Any]
    ) -> None:
        intruder = make_user(Role.auditor, password=PASSWORD)
        login(api_client, intruder)

        response = upload(api_client, uploader["engagement"].id, ff.valid_pdf(), "evidence.pdf")

        assert response.status_code == 403

    def test_unauthenticated_upload_is_rejected(
        self, api_client: TestClient, uploader: dict[str, Any]
    ) -> None:
        response = upload(api_client, uploader["engagement"].id, ff.valid_pdf(), "evidence.pdf")
        assert response.status_code == 401

    def test_evidence_request_from_another_engagement_cannot_be_linked(
        self,
        api_client: TestClient,
        db: DBSession,
        make_user: Any,
        make_engagement: Any,
        make_scoped_requirement: Any,
        uploader: dict[str, Any],
    ) -> None:
        """A valid request id belonging to a different engagement must not
        link across — that would attach one client's evidence to another's
        checklist item."""
        from app.repositories.scoping import EvidenceRequestRepository

        other_auditor = make_user(Role.auditor, password=PASSWORD)
        other_engagement = make_engagement(other_auditor, status=EngagementStatus.in_progress)
        other_scoped = make_scoped_requirement(other_engagement)
        foreign_request = EvidenceRequestRepository(db).create(
            engagement_id=other_engagement.id,
            scoped_requirement_id=other_scoped.id,
            description="Provide the firewall export.",
            description_source="template",
        )

        login(api_client, uploader["auditor"])
        response = upload(
            api_client,
            uploader["engagement"].id,
            ff.valid_pdf(),
            "evidence.pdf",
            evidence_request_id=str(foreign_request.id),
        )

        assert response.status_code == 404


class TestUploadSideEffects:
    def test_linking_to_a_request_marks_it_received(
        self,
        api_client: TestClient,
        db: DBSession,
        make_scoped_requirement: Any,
        uploader: dict[str, Any],
    ) -> None:
        """01_REQUIREMENTS.md Database Effects: "if linked, updates the
        referenced EvidenceRequest.status to received"."""
        from app.repositories.scoping import EvidenceRequestRepository

        scoped = make_scoped_requirement(uploader["engagement"])
        request = EvidenceRequestRepository(db).create(
            engagement_id=uploader["engagement"].id,
            scoped_requirement_id=scoped.id,
            description="Provide the firewall export.",
            description_source="template",
        )
        login(api_client, uploader["auditor"])

        response = upload(
            api_client,
            uploader["engagement"].id,
            ff.valid_pdf(),
            "evidence.pdf",
            evidence_request_id=str(request.id),
        )

        assert response.status_code == 201
        db.refresh(request)
        assert request.status == EvidenceRequestStatus.received

    def test_storage_is_content_addressed(
        self, api_client: TestClient, db: DBSession, uploader: dict[str, Any]
    ) -> None:
        import hashlib

        content = ff.valid_pdf("Content addressing check.")
        login(api_client, uploader["auditor"])

        response = upload(api_client, uploader["engagement"].id, content, "evidence.pdf")

        assert response.status_code == 201
        assert response.json()["content_hash"] == hashlib.sha256(content).hexdigest()

    def test_re_uploading_identical_content_is_refused(
        self, api_client: TestClient, uploader: dict[str, Any]
    ) -> None:
        """Content-addressed storage means a duplicate costs no disk, but a
        second EvidenceDocument row would double every Finding drawn from it."""
        content = ff.valid_pdf("Duplicate check.")
        login(api_client, uploader["auditor"])
        assert upload(api_client, uploader["engagement"].id, content, "a.pdf").status_code == 201

        response = upload(api_client, uploader["engagement"].id, content, "b.pdf")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DUPLICATE_EVIDENCE"

    def test_the_same_file_may_be_uploaded_to_two_engagements(
        self, api_client: TestClient, make_engagement: Any, uploader: dict[str, Any]
    ) -> None:
        """Deduplication is per engagement. Two clients may legitimately submit
        the same vendor attestation."""
        content = ff.valid_pdf("Shared vendor attestation.")
        second = make_engagement(
            uploader["auditor"], status=EngagementStatus.in_progress, client_name="Other"
        )
        login(api_client, uploader["auditor"])

        assert upload(api_client, uploader["engagement"].id, content, "a.pdf").status_code == 201
        assert upload(api_client, second.id, content, "a.pdf").status_code == 201

    def test_storage_path_is_never_returned_to_the_client(
        self, api_client: TestClient, uploader: dict[str, Any]
    ) -> None:
        """03_DATA_MODEL.md §8.4 classifies `storage_path` Sensitive."""
        login(api_client, uploader["auditor"])
        created = upload(
            api_client, uploader["engagement"].id, ff.valid_pdf(), "evidence.pdf"
        ).json()

        detail = api_client.get(f"/api/evidence-documents/{created['id']}").json()
        listed = api_client.get(
            f"/api/engagements/{uploader['engagement'].id}/evidence-documents"
        ).json()

        assert "storage_path" not in created
        assert "storage_path" not in detail
        assert "storage_path" not in listed[0]

    def test_download_is_served_as_an_attachment(
        self, api_client: TestClient, uploader: dict[str, Any]
    ) -> None:
        """An uploaded file rendered inline would execute in the app's own
        origin — stored XSS from an external, untrusted source."""
        content = ff.valid_pdf("Downloadable.")
        login(api_client, uploader["auditor"])
        created = upload(api_client, uploader["engagement"].id, content, "evidence.pdf").json()

        response = api_client.get(f"/api/evidence-documents/{created['id']}/download")

        assert response.status_code == 200
        assert response.content == content
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_download_is_ownership_filtered(
        self, api_client: TestClient, make_user: Any, uploader: dict[str, Any]
    ) -> None:
        login(api_client, uploader["auditor"])
        created = upload(
            api_client, uploader["engagement"].id, ff.valid_pdf(), "evidence.pdf"
        ).json()

        intruder = make_user(Role.auditor, password=PASSWORD)
        login(api_client, intruder)

        assert (
            api_client.get(f"/api/evidence-documents/{created['id']}/download").status_code == 403
        )
        assert api_client.get(f"/api/evidence-documents/{created['id']}").status_code == 403


class TestExtraction:
    """TASK-017: extraction sets `extraction_status` explicitly and never
    crashes the worker."""

    def _stored_document(
        self,
        db: DBSession,
        api_client: TestClient,
        uploader: dict[str, Any],
        content: bytes,
        filename: str,
        content_type: str = "application/pdf",
    ) -> EvidenceDocument:
        login(api_client, uploader["auditor"])
        response = upload(api_client, uploader["engagement"].id, content, filename, content_type)
        assert response.status_code == 201, response.text
        document = db.get(EvidenceDocument, uuid.UUID(response.json()["id"]))
        assert document is not None
        return document

    def test_valid_pdf_extracts_to_complete(
        self, api_client: TestClient, db: DBSession, uploader: dict[str, Any]
    ) -> None:
        """01_REQUIREMENTS.md acceptance criterion: `extraction_status`
        transitions from `processing` to `complete`."""
        document = self._stored_document(
            db,
            api_client,
            uploader,
            ff.valid_pdf("Cardholder data is encrypted at rest."),
            "policy.pdf",
        )

        process_extraction(db, document)

        assert document.extraction_status == ExtractionStatus.complete
        assert document.extraction_error is None
        assert document.extracted_text is not None
        assert "encrypted at rest" in document.extracted_text
        assert document.extraction_completed_at is not None

    def test_corrupt_pdf_becomes_extraction_failed_rather_than_crashing(
        self, api_client: TestClient, db: DBSession, uploader: dict[str, Any]
    ) -> None:
        """TASK-017: "Corrupt file → extraction_failed, not a crash."

        01_REQUIREMENTS.md is explicit that the file is still stored — it is
        still evidence — but no Finding is fabricated from it.
        """
        document = self._stored_document(db, api_client, uploader, ff.corrupt_pdf(), "corrupt.pdf")

        process_extraction(db, document)  # must not raise

        assert document.extraction_status == ExtractionStatus.extraction_failed
        assert document.extraction_error
        assert document.matching_status == "skipped"
        # The file itself is retained.
        assert Path(document.storage_path).exists()

    def test_truncated_pdf_fails_cleanly(
        self, api_client: TestClient, db: DBSession, uploader: dict[str, Any]
    ) -> None:
        document = self._stored_document(
            db, api_client, uploader, ff.truncated_pdf(), "truncated.pdf"
        )

        process_extraction(db, document)

        assert document.extraction_status == ExtractionStatus.extraction_failed

    def test_password_protected_pdf_gets_a_specific_rejection(
        self, api_client: TestClient, db: DBSession, uploader: dict[str, Any]
    ) -> None:
        """TASK-017 and 01_REQUIREMENTS.md Edge Cases: "rejected with a specific
        error asking the auditor to obtain an unprotected copy — the system does
        not attempt to guess or brute-force passwords"."""
        document = self._stored_document(
            db, api_client, uploader, ff.password_protected_pdf(), "protected.pdf"
        )

        process_extraction(db, document)

        assert document.extraction_status == ExtractionStatus.extraction_failed
        assert document.extraction_error is not None
        error = document.extraction_error.lower()
        assert "password" in error
        assert "unprotected copy" in error
        assert document.extracted_text is None

    def test_extraction_never_attempts_the_empty_password(self) -> None:
        """Trying even the empty password would be an attempt to defeat a
        protection measure. The encrypted branch returns before any decryption
        is tried."""
        result = extraction.extract(ff.password_protected_pdf(), "application/pdf")

        assert result.success is False
        assert result.error is not None
        assert "password-protected" in result.error.lower()
        assert result.sections == []

    def test_docx_extraction_captures_paragraphs_and_tables(self) -> None:
        result = extraction.extract(
            ff.valid_docx(["Encryption keys rotate annually."]),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        assert result.success is True
        locations = {s.location for s in result.sections}
        assert "document body" in locations
        assert any(loc.startswith("table") for loc in locations)
        assert "MFA enforcement" in result.full_text

    def test_xlsx_extraction_names_the_sheet_and_row(self) -> None:
        """The location string is what a Finding cites, so it has to be
        specific enough for an auditor to find the cell again."""
        result = extraction.extract(
            ff.valid_xlsx(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        assert result.success is True
        assert result.sections[0].location == "sheet 'Firewall'"
        assert "row 1:" in result.sections[0].text
        assert "inbound 443" in result.full_text

    def test_multipage_pdf_locations_are_per_page(self) -> None:
        result = extraction.extract(
            ff.multipage_pdf(["Page one content.", "Page two content."]),
            "application/pdf",
        )

        assert result.success is True
        assert [s.location for s in result.sections] == ["page 1", "page 2"]

    def test_image_with_no_text_layer_fails_gracefully(self) -> None:
        """Whether tesseract is installed or not, this must produce a status
        rather than an exception."""
        result = extraction.extract(ff.valid_png(text_like=False), "image/png")

        assert result.success is False
        assert result.error

    def test_unreadable_stored_file_marks_the_document_failed(
        self, api_client: TestClient, db: DBSession, uploader: dict[str, Any]
    ) -> None:
        """A missing file on disk is an operational fault, not a crash."""
        document = self._stored_document(db, api_client, uploader, ff.valid_pdf(), "evidence.pdf")
        Path(document.storage_path).unlink()

        process_extraction(db, document)

        assert document.extraction_status == ExtractionStatus.extraction_failed
        assert document.extraction_error


class TestChunking:
    def test_chunks_never_span_sections(self) -> None:
        """A chunk's `location` is what a Finding cites. If a chunk spanned two
        pages its citation would be wrong, which is worse than no citation."""
        sections = [
            extraction.ExtractedSection("page 1", "A" * 100),
            extraction.ExtractedSection("page 2", "B" * 100),
        ]

        chunks = extraction.chunk_sections(sections, max_chars=1500)

        assert len(chunks) == 2
        assert chunks[0] == ("page 1", "A" * 100)
        assert chunks[1] == ("page 2", "B" * 100)

    def test_long_sections_are_split_with_part_numbered_locations(self) -> None:
        sections = [extraction.ExtractedSection("page 1", "word " * 1000)]

        chunks = extraction.chunk_sections(sections, max_chars=500, overlap=50)

        assert len(chunks) > 1
        assert all(location.startswith("page 1 (part ") for location, _ in chunks)
        assert all(len(text) <= 500 for _, text in chunks)

    def test_empty_sections_produce_no_chunks(self) -> None:
        chunks = extraction.chunk_sections([extraction.ExtractedSection("page 1", "   \n  ")])
        assert chunks == []


class TestStuckExtractionSweep:
    def test_documents_stuck_in_processing_are_swept_to_failed(
        self, api_client: TestClient, db: DBSession, uploader: dict[str, Any]
    ) -> None:
        """02_ARCHITECTURE.md §7.5 requires this explicitly: a row must never
        sit in `processing` indefinitely, or the auditor waits forever on a
        result that is never coming, with nothing on screen to say so."""
        from datetime import UTC, datetime, timedelta

        from app.repositories.evidence import EvidenceDocumentRepository

        login(api_client, uploader["auditor"])
        created = upload(api_client, uploader["engagement"].id, ff.valid_pdf(), "stuck.pdf").json()
        document = db.get(EvidenceDocument, uuid.UUID(created["id"]))
        assert document is not None

        document.extraction_started_at = datetime.now(UTC) - timedelta(minutes=30)
        db.flush()

        swept = EvidenceDocumentRepository(db).sweep_stuck_extractions(timedelta(minutes=10))

        assert [d.id for d in swept] == [document.id]
        assert document.extraction_status == ExtractionStatus.extraction_failed
        assert document.extraction_error is not None
        assert "manually" in document.extraction_error

    def test_recently_started_extractions_are_not_swept(
        self, api_client: TestClient, db: DBSession, uploader: dict[str, Any]
    ) -> None:
        from datetime import UTC, datetime, timedelta

        from app.repositories.evidence import EvidenceDocumentRepository

        login(api_client, uploader["auditor"])
        created = upload(api_client, uploader["engagement"].id, ff.valid_pdf(), "fresh.pdf").json()
        document = db.get(EvidenceDocument, uuid.UUID(created["id"]))
        assert document is not None
        document.extraction_started_at = datetime.now(UTC) - timedelta(minutes=1)
        db.flush()

        swept = EvidenceDocumentRepository(db).sweep_stuck_extractions(timedelta(minutes=10))

        assert swept == []
        assert document.extraction_status == ExtractionStatus.processing

    def test_unclaimed_documents_are_not_swept(
        self, api_client: TestClient, db: DBSession, uploader: dict[str, Any]
    ) -> None:
        """A document nobody has started yet is queued, not stuck."""
        from datetime import timedelta

        from app.repositories.evidence import EvidenceDocumentRepository

        login(api_client, uploader["auditor"])
        upload(api_client, uploader["engagement"].id, ff.valid_pdf(), "queued.pdf")

        swept = EvidenceDocumentRepository(db).sweep_stuck_extractions(timedelta(minutes=10))

        assert swept == []
