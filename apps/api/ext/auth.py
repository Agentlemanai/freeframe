"""Who is calling an /ext route: a signed-in user, or the gateway.

The gateway authenticates with a token (`ffgw_…`) instead of a user's JWT. Only
the token's SHA-256 is stored. A gateway token reads internal data on every
project, which is its job, and is limited in what it may *change* by its
scopes. No scope lets it approve, reject, cancel, publish, change a comment's
visibility or manage users: those routes do not accept a gateway actor at all.
`members:write` puts people on projects (HQ's project team is the source); it
cannot make or remove an owner, or change an admin.
"""
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from ..database import get_db
from ..middleware.auth import bearer_scheme, get_current_user
from ..models.user import User
from . import visibility
from .models import GatewayToken

TOKEN_PREFIX = "ffgw_"

SCOPE_READ = "read"                    # tasks, notes, events, version metadata, on every project
SCOPE_VERSIONS = "versions:write"      # upload a version and write its metadata
SCOPE_NOTES = "notes:write"            # post task notes
SCOPE_TASK_STATUS = "tasks:status"     # claimed, in_progress, proposed, needs_info, back to open
SCOPE_MEMBERS = "members:write"        # put people on a project (editor, reviewer, viewer) and take them off; never owners
ALL_SCOPES = (SCOPE_READ, SCOPE_VERSIONS, SCOPE_NOTES, SCOPE_TASK_STATUS, SCOPE_MEMBERS)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_token(db: Session, name: str, created_by, scopes=ALL_SCOPES) -> tuple[GatewayToken, str]:
    """Add a token row to the session and return it with the token itself,
    which exists nowhere else after this returns."""
    unknown = [s for s in scopes if s not in ALL_SCOPES]
    if unknown:
        raise ValueError(f"unknown scope(s): {', '.join(unknown)}")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = GatewayToken(name=name, token_hash=hash_token(token), scopes=list(scopes), created_by=created_by)
    db.add(row)
    return row, token


@dataclass
class Actor:
    user: Optional[User] = None
    gateway: Optional[GatewayToken] = None

    @property
    def is_gateway(self) -> bool:
        return self.gateway is not None

    def has_scope(self, scope: str) -> bool:
        return self.gateway is not None and scope in (self.gateway.scopes or [])

    @property
    def label(self) -> str:
        if self.gateway is not None:
            return f"gateway:{self.gateway.name}"
        return self.user.name if self.user is not None else "unknown"


def get_actor(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> Actor:
    token = credentials.credentials
    if not token.startswith(TOKEN_PREFIX):
        return Actor(user=get_current_user(credentials, db))
    row = db.query(GatewayToken).filter(
        GatewayToken.token_hash == hash_token(token),
        GatewayToken.revoked_at.is_(None),
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked gateway token")
    now = datetime.now(timezone.utc)
    # One write a minute at most, so the token's own bookkeeping does not turn
    # every read into a write.
    if row.last_used_at is None or now - row.last_used_at > timedelta(minutes=1):
        row.last_used_at = now
        db.commit()
    visibility.bind_all(db)
    return Actor(gateway=row)


def require_user(actor: Actor) -> User:
    """For routes the gateway may never call, whatever its scopes."""
    if actor.user is None:
        raise HTTPException(status_code=403, detail="This action needs a signed-in person, not the gateway")
    return actor.user


def require_scope(actor: Actor, scope: str) -> None:
    if actor.is_gateway and not actor.has_scope(scope):
        raise HTTPException(status_code=403, detail=f"The gateway token lacks the '{scope}' scope")
