"""OCR provider tests.

Two behaviours carry weight here.

The **fallback** is the reason the API path is safe to enable: an outage at a
third party must not turn a legible screenshot into a document that silently
yields nothing. And when the fallback is deliberately switched off, the failure
has to surface as a configuration fault rather than as "no text found", or an
auditor re-uploads the same file forever.

The **response parsing** is pinned per provider because the two APIs return
completely different shapes, and a wrong parse looks exactly like an empty
image.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.config.settings import settings
from app.pipelines import ocr
from tests import filefixtures as ff

GOOGLE_OK = {"responses": [{"fullTextAnnotation": {"text": "minimum password length: 14"}}]}
OCR_SPACE_OK = {
    "IsErroredOnProcessing": False,
    "ParsedResults": [{"ParsedText": "minimum password length: 14"}],
}


@pytest.fixture
def png() -> bytes:
    return ff.valid_png()


@pytest.fixture(autouse=True)
def _restore_settings() -> Any:
    original = (
        settings.OCR_PROVIDER,
        settings.OCR_API_KEY,
        settings.OCR_FALLBACK_TO_LOCAL,
    )
    yield
    (
        settings.OCR_PROVIDER,
        settings.OCR_API_KEY,
        settings.OCR_FALLBACK_TO_LOCAL,
    ) = original


def _fake_post(payload: dict[str, Any], status: int = 200) -> Any:
    def post(*args: Any, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            status_code=status, json=payload, request=httpx.Request("POST", "https://ocr.test")
        )

    return post


class TestProviderParsing:
    def test_google_vision_reads_the_dense_text_field(
        self, monkeypatch: pytest.MonkeyPatch, png: bytes
    ) -> None:
        settings.OCR_PROVIDER = "google_vision"
        settings.OCR_API_KEY = type(settings.OCR_API_KEY)("k")
        monkeypatch.setattr(httpx, "post", _fake_post(GOOGLE_OK))

        result = ocr.run(png)

        assert result.text == "minimum password length: 14"
        assert result.provider == "google_vision"

    def test_ocr_space_joins_its_parsed_results(
        self, monkeypatch: pytest.MonkeyPatch, png: bytes
    ) -> None:
        settings.OCR_PROVIDER = "ocr_space"
        settings.OCR_API_KEY = type(settings.OCR_API_KEY)("k")
        monkeypatch.setattr(httpx, "post", _fake_post(OCR_SPACE_OK))

        assert ocr.run(png).text == "minimum password length: 14"

    def test_a_provider_error_payload_is_not_read_as_empty_text(
        self, monkeypatch: pytest.MonkeyPatch, png: bytes
    ) -> None:
        """A 200 response carrying an error must not be mistaken for a blank
        image — that would report "no readable text" for a working document."""
        settings.OCR_PROVIDER = "ocr_space"
        settings.OCR_API_KEY = type(settings.OCR_API_KEY)("k")
        settings.OCR_FALLBACK_TO_LOCAL = False
        monkeypatch.setattr(
            httpx,
            "post",
            _fake_post({"IsErroredOnProcessing": True, "ErrorMessage": "quota exceeded"}),
        )

        with pytest.raises(ocr.OCRError, match="quota"):
            ocr.run(png)


class TestFallback:
    def test_an_api_outage_falls_back_to_local(
        self, monkeypatch: pytest.MonkeyPatch, png: bytes
    ) -> None:
        settings.OCR_PROVIDER = "google_vision"
        settings.OCR_API_KEY = type(settings.OCR_API_KEY)("k")
        settings.OCR_FALLBACK_TO_LOCAL = True

        def boom(*args: Any, **kwargs: Any) -> None:
            raise httpx.ConnectError("unreachable")

        monkeypatch.setattr(httpx, "post", boom)
        monkeypatch.setattr(ocr, "_tesseract", lambda content: "local text")

        result = ocr.run(png)

        assert result.text == "local text"
        # Named, not silently substituted — the report should be able to say
        # which provider actually read the evidence.
        assert "fallback" in result.provider

    def test_fallback_off_surfaces_the_failure(
        self, monkeypatch: pytest.MonkeyPatch, png: bytes
    ) -> None:
        settings.OCR_PROVIDER = "google_vision"
        settings.OCR_API_KEY = type(settings.OCR_API_KEY)("k")
        settings.OCR_FALLBACK_TO_LOCAL = False

        def boom(*args: Any, **kwargs: Any) -> None:
            raise httpx.ConnectError("unreachable")

        monkeypatch.setattr(httpx, "post", boom)

        with pytest.raises(ocr.OCRError):
            ocr.run(png)

    def test_a_missing_key_is_a_configuration_error(self, png: bytes) -> None:
        settings.OCR_PROVIDER = "google_vision"
        settings.OCR_API_KEY = type(settings.OCR_API_KEY)("")
        settings.OCR_FALLBACK_TO_LOCAL = False

        with pytest.raises(ocr.OCRError, match="not configured"):
            ocr.run(png)


class TestDefault:
    def test_the_default_provider_sends_nothing_anywhere(
        self, monkeypatch: pytest.MonkeyPatch, png: bytes
    ) -> None:
        """Client evidence leaving the building is a disclosure decision, so the
        shipped default must not make it."""
        assert settings.OCR_PROVIDER == "tesseract"

        def fail(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("tesseract mode must not make a network call")

        monkeypatch.setattr(httpx, "post", fail)
        monkeypatch.setattr(ocr, "_tesseract", lambda content: "local text")

        assert ocr.run(png).provider == "tesseract"
