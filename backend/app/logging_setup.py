"""Logging configuration and the request-id context.

05_SECURITY.md §10.7 lists what must never reach a log line: `password_hash`,
session tokens, API keys, `extracted_text`, `tech_stack_summary`, and full LLM
prompts or completions. That is enforced two ways here:

1. Structurally — call sites log metadata (duration, status, token count), and
   the helpers in this module take no free-form payload argument.
2. As a backstop — `SecretRedactingFilter` scrubs anything that still looks like
   a credential, so a careless future log line degrades to a redacted string
   rather than a leak. 08_TESTING.md requires a test that scans log output for
   secret patterns after a full request cycle; that test targets this filter.
"""

from __future__ import annotations

import logging
import re
import sys
import uuid
from contextvars import ContextVar
from typing import Any

_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def set_request_id(value: str) -> None:
    _request_id.set(value)


def get_request_id() -> str:
    return _request_id.get()


class RequestIdFilter(logging.Filter):
    """Attaches the current request id to every record (02_ARCHITECTURE.md §7.8)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


# Patterns that must never appear in a log line. Ordered most-specific first.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Anthropic / OpenAI style API keys
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "***REDACTED_API_KEY***"),
    # Argon2 password hashes
    (re.compile(r"\$argon2[a-z]{1,3}\$[^\s\"']+"), "***REDACTED_HASH***"),
    # Postgres connection strings with inline credentials
    (re.compile(r"postgresql(?:\+\w+)?://[^:\s]+:[^@\s]+@"), "postgresql://***:***@"),
    # Our own session cookie
    (re.compile(r"(auditlens_session=)[A-Za-z0-9_\-]+"), r"\1***REDACTED***"),
    # Bearer tokens
    (re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"), r"\1***REDACTED***"),
)


class SecretRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._scrub(v) for k, v in record.args.items()}
            else:
                record.args = tuple(self._scrub(a) for a in record.args)
        return True

    @staticmethod
    def _scrub(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        for pattern, replacement in _SECRET_PATTERNS:
            value = pattern.sub(replacement, value)
        return value


def configure_logging(environment: str) -> None:
    """Configure root logging to stdout (09_DEPLOYMENT.md — Docker captures it)."""
    level = logging.INFO if environment == "production" else logging.DEBUG

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    handler.addFilter(RequestIdFilter())
    handler.addFilter(SecretRedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # SQLAlchemy echoes full statements including bound parameters at INFO.
    # Those parameters include password hashes and extracted evidence text, so
    # this stays at WARNING in every environment — not just production.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


# --- Audit logging -----------------------------------------------------------
# 02_ARCHITECTURE.md §7.8 requires: every authentication attempt, every
# authorization denial, and every Finding status transition with actor and
# timestamp. These helpers take only identifiers and enum values, so there is no
# parameter through which document content or a credential could be passed.

_audit = logging.getLogger("auditlens.audit")


def log_auth_attempt(*, email_hash: str, success: bool, reason: str) -> None:
    """Log a login attempt. The email is hashed, never recorded in the clear."""
    _audit.info(
        "auth.attempt outcome=%s reason=%s subject=%s",
        "success" if success else "failure",
        reason,
        email_hash,
    )


def log_authz_denial(*, actor_id: str, action: str, resource_type: str, resource_id: str) -> None:
    _audit.warning(
        "authz.denied actor=%s action=%s resource=%s/%s",
        actor_id,
        action,
        resource_type,
        resource_id,
    )


def log_finding_transition(
    *, actor_id: str, finding_id: str, action: str, previous_status: str, new_status: str
) -> None:
    _audit.info(
        "finding.transition actor=%s finding=%s action=%s %s->%s",
        actor_id,
        finding_id,
        action,
        previous_status,
        new_status,
    )


def log_admin_audit_access(*, actor_id: str, audit_id: str) -> None:
    """03_DATA_MODEL.md §8.2: Admin access to audit content is logged
    distinctly from normal Reviewer access."""
    _audit.warning(
        "admin.audit_access actor=%s audit=%s",
        actor_id,
        audit_id,
    )


def log_external_call(*, service: str, operation: str, duration_ms: float, status: str) -> None:
    """05_SECURITY.md §10.7: metadata only — never the prompt or the completion."""
    _audit.info(
        "external.call service=%s op=%s duration_ms=%.1f status=%s",
        service,
        operation,
        duration_ms,
        status,
    )
