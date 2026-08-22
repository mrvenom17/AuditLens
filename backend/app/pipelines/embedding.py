"""Self-hosted embedding client (ADR-005).

Evidence content never leaves the server for this step — that is the whole point
of self-hosting a small model for the mechanical vectorization work while
reserving the external API for reasoning.

The model is loaded lazily on first use and only in processes that actually
embed (the worker and the corpus loader). The API process imports this module
but never triggers a load, which is what keeps torch out of its image.
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

from app.config.settings import settings

logger = logging.getLogger(__name__)


class EmbeddingUnavailableError(RuntimeError):
    """Raised when the model cannot be loaded or an embedding call fails.

    Callers must treat this as "matching is deferred", never as "the upload
    failed" — 02_ARCHITECTURE.md §7.6 requires extraction to complete and be
    stored even when the embedding service is down.
    """


class EmbeddingClient(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class SentenceTransformerEmbeddingClient:
    """Wraps a local sentence-transformers model."""

    def __init__(self, model_path: str, dimensions: int) -> None:
        self._model_path = model_path
        self._dimensions = dimensions
        self._model: object | None = None
        self._lock = threading.Lock()

    def _ensure_model(self) -> object:
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise EmbeddingUnavailableError(
                    "sentence-transformers is not installed in this process. "
                    "Embedding runs in the worker image only (ADR-009)."
                ) from exc
            logger.info("Loading embedding model %s", self._model_path)
            try:
                self._model = SentenceTransformer(self._model_path)
            except Exception as exc:
                raise EmbeddingUnavailableError(
                    f"Could not load embedding model {self._model_path}"
                ) from exc
            return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._ensure_model()
        try:
            # Normalised vectors, so cosine distance in the HNSW index is a
            # straight dot product.
            vectors = model.encode(  # type: ignore[attr-defined]
                texts, normalize_embeddings=True, show_progress_bar=False
            )
        except Exception as exc:
            raise EmbeddingUnavailableError("Embedding computation failed") from exc

        result = [list(map(float, v)) for v in vectors]
        for vector in result:
            if len(vector) != self._dimensions:
                raise EmbeddingUnavailableError(
                    f"Model returned {len(vector)}-dim vectors, "
                    f"but the schema expects {self._dimensions}. "
                    "Set EMBEDDING_DIMENSIONS to match and migrate the vector columns."
                )
        return result


_client: EmbeddingClient | None = None


def get_embedding_client() -> EmbeddingClient:
    global _client
    if _client is None:
        _client = SentenceTransformerEmbeddingClient(
            settings.EMBEDDING_MODEL_PATH, settings.EMBEDDING_DIMENSIONS
        )
    return _client


def set_embedding_client(client: EmbeddingClient | None) -> None:
    """Override the client. Used by tests to avoid loading a real model
    (08_TESTING.md § Mocking boundaries)."""
    global _client
    _client = client
