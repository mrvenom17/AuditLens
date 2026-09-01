"""Application error taxonomy.

02_ARCHITECTURE.md §7.7 requires a single standardized error envelope and a hard
split between user-visible errors (400/403/404/409, stable machine-readable
`code`) and internal errors (logged with a `request_id`, generic message to the
client). Every class here is user-visible by construction; anything not derived
from `AppError` becomes a generic 500 in the handler, which is what keeps stack
traces and database internals off the wire.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base for every error that is safe to describe to the client."""

    status_code: int = 400
    code: str = "APP_ERROR"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        # Extra machine-readable context (e.g. the blocking requirement list on
        # a finalization conflict). Never populate this with anything Sensitive
        # or Secret per 03_DATA_MODEL.md §8.4.
        self.details: dict[str, Any] = details


class ValidationError(AppError):
    status_code = 400
    code = "VALIDATION_ERROR"

    def __init__(self, message: str, code: str | None = None, **details: Any) -> None:
        """`code` allows a specific machine-readable name for a validation the
        API contract singles out — e.g. DETERMINISTIC_CONTROL_MISSING_RULES,
        which 04_API_CONTRACT.md names explicitly because it guards the rule
        engine's whole reason for existing."""
        super().__init__(message, **details)
        if code:
            self.code = code


class AuthenticationError(AppError):
    """401 — no valid session. Distinct from authorization failure."""

    status_code = 401
    code = "NOT_AUTHENTICATED"


class InvalidCredentialsError(AppError):
    """401 — used identically for wrong password and unknown email.

    01_REQUIREMENTS.md § User Authentication, Explicitly Forbidden Behavior: the
    API must never distinguish these two cases.
    """

    status_code = 401
    code = "INVALID_CREDENTIALS"


class TooManyAttemptsError(AppError):
    status_code = 429
    code = "TOO_MANY_ATTEMPTS"

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            "Too many attempts. Try again later.",
            retry_after=retry_after_seconds,
        )
        self.retry_after_seconds = retry_after_seconds


class ForbiddenError(AppError):
    status_code = 403
    code = "FORBIDDEN"

    def __init__(self, message: str = "You do not have access to this resource.") -> None:
        super().__init__(message)


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"

    def __init__(self, message: str = "Resource not found.") -> None:
        super().__init__(message)


class ConflictError(AppError):
    """409 — the request is well-formed but the resource is in the wrong state."""

    status_code = 409
    code = "CONFLICT"

    def __init__(self, message: str, code: str | None = None, **details: Any) -> None:
        super().__init__(message, **details)
        if code:
            self.code = code


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "FILE_TOO_LARGE"


class UnsupportedFileTypeError(AppError):
    status_code = 400
    code = "UNSUPPORTED_FILE_TYPE"


# --- Named conflict codes referenced by 04_API_CONTRACT.md -------------------
# These exist as constants rather than inline strings so a rename is a single
# edit and a typo is a NameError instead of a silently wrong error contract.

CODE_MISSING_PROFILE_FIELDS = "MISSING_PROFILE_FIELDS"
CODE_NO_CONFIRMED_SCOPE = "NO_CONFIRMED_SCOPE"
CODE_UNRESOLVED_FINDINGS = "UNRESOLVED_FINDINGS"
CODE_ALREADY_FINALIZED = "ALREADY_FINALIZED"
CODE_AUDIT_FINALIZED = "AUDIT_FINALIZED"
CODE_ALREADY_ASSIGNED = "ALREADY_ASSIGNED"
CODE_EMAIL_ALREADY_EXISTS = "EMAIL_ALREADY_EXISTS"
CODE_EXTRACTION_NOT_COMPLETE = "EXTRACTION_NOT_COMPLETE"
CODE_RATE_LIMITED = "RATE_LIMITED"
