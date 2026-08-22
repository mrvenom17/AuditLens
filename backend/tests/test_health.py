"""TASK-002 smoke test: the app boots and /health responds."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_200() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_security_headers_present_on_every_response() -> None:
    """05_SECURITY.md §10.9 — set at the response layer, so an unauthenticated
    endpoint carries them too."""
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Request-ID"]


def test_unknown_route_uses_the_standard_error_envelope() -> None:
    """02_ARCHITECTURE.md §7.7 — one shape for every error, including 404s
    raised by the framework rather than by application code."""
    with TestClient(app) as client:
        response = client.get("/no-such-route")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["request_id"]
