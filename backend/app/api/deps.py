"""Request dependencies: authentication and role gates.

02_ARCHITECTURE.md §7.4 forbids inline role checks scattered across routes.
Every route declares its requirement by depending on one of the gates here, so
the set of endpoints and the set of authorization rules cannot drift apart —
an endpoint with no gate has no `Actor` to pass to a repository, and every
engagement-scoped repository method requires one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session as DBSession

from app.db.session import get_db
from app.errors import AuthenticationError, ForbiddenError
from app.logging_setup import log_authz_denial
from app.models.enums import Role
from app.models.user import Session, User
from app.services.auth import SESSION_COOKIE_NAME, AuthService


@dataclass(frozen=True)
class Actor:
    """The authenticated caller, derived server-side on every request.

    This is the *only* accepted source of identity, role and ownership for an
    authorization decision (05_SECURITY.md §10.3). Nothing in a request body or
    header contributes to it.
    """

    id: uuid.UUID
    role: Role
    name: str
    email: str

    @property
    def is_reviewer(self) -> bool:
        return self.role == Role.reviewer

    @property
    def is_admin(self) -> bool:
        return self.role == Role.admin

    @property
    def sees_all_engagements(self) -> bool:
        """03_DATA_MODEL.md §8.2: Reviewers see all engagements; Admins see all
        for support purposes, with their access logged distinctly."""
        return self.role in (Role.reviewer, Role.admin)


def get_auth_service(db: Annotated[DBSession, Depends(get_db)]) -> AuthService:
    return AuthService(db)


def _authenticate(request: Request, auth: AuthService) -> tuple[User, Session]:
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    resolved = auth.resolve_session(token)
    if resolved is None:
        raise AuthenticationError("Authentication required.")
    return resolved


def current_actor(
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> Actor:
    """Require a valid session. The base dependency for every protected route."""
    user, _ = _authenticate(request, auth)
    return Actor(id=user.id, role=user.role, name=user.name, email=user.email)


def current_session(
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> Session:
    """The session row itself — needed only by logout."""
    _, session = _authenticate(request, auth)
    return session


CurrentActor = Annotated[Actor, Depends(current_actor)]


def require_roles(*roles: Role):  # type: ignore[no-untyped-def]
    """Build a dependency that admits only the given roles.

    Denials are logged (02_ARCHITECTURE.md §7.8) because a spike in 403s is one
    of the two alert conditions 09_DEPLOYMENT.md asks for — it is how an
    authorization bug or a boundary probe becomes visible.
    """
    allowed = set(roles)

    def _gate(request: Request, actor: CurrentActor) -> Actor:
        if actor.role not in allowed:
            log_authz_denial(
                actor_id=str(actor.id),
                action=f"{request.method} {request.url.path}",
                resource_type="role_gate",
                resource_id="|".join(sorted(r.value for r in allowed)),
            )
            raise ForbiddenError("Your role does not permit this action.")
        return actor

    return _gate


# Named gates, so a route reads as its rule rather than as a list of roles.
RequireAuditorOrReviewer = Annotated[Actor, Depends(require_roles(Role.auditor, Role.reviewer))]
# 04_API_CONTRACT.md → POST /api/engagements/{id}/finalize: Reviewer only. Admin
# is deliberately excluded — 00_PRODUCT.md §5.3 states sign-off authority is a
# role property, not an escalation path, so an Admin cannot finalize by virtue
# of being an Admin.
RequireReviewer = Annotated[Actor, Depends(require_roles(Role.reviewer))]
RequireAdmin = Annotated[Actor, Depends(require_roles(Role.admin))]
