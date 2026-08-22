"""FastAPI application entrypoint.

Wires the middleware stack, the standardized error envelope (02_ARCHITECTURE.md
§7.7), and the security headers required by 05_SECURITY.md §10.9. Route modules
are registered here and nowhere else.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config.settings import settings
from app.errors import AppError, TooManyAttemptsError
from app.logging_setup import configure_logging, get_request_id, new_request_id, set_request_id

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.ENVIRONMENT)
    # Fail fast rather than serving traffic with a development secret.
    settings.validate_for_environment()
    logger.info("AuditLens starting environment=%s", settings.ENVIRONMENT)
    yield
    logger.info("AuditLens shutting down")


app = FastAPI(
    title="AuditLens API",
    version="0.1.0",
    description="PCI DSS v4.0.1 audit assistant — internal, single-tenant.",
    lifespan=lifespan,
    # OpenAPI docs are an internal-tooling convenience, not a public surface.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
)

# 05_SECURITY.md §10.9: restricted to the app's own frontend origin, no wildcard.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.CORS_ALLOWED_ORIGIN],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def request_context_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Assigns a request id and sets the security headers on every response."""
    set_request_id(new_request_id())
    response = await call_next(request)
    response.headers["X-Request-ID"] = get_request_id()
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    # The API serves JSON and file attachments only — it never renders HTML, so
    # the strictest possible policy is also the correct one here.
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )
    if settings.is_production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


def _envelope(code: str, message: str, status_code: int, **details: object) -> JSONResponse:
    """The single error shape every endpoint returns (02_ARCHITECTURE.md §7.7)."""
    error: dict[str, object] = {
        "code": code,
        "message": message,
        "request_id": get_request_id(),
        **details,
    }
    return JSONResponse(status_code=status_code, content={"error": error})


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    response = _envelope(exc.code, exc.message, exc.status_code, **exc.details)
    if isinstance(exc, TooManyAttemptsError):
        response.headers["Retry-After"] = str(exc.retry_after_seconds)
    return response


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Field-level detail, as 04_API_CONTRACT.md requires for 400 VALIDATION_ERROR.

    Pydantic's raw errors can echo the submitted value back, which for this API
    could mean a password or evidence text. Only the field location and the rule
    that failed are surfaced.
    """
    fields = [
        {"field": ".".join(str(p) for p in e["loc"][1:]) or "body", "reason": e["msg"]}
        for e in exc.errors()
    ]
    return _envelope("VALIDATION_ERROR", "Request validation failed.", 400, fields=fields)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    codes = {
        401: "NOT_AUTHENTICATED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
    }
    code = codes.get(exc.status_code, "HTTP_ERROR")
    return _envelope(code, str(exc.detail), exc.status_code)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """The client learns nothing beyond a reference id (02_ARCHITECTURE.md §7.7)."""
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return _envelope(
        "INTERNAL_ERROR",
        f"Something went wrong. Reference ID: {get_request_id()}",
        500,
    )


from app.api.routes import auth, health  # noqa: E402  (registered after handlers exist)

app.include_router(health.router)
app.include_router(auth.router)
