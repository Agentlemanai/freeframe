"""Single sign-on endpoints (OpenID Connect). See services/oidc_service.py."""
import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..middleware.rate_limit import rate_limit
from ..models.user import User, UserStatus
from ..services import oidc_service
from ..services.auth_service import create_access_token, create_refresh_token, get_email_owner, get_user_by_email

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/oidc", tags=["auth"])


def _frontend(path: str) -> str:
    return f"{(settings.frontend_url or '').rstrip('/')}{path}"


def _fail(code: str) -> RedirectResponse:
    return RedirectResponse(_frontend("/login?" + urlencode({"sso_error": code})), status_code=302)


@router.get("/config")
def oidc_config():
    """What the login page needs to show (or hide) the SSO button."""
    return {
        "enabled": settings.oidc_configured,
        "button_label": settings.oidc_button_label if settings.oidc_configured else None,
    }


@router.get("/login", dependencies=[Depends(rate_limit("oidc_login", 30, 600))])
def oidc_login(next: str | None = None):
    if not settings.oidc_configured:
        raise HTTPException(status_code=404, detail="Single sign-on is not enabled")
    try:
        url = oidc_service.start_login(oidc_service.safe_next(next))
    except oidc_service.OIDCError as exc:
        logger.warning("oidc login: %s (%s)", exc.code, exc.detail)
        return _fail(exc.code)
    return RedirectResponse(url, status_code=302)


@router.get("/callback", dependencies=[Depends(rate_limit("oidc_callback", 30, 600))])
def oidc_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    if not settings.oidc_configured:
        raise HTTPException(status_code=404, detail="Single sign-on is not enabled")
    try:
        stored = oidc_service.consume_state(state or "")
        if error:
            # The provider refused (user cancelled, or is not allowed the app).
            raise oidc_service.OIDCError("access_denied", f"provider returned error={error}")
        if not code:
            raise oidc_service.OIDCError("invalid_request", "callback without code")
        identity = oidc_service.complete_login(code, stored)
        user = _resolve_user(db, identity)
    except oidc_service.OIDCError as exc:
        logger.warning("oidc callback: %s (%s)", exc.code, exc.detail)
        return _fail(exc.code)

    access = create_access_token(str(user.id), token_version=user.token_version)
    refresh = create_refresh_token(str(user.id), token_version=user.token_version)
    # Tokens travel in the fragment: browsers never send it to a server, so it
    # stays out of access logs and Referer headers. The page stores and clears it.
    fragment = urlencode({"access_token": access, "refresh_token": refresh, "next": stored.get("next") or "/projects"})
    return RedirectResponse(_frontend("/sso-callback#" + fragment), status_code=302)


def _resolve_user(db: Session, identity: oidc_service.OIDCIdentity) -> User:
    user = get_user_by_email(db, identity.email)
    if user is None:
        owner = get_email_owner(db, identity.email)
        if owner is not None:
            # Soft-deleted account, or an ambiguous case-only duplicate.
            raise oidc_service.OIDCError("account_unavailable", f"{identity.email} has no usable account")
        if not settings.oidc_auto_create_users:
            raise oidc_service.OIDCError("no_account", f"{identity.email} has no FreeFrame account")
        may_create = oidc_service.project_creation_policy(identity)
        user = User(
            email=identity.email,
            name=identity.name,
            status=UserStatus.active,
            email_verified=True,
            is_superadmin=oidc_service.is_admin(identity),
            can_create_projects=True if may_create is None else may_create,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info("oidc: created account for %s", identity.email)
        return user

    if user.status == UserStatus.deactivated:
        raise oidc_service.OIDCError("account_deactivated", f"{identity.email} is deactivated")

    # The provider has verified this mailbox, which is what accepting an invite
    # or a magic code would have proven.
    if user.status in (UserStatus.pending_invite, UserStatus.pending_verification):
        user.status = UserStatus.active
        user.invite_token = None
        user.invite_token_expires_at = None
    user.email_verified = True
    if oidc_service.is_admin(identity) and not user.is_superadmin:
        user.is_superadmin = True
        logger.info("oidc: promoted %s to admin (OIDC_ADMIN_GROUPS)", identity.email)
    may_create = oidc_service.project_creation_policy(identity)
    if may_create is not None:
        user.can_create_projects = may_create
    db.commit()
    return user
