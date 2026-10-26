"""OpenID Connect single sign-on (authorization code flow with PKCE).

The browser is sent to the provider by GET /auth/oidc/login and comes back to
GET /auth/oidc/callback. Everything the callback needs to trust the answer
(state, nonce, PKCE verifier, where to go afterwards) is kept server-side in
Redis under a random, single-use state key, so nothing about the flow lives in
a cookie and a replayed or forged callback finds no state.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import jwt

from ..config import settings
from .redis_service import get_redis

STATE_TTL_SECONDS = 600
_STATE_PREFIX = "oidc:state:"
_DISCOVERY_TTL_SECONDS = 3600
_HTTP_TIMEOUT = 10.0
# Asymmetric algorithms only: an id_token signed with HS256 would be keyed by
# the client secret, which this flow never uses for verification.
_ALLOWED_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"]

_discovery_cache: dict[str, tuple[float, dict]] = {}
_jwks_clients: dict[str, jwt.PyJWKClient] = {}


class OIDCError(Exception):
    """A sign-in that must not go through. `code` is shown to the user's browser
    as ?sso_error=<code>; details stay in the server log."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass
class OIDCIdentity:
    email: str
    name: str
    groups: list[str]
    subject: str


def _csv(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def discovery() -> dict:
    issuer = settings.oidc_issuer.strip()
    cached = _discovery_cache.get(issuer)
    if cached and time.monotonic() - cached[0] < _DISCOVERY_TTL_SECONDS:
        return cached[1]
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        resp = httpx.get(url, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        doc = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OIDCError("provider_unavailable", f"discovery failed for {url}: {exc}") from exc
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer"):
        if not doc.get(key):
            raise OIDCError("provider_unavailable", f"discovery document has no {key}")
    _discovery_cache[issuer] = (time.monotonic(), doc)
    return doc


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def start_login(next_path: str) -> str:
    """Store a fresh state and return the provider's authorization URL."""
    doc = discovery()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    get_redis().setex(
        _STATE_PREFIX + state,
        STATE_TTL_SECONDS,
        json.dumps({"nonce": nonce, "verifier": verifier, "next": next_path}),
    )
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": settings.oidc_callback_url,
        "scope": settings.oidc_scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return str(httpx.URL(doc["authorization_endpoint"], params=params))


def consume_state(state: str) -> dict:
    """Return and delete the stored state. Each state works exactly once."""
    if not state:
        raise OIDCError("invalid_state", "callback without state")
    r = get_redis()
    key = _STATE_PREFIX + state
    pipe = r.pipeline()
    pipe.get(key)
    pipe.delete(key)
    raw, _ = pipe.execute()
    if not raw:
        raise OIDCError("invalid_state", "unknown or expired state")
    return json.loads(raw)


def _verify_id_token(id_token: str, nonce: str, doc: dict) -> dict:
    jwks_uri = doc["jwks_uri"]
    client = _jwks_clients.get(jwks_uri)
    if client is None:
        client = jwt.PyJWKClient(jwks_uri, timeout=int(_HTTP_TIMEOUT))
        _jwks_clients[jwks_uri] = client
    try:
        key = client.get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(
            id_token,
            key,
            algorithms=_ALLOWED_ALGORITHMS,
            audience=settings.oidc_client_id,
            issuer=doc["issuer"],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise OIDCError("invalid_token", f"id_token rejected: {exc}") from exc
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OIDCError("invalid_token", "nonce mismatch")
    return claims


def complete_login(code: str, stored: dict) -> OIDCIdentity:
    """Exchange the code, verify the id_token and return who signed in."""
    doc = discovery()
    try:
        resp = httpx.post(
            doc["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.oidc_callback_url,
                "client_id": settings.oidc_client_id,
                "client_secret": settings.oidc_client_secret,
                "code_verifier": stored["verifier"],
            },
            headers={"Accept": "application/json"},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        tokens = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OIDCError("token_exchange_failed", f"token endpoint: {exc}") from exc

    id_token = tokens.get("id_token")
    if not id_token:
        raise OIDCError("invalid_token", "no id_token in token response")
    claims = _verify_id_token(id_token, stored["nonce"], doc)

    # Some providers keep email/groups out of the id_token; ask userinfo then.
    claim_name = settings.oidc_groups_claim
    if (not claims.get("email") or claim_name not in claims) and doc.get("userinfo_endpoint") and tokens.get("access_token"):
        try:
            info = httpx.get(
                doc["userinfo_endpoint"],
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
                timeout=_HTTP_TIMEOUT,
            )
            info.raise_for_status()
            userinfo = info.json()
            if userinfo.get("sub") == claims.get("sub"):
                claims = {**userinfo, **claims}
        except (httpx.HTTPError, ValueError):
            pass

    email = (claims.get("email") or "").strip()
    if not email:
        raise OIDCError("no_email", "provider sent no email claim")
    if claims.get("email_verified") is False:
        raise OIDCError("email_not_verified", f"{email} is not verified at the provider")

    raw_groups: Any = claims.get(claim_name) or []
    if isinstance(raw_groups, str):
        raw_groups = [raw_groups]
    groups = [str(g) for g in raw_groups]

    allowed = _csv(settings.oidc_allowed_groups)
    if allowed and not set(allowed) & set(groups):
        raise OIDCError("not_allowed", f"{email} is in none of OIDC_ALLOWED_GROUPS")

    name = (claims.get("name") or claims.get("preferred_username") or email.split("@")[0]).strip()
    return OIDCIdentity(email=email, name=name[:255], groups=groups, subject=str(claims["sub"]))


def is_admin(identity: OIDCIdentity) -> bool:
    admins = _csv(settings.oidc_admin_groups)
    return bool(admins) and bool(set(admins) & set(identity.groups))


def reset_caches() -> None:
    """For tests and after a configuration change."""
    _discovery_cache.clear()
    _jwks_clients.clear()


def safe_next(path: Optional[str]) -> str:
    """Only same-site relative paths survive the round trip; anything else
    (absolute URLs, protocol-relative //host, backslashes) becomes /projects."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/projects"
    return path
