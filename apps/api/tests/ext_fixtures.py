"""Fixtures for the fork's review-task tests (apps/api/ext).

Imported by name into each test_ext_*.py; pytest collects only conftest.py on
its own, and upstream's conftest is left alone.

Everything runs against the real test database inside one transaction that is
rolled back (`live_db`). Requests go through the *real* auth dependencies with
a real access token, because where a request's viewer gets bound is exactly
what these tests are about; `as_user` in conftest.py replaces that dependency
and would skip it.
"""
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

INTERNAL_NOTE = "INTERNAL-NOTE-do-not-show-the-client"
INTERNAL_REPLY = "INTERNAL-REPLY-do-not-show-the-client"
V2_NOTE = "INTERNAL-ON-V2-do-not-show-the-client"
CLIENT_NOTE = "client feedback, visible to all"


@pytest.fixture
def http(live_db, monkeypatch):
    """`http(user)` → a TestClient signed in as `user` (None = nobody), wired
    the way production is: the session starts as a public viewer and the auth
    dependency binds the user."""
    from fastapi.testclient import TestClient
    from apps.api.database import _bind_public_viewer, get_db
    from apps.api.ext import events, visibility
    from apps.api.main import app
    from apps.api.services import event_service
    from apps.api.services.auth_service import create_access_token

    published = []
    monkeypatch.setattr(
        event_service, "publish_sync",
        lambda project_id, event_type, payload: published.append((str(project_id), event_type, payload)) or True,
    )
    monkeypatch.setattr(events, "dispatch", lambda: None)

    def _get_db():
        _bind_public_viewer(live_db)
        try:
            yield live_db
        finally:
            # A request's session is thrown away; this one is shared with the
            # test, which must read everything when it checks what was stored.
            visibility.unbind(live_db)

    with patch("apps.api.services.s3_service.ensure_bucket_exists"), \
         patch("apps.api.services.s3_service.get_s3_client", return_value=MagicMock()):
        app.dependency_overrides[get_db] = _get_db

        def _client(user=None, token=None):
            client = TestClient(app, raise_server_exceptions=False)
            if token is None and user is not None:
                token = create_access_token(str(user.id))
            if token is not None:
                client.headers["Authorization"] = f"Bearer {token}"
            return client

        _client.published = published
        yield _client
        app.dependency_overrides.clear()


@pytest.fixture
def world(seed, live_db):
    """One private project as Garlomedia runs it: an owner, a freelance editor,
    a client who is a reviewer, a client who is a viewer, an outsider and an
    instance admin; a video with a published v1 and an internal v2; comments on
    both sides of the line; and a share link."""
    from apps.api.models.asset import AssetVersion
    from apps.api.models.comment import Annotation, Comment
    from apps.api.models.project import ProjectRole
    from apps.api.models.share import ShareLink, SharePermission

    owner = seed.user(name="Olga Owner")
    editor = seed.user(name="Eddie Editor")
    reviewer = seed.user(name="Rita Client")
    viewer = seed.user(name="Viktor Client")
    outsider = seed.user(name="Otto Outsider")
    admin = seed.user(superadmin=True, name="Ada Admin")
    project = seed.project(owner, name="Campaign film")
    seed.member(project, editor, ProjectRole.editor)
    seed.member(project, reviewer, ProjectRole.reviewer)
    seed.member(project, viewer, ProjectRole.viewer)

    asset = seed.asset(project, owner, name="hero-cut")
    v1, _ = seed.version(asset, owner, number=1, s3_keys=True)
    v2, _ = seed.version(asset, editor, number=2, s3_keys=True)
    v2.visibility = "internal"

    def comment(version, author, body, visibility="public", parent=None, timecode=None):
        c = Comment(
            asset_id=asset.id, version_id=version.id, author_id=author.id, body=body,
            visibility=visibility, parent_id=parent.id if parent else None,
            timecode_start=timecode,
        )
        live_db.add(c)
        live_db.flush()
        return c

    c_client = comment(v1, reviewer, CLIENT_NOTE, timecode=2.0)
    c_internal = comment(v1, owner, INTERNAL_NOTE, "internal", timecode=4.0)
    c_internal_reply = comment(v1, editor, INTERNAL_REPLY, "internal", parent=c_internal)
    c_v2 = comment(v2, editor, V2_NOTE, "internal")
    live_db.add(Annotation(comment_id=c_internal.id, drawing_data={"marker": "INTERNAL-DRAWING"}))

    link = ShareLink(
        token=f"tok-{uuid.uuid4().hex}", asset_id=asset.id, created_by=owner.id,
        permission=SharePermission.comment, is_enabled=True,
    )
    project_link = ShareLink(
        token=f"tok-{uuid.uuid4().hex}", project_id=project.id, created_by=owner.id,
        permission=SharePermission.comment, is_enabled=True,
    )
    live_db.add_all([link, project_link])
    live_db.flush()

    #: Strings that must never appear in anything the client tier is sent.
    secrets = [
        INTERNAL_NOTE, INTERNAL_REPLY, V2_NOTE, "INTERNAL-DRAWING",
        str(c_internal.id), str(c_internal_reply.id), str(c_v2.id), str(v2.id),
    ]
    return SimpleNamespace(
        owner=owner, editor=editor, reviewer=reviewer, viewer=viewer, outsider=outsider,
        admin=admin, project=project, asset=asset, v1=v1, v2=v2,
        c_client=c_client, c_internal=c_internal, c_internal_reply=c_internal_reply, c_v2=c_v2,
        link=link, project_link=project_link, secrets=secrets, comment=comment,
    )
