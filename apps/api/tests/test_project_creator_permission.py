"""Who may create projects (users.can_create_projects), and how SSO sets it."""
import uuid
from datetime import datetime, timezone

from apps.api.models.project import ProjectType


def _refresh(test_user):
    def _side_effect(obj):
        obj.id = uuid.uuid4()
        obj.created_at = datetime.now(timezone.utc)
        obj.deleted_at = None
        obj.description = None
        obj.project_type = ProjectType.personal
        obj.is_public = False
        obj.poster_url = None
        obj.created_by = test_user.id
        obj.name = "P"
    return _side_effect


def test_user_without_permission_cannot_create_project(client, auth_headers, mock_db, test_user):
    test_user.can_create_projects = False
    resp = client.post("/projects", json={"name": "P"}, headers=auth_headers)
    assert resp.status_code == 403
    mock_db.add.assert_not_called()


def test_user_with_permission_can_create_project(client, auth_headers, mock_db, test_user):
    test_user.can_create_projects = True
    mock_db.refresh.side_effect = _refresh(test_user)
    resp = client.post("/projects", json={"name": "P"}, headers=auth_headers)
    assert resp.status_code == 201


def test_admin_can_always_create_project(client, auth_headers, mock_db, test_user):
    test_user.is_superadmin = True
    test_user.can_create_projects = False
    mock_db.refresh.side_effect = _refresh(test_user)
    resp = client.post("/projects", json={"name": "P"}, headers=auth_headers)
    assert resp.status_code == 201


def test_me_reports_the_permission(client, auth_headers, test_user):
    test_user.can_create_projects = False
    assert client.get("/auth/me", headers=auth_headers).json()["can_create_projects"] is False


# --- SSO keeps the permission in step with OIDC_PROJECT_CREATOR_GROUPS --------

from apps.api.services.oidc_service import OIDCIdentity, project_creation_policy  # noqa: E402


def _identity(groups):
    return OIDCIdentity(email="a@example.com", name="A", groups=groups, subject="s")


def test_policy_is_none_when_not_configured(monkeypatch):
    from apps.api.config import settings
    monkeypatch.setattr(settings, "oidc_project_creator_groups", "")
    assert project_creation_policy(_identity(["clients"])) is None


def test_policy_follows_groups_and_admins(monkeypatch):
    from apps.api.config import settings
    monkeypatch.setattr(settings, "oidc_project_creator_groups", "staff, freelancers")
    monkeypatch.setattr(settings, "oidc_admin_groups", "admins")
    assert project_creation_policy(_identity(["freelancers"])) is True
    assert project_creation_policy(_identity(["client-acme", "clients"])) is False
    assert project_creation_policy(_identity(["admins"])) is True
