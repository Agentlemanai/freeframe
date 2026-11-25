"""Every comment is a task (apps/api/ext/comment_tasks.py): no step to turn one
into the other. World (ext_fixtures.world): published v1, internal v2.
"""
import pytest

from apps.api.ext.models import ExtEvent, ReviewTask, ReviewTaskNote, VersionExt, VersionResolvedTask
from apps.api.models.comment import Comment
from apps.api.tests.ext_fixtures import http, world  # noqa: F401 - fixtures


@pytest.fixture
def fps(world, live_db):
    for v in (world.v1, world.v2):
        live_db.add(VersionExt(version_id=v.id, fps=25.0))
    live_db.flush()


def _post(http, world, user, body, version=None, **extra):
    r = http(user).post(f"/assets/{world.asset.id}/comments",
                        json={"version_id": str((version or world.v1).id), "body": body, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _task_of(live_db, comment_id):
    return live_db.query(ReviewTask).filter(ReviewTask.source_comment_id == comment_id).one_or_none()


def _types(live_db, since):
    return [e.type for e in live_db.query(ExtEvent).filter(ExtEvent.seq > since).order_by(ExtEvent.seq)]


def _last_seq(live_db):
    e = live_db.query(ExtEvent).order_by(ExtEvent.seq.desc()).first()
    return e.seq if e else 0


def test_a_clients_comment_is_a_task_for_the_editors(http, world, live_db, fps):
    since = _last_seq(live_db)
    c = _post(http, world, world.reviewer, "The logo is too small", timecode_start=2.0, timecode_end=3.0)
    task = _task_of(live_db, c["id"])
    assert task is not None, "no 'Make task' step: the comment is the task"
    assert (task.kind, task.stage, task.assignee_type, task.assignee_role, task.status) == \
        ("comment", "review", "role_pool", "editor", "open")
    assert (task.instruction, task.frame_in, task.frame_out) == ("The logo is too small", 50, 75)
    assert (task.created_on_version_id, task.created_by) == (world.v1.id, world.reviewer.id)
    assert _types(live_db, since) == ["comment.created", "task.created"], "one transaction, the comment first"


def test_the_teams_internal_comment_is_a_task_too(http, world, live_db):
    c = _post(http, world, world.editor, "Music too loud", version=world.v2)
    task = _task_of(live_db, c["id"])
    assert task is not None and task.created_on_version_id == world.v2.id
    assert task.frame_in is None, "no timecode, no frames"


def test_a_reply_is_not_a_task(http, world, live_db):
    c = _post(http, world, world.reviewer, "Change the font")
    before = live_db.query(ReviewTask).count()
    r = http(world.editor).post(f"/assets/{world.asset.id}/comments/{c['id']}/replies",
                                json={"version_id": str(world.v1.id), "body": "On it"})
    assert r.status_code == 201, r.text
    assert live_db.query(ReviewTask).count() == before


def test_a_guest_comment_through_a_share_link_is_a_task(http, world, live_db):
    r = http().post(f"/share/{world.link.token}/comment",
                    json={"body": "Brighter please", "guest_email": "guest@example.com", "guest_name": "Guest"})
    assert r.status_code == 201, r.text
    task = _task_of(live_db, r.json()["id"])
    assert task is not None and task.created_by == world.asset.created_by, "a guest has no account: the video's owner"


def test_editing_the_comment_edits_the_task_until_someone_rewrote_it(http, world, live_db):
    c = _post(http, world, world.reviewer, "Logo bigger")
    assert http(world.reviewer).patch(f"/comments/{c['id']}", json={"body": "Logo much bigger"}).status_code == 200
    task = _task_of(live_db, c["id"])
    assert task.instruction == "Logo much bigger"
    r = http(world.owner).patch(f"/ext/tasks/{task.id}", json={"instruction": "Scale logo to 120%"})
    assert r.status_code == 200, r.text
    http(world.reviewer).patch(f"/comments/{c['id']}", json={"body": "Logo much, much bigger"})
    live_db.refresh(task)
    assert task.instruction == "Scale logo to 120%", "the team's wording is kept"


def test_deleting_the_comment_cancels_its_open_task(http, world, live_db):
    c = _post(http, world, world.reviewer, "tpyo")
    assert http(world.reviewer).delete(f"/comments/{c['id']}").status_code == 204
    task = _task_of(live_db, c["id"])
    assert task.status == "cancelled"
    assert live_db.query(ReviewTaskNote).filter(ReviewTaskNote.task_id == task.id).one().body == "Its comment was deleted."


def test_not_a_task(http, world, live_db):
    c = _post(http, world, world.reviewer, "Love it, great work!")
    task = _task_of(live_db, c["id"])
    for user in (world.reviewer, world.viewer, world.outsider):
        assert http(user).post(f"/ext/tasks/{task.id}/dismiss", json={}).status_code == 404
    r = http(world.editor).post(f"/ext/tasks/{task.id}/dismiss", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"
    assert live_db.query(ReviewTaskNote).filter(ReviewTaskNote.task_id == task.id).one().body == "Not a task"
    assert live_db.query(Comment).filter(Comment.id == c["id"]).one().deleted_at is None, "the comment stays"
    assert http(world.editor).post(f"/ext/tasks/{task.id}/dismiss", json={}).status_code == 409, "only an open one"


def test_publishing_closes_the_tasks_the_fix_check_decided(http, world, live_db):
    fixed = _post(http, world, world.reviewer, "Logo bigger", timecode_start=1.0)
    skip = _post(http, world, world.reviewer, "Different music?", timecode_start=5.0)
    left = _post(http, world, world.reviewer, "Colour warmer", timecode_start=8.0)
    for comment, status in ((fixed, "fixed"), (skip, "wont_fix"), (left, "not_fixed")):
        r = http(world.editor).put(f"/ext/versions/{world.v2.id}/fix-checks/{comment['id']}", json={"status": status})
        assert r.status_code == 200, r.text
    r = http(world.owner).post(f"/ext/versions/{world.v2.id}/publish?force=true")
    assert r.status_code == 200, r.text
    t_fixed, t_skip, t_left = (_task_of(live_db, c["id"]) for c in (fixed, skip, left))
    for t in (t_fixed, t_skip, t_left):
        live_db.refresh(t)
    assert (t_fixed.status, t_fixed.result_version_id) == ("approved", world.v2.id)
    assert live_db.get(VersionResolvedTask, (world.v2.id, t_fixed.id)) is not None
    assert t_skip.status == "cancelled"
    assert t_left.status == "open", "not fixed: still to do"
    payload = live_db.query(ExtEvent).filter(ExtEvent.type == "version.published").order_by(ExtEvent.seq.desc()).first().payload
    assert payload["approved_task_ids"] == [str(t_fixed.id)] and payload["cancelled_task_ids"] == [str(t_skip.id)]


def test_no_task_on_a_version_a_manager_approved(http, world, live_db):
    from apps.api.models.approval import Approval, ApprovalStatus
    live_db.add(Approval(asset_id=world.asset.id, version_id=world.v1.id, user_id=world.owner.id, status=ApprovalStatus.approved))
    live_db.flush()
    c = _post(http, world, world.reviewer, "One more thing")
    assert _task_of(live_db, c["id"]) is None
