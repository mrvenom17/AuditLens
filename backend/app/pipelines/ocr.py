"""OCR providers — local Tesseract, or a hosted API.

Tesseract is the default because it runs offline and sends nothing anywhere.
That matters here more than in most systems: the images are a client's
configuration screenshots, and posting them to a third party is a disclosure
decision somebody has to make deliberately rather than inherit from a default.

The API providers exist because Tesseract is weak on exactly the evidence
auditors get most often — phone photos of a screen, skewed scans, low-contrast
console output. When that is the input, a hosted OCR is not a luxury.

Whichever provider runs, the text it returns is *evidence content*, never
instructions. It goes to the same fact scanner as everything else and cannot
reach a compliance result — an OCR provider is no more trusted than the
document it read.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass

from app.config.settings import settings

logger = logging.getLogger(__name__)

# Documented endpoints, used when OCR_API_URL is left blank.
_DEFAULT_URLS = {
    "google_vision": "https://vision.googleapis.com/v1/images:annotate",
    "ocr_space": "https://api.ocr.space/parse/image",
}


class OCRError(Exception):
    """The provider could not be reached or refused the request.

    Distinct from "the image contains no text": that is a legitimate answer
    about the evidence, while this is an operational fault the auditor cannot
    fix by re-uploading.
    """


@dataclass(frozen=True)
class OCRResult:
    text: str
    provider: str


def run(content: bytes) -> OCRResult:
    """Extract text from an image, honouring the configured provider.

    Falls back to local Tesseract when the API fails and
    `OCR_FALLBACK_TO_LOCAL` is on — a degraded read beats a document that
    silently yields nothing, and the fallback is logged rather than hidden.
    """
    provider = settings.OCR_PROVIDER

    if provider == "tesseract":
        return OCRResult(text=_tesseract(content), provider="tesseract")

    started = time.monotonic()
    try:
        text = _via_api(content, provider)
    except OCRError as exc:
        logger.warning("ocr.api_failed provider=%s reason=%s", provider, exc)
        if not settings.OCR_FALLBACK_TO_LOCAL:
            raise
        logger.info("ocr.fallback provider=tesseract")
        return OCRResult(text=_tesseract(content), provider="tesseract (fallback)")

    logger.info(
        "ocr.completed provider=%s duration_ms=%.0f chars=%d",
        provider,
        (time.monotonic() - started) * 1000,
        len(text),
    )
    return OCRResult(text=text, provider=provider)


def _via_api(content: bytes, provider: str) -> str:
    import httpx

    key = settings.OCR_API_KEY.get_secret_value()
    if not key:
        raise OCRError("OCR_API_KEY is not configured")

    url = settings.OCR_API_URL or _DEFAULT_URLS[provider]
    encoded = base64.b64encode(content).decode()

    try:
        if provider == "google_vision":
            response = httpx.post(
                url,
                params={"key": key},
                json={
                    "requests": [
                        {
                            "image": {"content": encoded},
                            # DOCUMENT_TEXT_DETECTION is the dense-text model;
                            # TEXT_DETECTION is tuned for signage and reads a
                            # config screenshot badly.
                            "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                        }
                    ]
                },
                timeout=settings.OCR_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()["responses"][0]
            if "error" in payload:
                raise OCRError(str(payload["error"].get("message", "unknown error")))
            return str(payload.get("fullTextAnnotation", {}).get("text", "")).strip()

        # ocr_space
        response = httpx.post(
            url,
            data={
                "base64Image": f"data:image/png;base64,{encoded}",
                "OCREngine": "2",
                "scale": "true",
            },
            headers={"apikey": key},
            timeout=settings.OCR_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("IsErroredOnProcessing"):
            raise OCRError(str(payload.get("ErrorMessage", "unknown error")))
        return "\n".join(
            str(r.get("ParsedText", "")) for r in payload.get("ParsedResults", [])
        ).strip()

    except httpx.HTTPError as exc:
        # ponytail: no retry — the worker already re-claims a deferred document
        # on its next pass, so a transient blip is retried at that level.
        raise OCRError(f"{type(exc).__name__}") from exc
    except (KeyError, IndexError, ValueError) as exc:
        raise OCRError(f"unexpected response shape: {type(exc).__name__}") from exc


def _tesseract(content: bytes) -> str:
    """Local OCR. Raises OCRError when the binary is missing, because that is a
    server misconfiguration and not a property of the image."""
    import io

    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise OCRError("pytesseract/Pillow is not installed") from exc

    try:
        image = Image.open(io.BytesIO(content))
        image.load()
    except Exception as exc:
        raise OCRError("image could not be decoded") from exc

    try:
        return str(pytesseract.image_to_string(image)).strip()
    except pytesseract.TesseractNotFoundError as exc:
        raise OCRError("the Tesseract binary is not installed") from exc
