"""LLM client (ADR-009, 02_ARCHITECTURE.md §7.6).

Every reasoning call in the system goes through `LLMClient`. Routes and
repositories never import this module — only `/services` and `/pipelines` do.

Three properties this module is responsible for, because getting them wrong at
each call site would be worse:

* **Timeouts** are per-call and mandatory: 8s interactive, 30s background.
* **Retry** is one attempt with backoff for transient failures (5xx, timeout)
  and never for 4xx, because a malformed request will be just as malformed the
  second time.
* **Logging** is metadata only — duration, status, token counts. Never the
  prompt, never the completion (05_SECURITY.md §10.7). The prompt contains
  client evidence text; a log line containing it would be a disclosure.

Prompt injection: extracted document text is untrusted input
(05_SECURITY.md §10.1). It is passed as clearly-delimited data with an
instruction that content inside the delimiters is evidence to assess, never
instructions to follow. That hardening is real but secondary — the actual
backstop is that no Finding is ever auto-approved, which is why a successful
injection produces a draft a human still has to accept.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

from app.config.settings import settings
from app.logging_setup import log_external_call

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Any LLM failure. Callers must degrade, never propagate this to a 500 —
    every feature that calls the LLM has a defined non-LLM fallback state
    (02_ARCHITECTURE.md §7.6)."""


class LLMTimeoutError(LLMError):
    pass


@dataclass(frozen=True)
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int

    def as_json(self) -> Any:
        """Parse the model's reply as JSON.

        Models sometimes wrap JSON in prose or a markdown fence despite
        instructions. Recovering the outermost JSON value is cheaper and more
        reliable than failing the whole pipeline step over formatting, and a
        genuinely unparseable reply still raises so the caller can fall back.
        """
        text = self.text.strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = min(
                (i for i in (text.find("{"), text.find("[")) if i != -1),
                default=-1,
            )
            end = max(text.rfind("}"), text.rfind("]"))
            if start != -1 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    pass
            raise LLMError("The model did not return parseable JSON.") from None


class LLMClient(Protocol):
    def complete(
        self, *, system: str, prompt: str, timeout: float, max_tokens: int = 2048
    ) -> LLMResponse: ...


class AnthropicLLMClient:
    """Anthropic implementation (ADR-009)."""

    # 5xx and timeouts are transient and worth one retry; 4xx is not.
    _RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model
        self._client: Any | None = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            if not self._api_key:
                raise LLMError("LLM_API_KEY is not configured.")
            import anthropic

            self._client = anthropic.Anthropic(api_key=self._api_key, max_retries=0)
        return self._client

    def complete(
        self, *, system: str, prompt: str, timeout: float, max_tokens: int = 2048
    ) -> LLMResponse:
        import anthropic

        client = self._ensure_client()
        started = time.monotonic()
        last_error: Exception | None = None

        # One initial attempt plus one retry (02_ARCHITECTURE.md §7.6).
        for attempt in range(2):
            try:
                message = client.messages.create(
                    model=self._model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                    timeout=timeout,
                )
            except anthropic.APIStatusError as exc:
                last_error = exc
                if exc.status_code not in self._RETRYABLE_STATUS or attempt == 1:
                    self._log(started, f"http_{exc.status_code}")
                    raise LLMError(f"LLM request failed with status {exc.status_code}") from exc
            except (anthropic.APITimeoutError, anthropic.APIConnectionError) as exc:
                last_error = exc
                if attempt == 1:
                    self._log(started, "timeout")
                    raise LLMTimeoutError("LLM request timed out") from exc
            else:
                duration_ms = (time.monotonic() - started) * 1000
                log_external_call(
                    service="llm",
                    operation="complete",
                    duration_ms=duration_ms,
                    status="ok",
                )
                logger.info(
                    "llm.tokens input=%d output=%d",
                    message.usage.input_tokens,
                    message.usage.output_tokens,
                )
                text = "".join(
                    block.text for block in message.content if getattr(block, "type", "") == "text"
                )
                return LLMResponse(
                    text=text,
                    input_tokens=message.usage.input_tokens,
                    output_tokens=message.usage.output_tokens,
                )

            # Brief backoff before the single retry.
            time.sleep(0.5 * (attempt + 1))

        self._log(started, "failed")
        raise LLMError("LLM request failed") from last_error

    @staticmethod
    def _log(started: float, status: str) -> None:
        log_external_call(
            service="llm",
            operation="complete",
            duration_ms=(time.monotonic() - started) * 1000,
            status=status,
        )


def wrap_untrusted(label: str, content: str, *, limit: int = 20_000) -> str:
    """Delimit untrusted document content for inclusion in a prompt.

    The delimiter is stripped from the content itself so the text cannot close
    its own block and escape into the instruction context. The length cap keeps
    a large document from crowding out the instructions entirely.
    """
    marker = f"<<<{label}>>>"
    end_marker = f"<<<END_{label}>>>"
    cleaned = content.replace(marker, "").replace(end_marker, "")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "\n[content truncated]"
    return f"{marker}\n{cleaned}\n{end_marker}"


_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = AnthropicLLMClient(settings.LLM_API_KEY.get_secret_value(), settings.LLM_MODEL)
    return _client


def set_llm_client(client: LLMClient | None) -> None:
    """Override the client. Tests mock at this boundary (08_TESTING.md)."""
    global _client
    _client = client
