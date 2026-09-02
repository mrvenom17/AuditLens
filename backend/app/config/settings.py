"""Environment-loaded configuration.

Every secret in this module comes from the environment and is never hardcoded
(05_SECURITY.md §10.6). The variable names match 09_DEPLOYMENT.md's table
exactly — adding one here means adding it there and to `.env.example` too
(06_ENGINEERING_RULES.md § Secrets).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Development-only defaults, named as constants so the production validation can
# compare against them by identity rather than by a duplicated literal that
# could drift out of sync with the value it is meant to reject.
DEV_DATABASE_URL = "postgresql+psycopg://auditlens:auditlens@localhost:5432/auditlens"
DEV_SESSION_SECRET = "dev-only-insecure-change-me"  # noqa: S105 — a rejected placeholder


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Core -------------------------------------------------------------
    ENVIRONMENT: Literal["local", "test", "production"] = "local"
    # The development default exists so that tooling (alembic, tests, the corpus
    # loader) can import settings without a full environment. It is rejected in
    # production by `validate_for_environment` — a deployment that forgot to set
    # DATABASE_URL must fail loudly rather than quietly connect somewhere else
    # with a guessable credential.
    DATABASE_URL: str = DEV_DATABASE_URL
    SESSION_SECRET: SecretStr = SecretStr(DEV_SESSION_SECRET)

    # --- External services -------------------------------------------------
    LLM_API_KEY: SecretStr = SecretStr("")
    LLM_MODEL: str = "claude-sonnet-5"
    EMBEDDING_MODEL_PATH: str = "BAAI/bge-small-en-v1.5"
    EMBEDDING_DIMENSIONS: int = 384

    # --- OCR ---------------------------------------------------------------
    # `tesseract` runs locally and needs no key; the API providers read scanned
    # and photographed evidence far better, at the cost of sending the image to
    # a third party. That is a real disclosure decision for client evidence, so
    # the default stays local and switching is explicit.
    OCR_PROVIDER: Literal["tesseract", "google_vision", "ocr_space"] = "tesseract"
    OCR_API_KEY: SecretStr = SecretStr("")
    # Overridable so a self-hosted or proxied endpoint can be used instead.
    OCR_API_URL: str = ""
    OCR_TIMEOUT_SECONDS: float = 30.0
    # When the API fails, retry locally rather than failing the document. The
    # auditor gets *some* text plus a logged warning, instead of an upload that
    # silently yields nothing.
    OCR_FALLBACK_TO_LOCAL: bool = True

    # --- Storage -----------------------------------------------------------
    FILE_STORAGE_PATH: str = "./data/evidence"
    MAX_UPLOAD_SIZE_MB: int = 25

    # --- Transport ---------------------------------------------------------
    CORS_ALLOWED_ORIGIN: str = "http://localhost:3000"

    # --- Tunables with documented defaults ---------------------------------
    # 05_SECURITY.md §10.2 / 01_REQUIREMENTS.md §User Authentication
    SESSION_IDLE_TIMEOUT_HOURS: int = 8
    SESSION_ABSOLUTE_TIMEOUT_HOURS: int = 24
    LOGIN_MAX_ATTEMPTS: int = 5
    LOGIN_LOCKOUT_WINDOW_MINUTES: int = 15

    # 02_ARCHITECTURE.md §7.6
    LLM_INTERACTIVE_TIMEOUT_SECONDS: float = 8.0
    LLM_BACKGROUND_TIMEOUT_SECONDS: float = 30.0

    # 04_API_CONTRACT.md § scope-suggestion rate limit
    SCOPE_SUGGESTION_PER_HOUR: int = 10

    # 09_DEPLOYMENT.md — stamped onto every ControlEvaluation and every Report,
    # so a result can always be traced to the exact logic and corpus that
    # produced it, and a later change to either cannot rewrite history.
    RULE_ENGINE_VERSION: str = "1.0.0"
    CONTROL_CORPUS_VERSION: str = "pci-dss-v4.0.1-poc-2"

    # 02_ARCHITECTURE.md §7.5 — stuck-in-processing sweep
    EXTRACTION_STUCK_TIMEOUT_MINUTES: int = 10
    WORKER_POLL_INTERVAL_SECONDS: float = 5.0

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def max_upload_bytes(self) -> int:
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024

    def validate_for_environment(self) -> None:
        """Fail fast on a misconfigured production start.

        Called from the app lifespan rather than at import time so that tests and
        tooling can import settings without a full production environment.
        """
        if not self.is_production:
            return
        problems: list[str] = []
        if self.SESSION_SECRET.get_secret_value() == DEV_SESSION_SECRET:
            problems.append("SESSION_SECRET is still the development default")
        if self.DATABASE_URL == DEV_DATABASE_URL:
            problems.append("DATABASE_URL is still the development default")
        if len(self.SESSION_SECRET.get_secret_value()) < 32:
            problems.append("SESSION_SECRET must be at least 32 bytes (09_DEPLOYMENT.md)")
        if not self.LLM_API_KEY.get_secret_value():
            problems.append("LLM_API_KEY is not set")
        if self.CORS_ALLOWED_ORIGIN.startswith("http://"):
            problems.append(
                "CORS_ALLOWED_ORIGIN must be https in production (05_SECURITY.md §10.9)"
            )
        if problems:
            # The message names the variables, never their values.
            raise RuntimeError("Invalid production configuration: " + "; ".join(problems))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
