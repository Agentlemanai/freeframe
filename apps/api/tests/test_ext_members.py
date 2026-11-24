"""HQ puts people on FreeFrame projects through the gateway (apps/api/ext/router.py,
"Project members from HQ"): by email, making a waiting account when there is none,
never touching an owner, and only with the members:write scope."""
import uuid

import pytest

from apps.api.ext import auth as ext_auth
from apps.api.tests.ext_fixtures import http, world  # noqa: F401 - fixtures


@pytest.fixture
def gateway(http, world, live_db):
    _, token = ext_auth.create_token(live_db, "garlomedia-gateway", world.admin.id)
    live_db.flush()
    return http(token=token)


def _member(live_db, project_id, user_id):
    from apps.api.models.project import ProjectMember
    return live_db.query(ProjectMember).filter(ProjectMember.project_id == project_id, ProjectMember.user_id == user_id).first()


def test_a_new_person_gets_a_waiting_account_and_the_role(gateway, world, live_db):
    from apps.api.models.user import User, UserStatus
    r = gateway.put(f"/ext/projects/{world.project.id}/members", json={"email": "Zoli@Example.hu", "name": "Garai Zoltán", "role": "reviewer"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["account_created"] is True and body["changed"] is True and body["role"] == "reviewer"
    user = live_db.query(User).filter(User.id == uuid.UUID(body["user_id"])).one()
    assert user.email == "zoli@example.hu" and user.name == "Garai Zoltán"
    assert user.status == UserStatus.pending_invite and not user.can_create_projects and not user.is_superadmin
    assert user.invite_token is None, "FreeFrame sends no invite of its own; the first sign-in opens the account"
    assert _member(live_db, world.project.id, user.id).role.value == "reviewer"


def test_an_existing_account_is_found_by_email_and_its_role_changed(gateway, world, live_db):
    r = gateway.put(f"/ext/projects/{world.project.id}/members", json={"email": world.editor.email.upper(), "role": "viewer"})
    assert r.status_code == 200, r.text
    assert r.json()["account_created"] is False and r.json()["changed"] is True
    assert _member(live_db, world.project.id, world.editor.id).role.value == "viewer"
    again = gateway.put(f"/ext/projects/{world.project.id}/members", json={"email": world.editor.email, "role": "viewer"})
    assert again.json()["changed"] is False


def test_an_owner_is_never_changed_or_removed(gateway, world, live_db):
    r = gateway.put(f"/ext/projects/{world.project.id}/members", json={"email": world.owner.email, "role": "viewer"})
    assert r.status_code == 200 and r.json()["role"] == "owner" and r.json()["changed"] is False
    d = gateway.delete(f"/ext/projects/{world.project.id}/members", params={"email": world.owner.email})
    assert d.status_code == 409
    assert _member(live_db, world.project.id, world.owner.id).deleted_at is None


def test_owner_cannot_be_asked_for(gateway, world):
    r = gateway.put(f"/ext/projects/{world.project.id}/members", json={"email": "x@example.hu", "role": "owner"})
    assert r.status_code == 422


def test_removing_and_adding_again(gateway, world, live_db):
    d = gateway.delete(f"/ext/projects/{world.project.id}/members", params={"email": world.editor.email})
    assert d.status_code == 200 and d.json()["changed"] is True
    assert _member(live_db, world.project.id, world.editor.id).deleted_at is not None
    assert gateway.delete(f"/ext/projects/{world.project.id}/members", params={"email": world.editor.email}).json()["changed"] is False
    back = gateway.put(f"/ext/projects/{world.project.id}/members", json={"email": world.editor.email, "role": "editor"})
    assert back.status_code == 200 and back.json()["changed"] is True
    m = _member(live_db, world.project.id, world.editor.id)
    assert m.deleted_at is None and m.role.value == "editor"


def test_a_person_cannot_use_it_and_a_token_needs_the_scope(http, gateway, world, live_db):
    assert http(world.owner).put(f"/ext/projects/{world.project.id}/members", json={"email": "a@b.hu", "role": "viewer"}).status_code == 403
    _, narrow = ext_auth.create_token(live_db, "read-only", world.admin.id, scopes=[ext_auth.SCOPE_READ])
    live_db.flush()
    r = http(token=narrow).put(f"/ext/projects/{world.project.id}/members", json={"email": "a@b.hu", "role": "viewer"})
    assert r.status_code == 403 and "members:write" in r.text


def test_unknown_or_deleted_project(gateway, world):
    r = gateway.put(f"/ext/projects/{uuid.uuid4()}/members", json={"email": "a@b.hu", "role": "viewer"})
    assert r.status_code == 404


def test_a_deactivated_account_is_not_reopened(gateway, world, live_db):
    from apps.api.models.user import UserStatus
    world.outsider.status = UserStatus.deactivated
    live_db.flush()
    r = gateway.put(f"/ext/projects/{world.project.id}/members", json={"email": world.outsider.email, "role": "viewer"})
    assert r.status_code == 409
