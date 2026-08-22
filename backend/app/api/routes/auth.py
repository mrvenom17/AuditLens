"""Authentication routes (04_API_CONTRACT.md → /api/auth/*).

Thin by design (02_ARCHITECTURE.md §7.4): parse, call the service, shape the
response, set or clear the cookie. No credential logic lives here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session as DBSession

from app.api.deps import Actor, CurrentActor, current_session, get_auth_service
from app.config.settings import settings
from app.db.session import get_db
from app.models.user import Session
from app.schemas.auth import CurrentUserResponse, LoginRequest, LoginResponse
from app.schemas.common import ErrorResponse
from app.services.auth import SESSION_COOKIE_NAME, AuthService

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _set_session_cookie(response: Response, token: str) -> None:
    """05_SECURITY.md §10.2/§10.9: httpOnly, Secure, SameSite=Strict.

    `secure` is conditional on the environment for one reason only: a browser
    refuses a Secure cookie over plain http, which would make local development
    impossible. Production always runs behind Cloudflare Tunnel over HTTPS
    (09_DEPLOYMENT.md), so the flag is always set there.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=settings.is_production,
        samesite="strict",
        # No `max_age`: a session cookie dies with the browser session, and the
        # server-side row is the real expiry authority regardless.
        path="/",
    )


@router.post(
    "/login",
    response_model=LoginResponse,
    responses={
        401: {"model": ErrorResponse, "description": "INVALID_CREDENTIALS"},
        429: {"model": ErrorResponse, "description": "TOO_MANY_ATTEMPTS"},
    },
)
def login(
    payload: LoginRequest,
    response: Response,
    db: Annotated[DBSession, Depends(get_db)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> LoginResponse:
    user, token = auth.login(payload.email, payload.password)
    # The service flushes; the commit is the route's, so a failure anywhere in
    # the request leaves no half-created session behind.
    db.commit()
    _set_session_cookie(response, token)
    return LoginResponse(user_id=user.id, role=user.role, name=user.name)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    db: Annotated[DBSession, Depends(get_db)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
    session: Annotated[Session, Depends(current_session)],
) -> Response:
    """Revoke server-side, then clear the cookie.

    Clearing the cookie alone would leave a stolen token valid for the rest of
    its lifetime, so revocation is the operation and the cookie is housekeeping.
    """
    auth.logout(session)
    db.commit()
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=settings.is_production,
        samesite="strict",
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=CurrentUserResponse)
def me(actor: CurrentActor) -> CurrentUserResponse:
    """What the frontend needs to render a role-appropriate view.

    Never an authorization source: every protected endpoint re-derives the role
    server-side regardless of what the client learned here.
    """
    return CurrentUserResponse(
        user_id=actor.id, email=actor.email, name=actor.name, role=actor.role
    )


__all__ = ["Actor", "router"]
