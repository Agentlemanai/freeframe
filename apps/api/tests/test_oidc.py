"""OpenID Connect single sign-on.

The provider is faked end to end: discovery and the token endpoint are patched,
and the id_token is a real RS256 JWT signed with a throwaway key, so signature,
issuer, audience, expiry and nonce checks all run for real.
"""
import json
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from apps.api.models.user import UserStatus

ISSUER = "https://idp.example.com/application/o/freeframe/"
CLIENT_ID = "freeframe-client"
DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": "https://idp.example.com/application/o/authorize/",
    "token_endpoint": "https://idp.example.com/application/o/token/",
    "jwks_uri": "https://idp.example.com/application/o/freeframe/jwks/",
    "userinfo_endpoint": "https://idp.example.com/application/o/userinfo/",
}
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeRedis:
    def __init__(self):
        self.store = {}

    def setex(self, key, ttl, value):
        self.store[key] = value

    def pipeline(self):
        outer = self

        class _P:
            def __init__(self):
                self.ops = []

            def get(self, key):
                self.ops.append(("get", key))

            def delete(self, key):
                self.ops.append(("delete", key))

            def execute(self):
                out = []
                for op, key in self.ops:
                    out.append(outer.store.get(key) if op == "get" else outer.store.pop(key, None))
                return out

        return _P()


@pytest.fixture
def oidc_on(monkeypatch):
    from apps.api.config import settings
    from apps.api.services import oidc_service

    for name, value in {
        "oidc_enabled": True,
        "oidc_issuer": ISSUER,
        "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": "s3cret",
        "oidc_redirect_uri": "",
        "oidc_auto_create_users": False,
        "oidc_allowed_groups": "",
        "oidc_admin_groups": "",
        "oidc_groups_claim": "groups",
        "frontend_url": "https://review.example.com",
    }.items():
        monkeypatch.setattr(settings, name, value)
    oidc_service.reset_caches()
    redis = FakeRedis()
    monkeypatch.setattr(oidc_service, "get_redis", lambda: redis)
    monkeypatch.setattr(oidc_service, "discovery", lambda: DISCOVERY)
    monkeypatch.setattr("apps.api.middleware.rate_limit.check_rate_limit", lambda *a, **k: (True, 0))
    signing = MagicMock()
    signing.key = _KEY.public_key()
    jwks = MagicMock()
    jwks.get_signing_key_from_jwt.return_value = signing
    monkeypatch.setattr(oidc_service.jwt, "PyJWKClient", lambda *a, **k: jwks)
    return redis


def _id_token(nonce, **overrides):
    now = int(time.time())
    claims = {
        "iss": ISSUER, "aud": CLIENT_ID, "sub": "user-123", "iat": now, "exp": now + 300,
        "nonce": nonce, "email": "kitti@example.com", "email_verified": True,
        "name": "Prager Kitti", "groups": ["clients", "client-pragerkitti"],
    }
    claims.update(overrides)
    return jwt.encode(claims, _KEY, algorithm="RS256")


def _start(client, redis, next_path="/projects/abc"):
    resp = client.get("/auth/oidc/login", params={"next": next_path}, follow_redirects=False)
    assert resp.status_code == 302
    query = parse_qs(urlparse(resp.headers["location"]).query)
    state = query["state"][0]
    stored = json.loads(redis.store["oidc:state:" + state])
    return state, stored, query


def _callback(client, state, token_response):
    post = MagicMock()
    post.return_value.json.return_value = token_response
    post.return_value.raise_for_status.return_value = None
    with patch("apps.api.services.oidc_service.httpx.post", post):
        resp = client.get("/auth/oidc/callback", params={"code": "abc", "state": state}, follow_redirects=False)
    return resp, post


def _user(**kw):
    u = MagicMock()
    u.id = uuid.uuid4()
    u.email = kw.get("email", "kitti@example.com")
    u.status = kw.get("status", UserStatus.active)
    u.is_superadmin = kw.get("is_superadmin", False)
    u.token_version = 1
    u.email_verified = False
    u.created_at = datetime.now(timezone.utc)
    return u


def test_config_reports_disabled_by_default(client):
    data = client.get("/auth/oidc/config").json()
    assert data == {"enabled": False, "button_label": None}


def test_login_404_when_disabled(client):
    assert client.get("/auth/oidc/login", follow_redirects=False).status_code == 404


def test_config_enabled(client, oidc_on):
    data = client.get("/auth/oidc/config").json()
    assert data["enabled"] is True and data["button_label"]


def test_login_redirects_with_pkce_and_stores_state(client, oidc_on):
    state, stored, query = _start(client, oidc_on)
    assert query["client_id"] == [CLIENT_ID]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["https://review.example.com/api/auth/oidc/callback"]
    assert query["nonce"][0] == stored["nonce"]
    assert stored["next"] == "/projects/abc"


def test_login_rejects_offsite_next(client, oidc_on):
    _, stored, _ = _start(client, oidc_on, next_path="//evil.example.com/x")
    assert stored["next"] == "/projects"


def test_callback_signs_in_existing_user(client, oidc_on):
    state, stored, _ = _start(client, oidc_on)
    user = _user()
    with patch("apps.api.routers.oidc.get_user_by_email", return_value=user):
        resp, post = _callback(client, state, {"id_token": _id_token(stored["nonce"]), "access_token": "at"})
    assert resp.status_code == 302
    loc = urlparse(resp.headers["location"])
    assert loc.path == "/sso-callback"
    frag = parse_qs(loc.fragment)
    assert frag["next"] == ["/projects/abc"]
    payload = jwt.decode(frag["access_token"][0], options={"verify_signature": False})
    assert payload["sub"] == str(user.id)
    assert user.email_verified is True
    assert post.call_args.kwargs["data"]["code_verifier"] == stored["verifier"]


def test_state_is_single_use(client, oidc_on):
    state, stored, _ = _start(client, oidc_on)
    with patch("apps.api.routers.oidc.get_user_by_email", return_value=_user()):
        _callback(client, state, {"id_token": _id_token(stored["nonce"])})
        resp, _ = _callback(client, state, {"id_token": _id_token(stored["nonce"])})
    assert "sso_error=invalid_state" in resp.headers["location"]


def test_nonce_mismatch_is_rejected(client, oidc_on):
    state, _, _ = _start(client, oidc_on)
    with patch("apps.api.routers.oidc.get_user_by_email", return_value=_user()):
        resp, _ = _callback(client, state, {"id_token": _id_token("not-the-nonce")})
    assert "sso_error=invalid_token" in resp.headers["location"]


def test_wrong_audience_is_rejected(client, oidc_on):
    state, stored, _ = _start(client, oidc_on)
    with patch("apps.api.routers.oidc.get_user_by_email", return_value=_user()):
        resp, _ = _callback(client, state, {"id_token": _id_token(stored["nonce"], aud="someone-else")})
    assert "sso_error=invalid_token" in resp.headers["location"]


def test_unknown_user_without_auto_create(client, oidc_on, mock_db):
    state, stored, _ = _start(client, oidc_on)
    with patch("apps.api.routers.oidc.get_user_by_email", return_value=None), \
         patch("apps.api.routers.oidc.get_email_owner", return_value=None):
        resp, _ = _callback(client, state, {"id_token": _id_token(stored["nonce"])})
    assert "sso_error=no_account" in resp.headers["location"]
    mock_db.add.assert_not_called()


def test_unknown_user_is_created_when_auto_create_on(client, oidc_on, mock_db, monkeypatch):
    from apps.api.config import settings
    monkeypatch.setattr(settings, "oidc_auto_create_users", True)
    monkeypatch.setattr(settings, "oidc_admin_groups", "staff")
    state, stored, _ = _start(client, oidc_on)

    def _refresh(obj):
        obj.id = uuid.uuid4()
        obj.token_version = 1
    mock_db.refresh.side_effect = _refresh
    with patch("apps.api.routers.oidc.get_user_by_email", return_value=None), \
         patch("apps.api.routers.oidc.get_email_owner", return_value=None):
        resp, _ = _callback(client, state, {"id_token": _id_token(stored["nonce"])})
    assert urlparse(resp.headers["location"]).path == "/sso-callback"
    created = mock_db.add.call_args.args[0]
    assert created.email == "kitti@example.com" and created.name == "Prager Kitti"
    assert created.status == UserStatus.active and created.email_verified is True
    assert created.is_superadmin is False  # not in "staff"


def test_soft_deleted_email_is_not_recreated(client, oidc_on, mock_db, monkeypatch):
    from apps.api.config import settings
    monkeypatch.setattr(settings, "oidc_auto_create_users", True)
    state, stored, _ = _start(client, oidc_on)
    with patch("apps.api.routers.oidc.get_user_by_email", return_value=None), \
         patch("apps.api.routers.oidc.get_email_owner", return_value=_user()):
        resp, _ = _callback(client, state, {"id_token": _id_token(stored["nonce"])})
    assert "sso_error=account_unavailable" in resp.headers["location"]
    mock_db.add.assert_not_called()


def test_deactivated_user_is_refused(client, oidc_on):
    state, stored, _ = _start(client, oidc_on)
    with patch("apps.api.routers.oidc.get_user_by_email", return_value=_user(status=UserStatus.deactivated)):
        resp, _ = _callback(client, state, {"id_token": _id_token(stored["nonce"])})
    assert "sso_error=account_deactivated" in resp.headers["location"]


def test_pending_invite_is_activated(client, oidc_on):
    state, stored, _ = _start(client, oidc_on)
    user = _user(status=UserStatus.pending_invite)
    with patch("apps.api.routers.oidc.get_user_by_email", return_value=user):
        _callback(client, state, {"id_token": _id_token(stored["nonce"])})
    assert user.status == UserStatus.active and user.invite_token is None


def test_allowed_groups_are_enforced(client, oidc_on, monkeypatch):
    from apps.api.config import settings
    monkeypatch.setattr(settings, "oidc_allowed_groups", "staff,freelancers")
    state, stored, _ = _start(client, oidc_on)
    with patch("apps.api.routers.oidc.get_user_by_email", return_value=_user()):
        resp, _ = _callback(client, state, {"id_token": _id_token(stored["nonce"])})
    assert "sso_error=not_allowed" in resp.headers["location"]


def test_admin_group_promotes_but_never_demotes(client, oidc_on, monkeypatch):
    from apps.api.config import settings
    monkeypatch.setattr(settings, "oidc_admin_groups", "staff")
    state, stored, _ = _start(client, oidc_on)
    user = _user()
    with patch("apps.api.routers.oidc.get_user_by_email", return_value=user):
        _callback(client, state, {"id_token": _id_token(stored["nonce"], groups=["staff"])})
    assert user.is_superadmin is True

    state, stored, _ = _start(client, oidc_on)
    admin = _user(is_superadmin=True)
    with patch("apps.api.routers.oidc.get_user_by_email", return_value=admin):
        _callback(client, state, {"id_token": _id_token(stored["nonce"], groups=["clients"])})
    assert admin.is_superadmin is True


def test_unverified_email_is_refused(client, oidc_on):
    state, stored, _ = _start(client, oidc_on)
    with patch("apps.api.routers.oidc.get_user_by_email", return_value=_user()):
        resp, _ = _callback(client, state, {"id_token": _id_token(stored["nonce"], email_verified=False)})
    assert "sso_error=email_not_verified" in resp.headers["location"]


def test_provider_error_is_passed_on(client, oidc_on):
    state, _, _ = _start(client, oidc_on)
    resp = client.get("/auth/oidc/callback", params={"state": state, "error": "access_denied"}, follow_redirects=False)
    assert "sso_error=access_denied" in resp.headers["location"]


def test_sso_sign_in_sets_project_permission_from_groups(client, oidc_on, monkeypatch):
    from apps.api.config import settings
    monkeypatch.setattr(settings, "oidc_project_creator_groups", "staff,freelancers")
    state, stored, _ = _start(client, oidc_on)
    client_user = _user()
    client_user.can_create_projects = True
    with patch("apps.api.routers.oidc.get_user_by_email", return_value=client_user):
        _callback(client, state, {"id_token": _id_token(stored["nonce"], groups=["clients", "client-pragerkitti"])})
    assert client_user.can_create_projects is False

    state, stored, _ = _start(client, oidc_on)
    crew = _user()
    crew.can_create_projects = False
    with patch("apps.api.routers.oidc.get_user_by_email", return_value=crew):
        _callback(client, state, {"id_token": _id_token(stored["nonce"], groups=["freelancers"])})
    assert crew.can_create_projects is True


def test_new_sso_account_gets_project_permission_from_groups(client, oidc_on, mock_db, monkeypatch):
    from apps.api.config import settings
    monkeypatch.setattr(settings, "oidc_auto_create_users", True)
    monkeypatch.setattr(settings, "oidc_project_creator_groups", "staff")
    state, stored, _ = _start(client, oidc_on)

    def _refresh(obj):
        obj.id = uuid.uuid4()
        obj.token_version = 1
    mock_db.refresh.side_effect = _refresh
    with patch("apps.api.routers.oidc.get_user_by_email", return_value=None), \
         patch("apps.api.routers.oidc.get_email_owner", return_value=None):
        _callback(client, state, {"id_token": _id_token(stored["nonce"])})
    assert mock_db.add.call_args.args[0].can_create_projects is False
