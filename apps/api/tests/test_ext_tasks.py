"""Review tasks: who may move one where, the stage rule, notes, and the events
each change leaves behind (apps/api/ext/tasks.py, router.py)."""
import itertools
import uuid

import pytest

from apps.api.ext import auth as ext_auth
from apps.api.ext import tasks
from apps.api.ext.models import ExtEvent, ReviewTask, ReviewTaskNote, TaskStatus
from apps.api.tests.ext_fixtures import http, world  # noqa: F401 - fixtures

S = TaskStatus
ALL = [s.value for s in TaskStatus]
G, M = tasks.GATEWAY, tasks.MANAGER

# The whole table, written out. Anything not listed here is refused.
ALLOWED = {
    ("open", "claimed", G),
    ("claimed", "in_progress", G),
    ("in_progress", "proposed", G),
    ("claimed", "open", G), ("in_progress", "open", G),          # the lease ran out
    ("proposed", "approved", M),
    ("proposed", "open", M),                                      # rejected
    ("open", "needs_info", G), ("open", "needs_info", M),
    ("claimed", "needs_info", G), ("claimed", "needs_info", M),
    ("in_progress", "needs_info", G), ("in_progress", "needs_info", M),
    ("proposed", "needs_info", G), ("proposed", "needs_info", M),
    ("open", "cancelled", M), ("claimed", "cancelled", M), ("in_progress", "cancelled", M),
    ("proposed", "cancelled", M), ("needs_info", "cancelled", M),
}


@pytest.mark.parametrize("from_status,to_status,who", [
    (f, t, w) for f, t, w in itertools.product(ALL, ALL, (G, M)) if f != "needs_info" or t == "cancelled"
])
def test_every_status_change_is_allowed_or_refused_by_the_table(from_status, to_status, who):
    assert tasks.allowed(from_status, to_status, who) is ((from_status, to_status, who) in ALLOWED)


@pytest.mark.parametrize("resume", ["open", "claimed", "in_progress", "proposed"])
@pytest.mark.parametrize("who", [G, M])
def test_an_answered_question_goes_back_where_it_was_asked_or_to_open(resume, who):
    for target in ALL:
        expected = target in (resume, "open") or (target == "cancelled" and who == M)
        assert tasks.allowed("needs_info", target, who, resume) is expected, target


def test_nothing_leaves_a_finished_task():
    for done in ("approved", "cancelled"):
        for target, who in itertools.product(ALL, (G, M)):
            assert tasks.allowed(done, target, who) is False


# ── Through the API ────────────────────────────────────────────────────────────

@pytest.fixture
def gateway(http, world, live_db):
    """A client holding a gateway token with every scope."""
    _, token = ext_auth.create_token(live_db, "test gateway", world.admin.id)
    live_db.flush()
    return http(token=token)


def _new_task(http, world, user=None, **overrides):
    body = {
        "asset_id": str(world.asset.id), "version_id": str(world.v1.id), "kind": "cut_trim",
        "instruction": "Trim the pause at the start", "stage": "rough",
        "assignee_type": "role_pool", "assignee_role": "editor", "frame_in": 10, "frame_out": 40,
    }
    body.update(overrides)
    return http(user or world.owner).post("/ext/tasks", json=body)


def _events(live_db, since=0):
    return [(e.type, e.payload) for e in live_db.query(ExtEvent).filter(ExtEvent.seq > since).order_by(ExtEvent.seq)]


def _last_seq(live_db):
    last = live_db.query(ExtEvent).order_by(ExtEvent.seq.desc()).first()
    return last.seq if last else 0


def test_a_comment_becomes_a_task(http, world, live_db):
    """Frames come from the comment's timecode and the version's frame rate."""
    from apps.api.models.asset import MediaFile

    live_db.query(MediaFile).filter(MediaFile.version_id == world.v1.id).one().fps = 25.0
    live_db.flush()
    response = http(world.owner).post("/ext/tasks", json={
        "asset_id": str(world.asset.id), "source_comment_id": str(world.c_client.id),
        "kind": "colour", "stage": "review", "assignee_type": "person",
        "assignee_person_id": str(world.editor.id),
    })
    assert response.status_code == 201, response.text
    task = response.json()
    assert task["instruction"] == world.c_client.body
    assert task["created_on_version_id"] == str(world.v1.id)
    assert (task["frame_in"], task["frame_out"]) == (50, 50)      # 2.0 s at 25 fps, a point
    assert task["status"] == "open" and task["source_comment_id"] == str(world.c_client.id)


def test_an_editor_can_split_off_a_task_and_a_client_cannot(http, world):
    assert _new_task(http, world, world.editor).status_code == 201
    for client in (world.reviewer, world.viewer, world.outsider):
        refused = _new_task(http, world, client)
        assert refused.status_code == 404, refused.text


@pytest.mark.parametrize("stage,allowed", [
    ("selects", False), ("radio", True), ("rough", True), ("fine", False),
    ("finish", False), ("review", True), ("deliver", False),
])
def test_an_agent_is_assigned_only_at_radio_rough_and_review(http, world, stage, allowed):
    response = _new_task(http, world, stage=stage, assignee_type="agent", assignee_role=None)
    assert response.status_code == (201 if allowed else 422), response.text
    # A person may be assigned at every stage.
    assert _new_task(http, world, stage=stage, assignee_type="person",
                     assignee_person_id=str(world.editor.id)).status_code == 201


def test_a_task_cannot_be_moved_to_an_agent_at_a_human_stage(http, world):
    task = _new_task(http, world, stage="fine").json()
    to_agent = http(world.owner).patch(f"/ext/tasks/{task['id']}", json={"assignee_type": "agent"})
    assert to_agent.status_code == 422
    agent_task = _new_task(http, world, stage="rough", assignee_type="agent").json()
    to_fine = http(world.owner).patch(f"/ext/tasks/{agent_task['id']}", json={"stage": "fine"})
    assert to_fine.status_code == 422


def test_the_assignee_must_be_on_the_projects_team(http, world):
    for person in (world.reviewer, world.outsider):
        response = _new_task(http, world, assignee_type="person", assignee_person_id=str(person.id))
        assert response.status_code == 422, response.text


def test_nothing_is_assigned_on_a_version_a_manager_approved(http, world, live_db):
    from apps.api.models.approval import Approval, ApprovalStatus

    # A client's approval does not close the version to further work...
    live_db.add(Approval(asset_id=world.asset.id, version_id=world.v1.id,
                         user_id=world.reviewer.id, status=ApprovalStatus.approved))
    live_db.flush()
    assert _new_task(http, world).status_code == 201
    # ...the owner's does.
    live_db.add(Approval(asset_id=world.asset.id, version_id=world.v1.id,
                         user_id=world.owner.id, status=ApprovalStatus.approved))
    live_db.flush()
    refused = _new_task(http, world)
    assert refused.status_code == 409, refused.text


def _status(client, task_id, status, **extra):
    return client.post(f"/ext/tasks/{task_id}/status", json={"status": status, **extra})


def test_the_whole_flow_to_approval(http, world, gateway, live_db):
    task = _new_task(http, world).json()
    tid = task["id"]
    assert _status(gateway, tid, "claimed", claimed_by="Eddie on garlopc").json()["claimed_by"] == "Eddie on garlopc"
    assert _status(gateway, tid, "in_progress").json()["status"] == "in_progress"
    assert _status(gateway, tid, "proposed").status_code == 422          # no version given
    proposed = _status(gateway, tid, "proposed", result_version_id=str(world.v2.id),
                       note="Trimmed 12 frames", note_author_type="person",
                       note_author_person_id=str(world.editor.id)).json()
    assert proposed["status"] == "proposed" and proposed["result_version_id"] == str(world.v2.id)
    approved = http(world.owner).post(f"/ext/tasks/{tid}/approve", json={"note": "good"})
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    # Finished: nothing moves it again, the gateway and the owner included.
    assert _status(gateway, tid, "open").status_code == 409
    assert _status(http(world.owner), tid, "cancelled").status_code == 409
    meta = http(world.owner).get(f"/ext/versions/{world.v2.id}/meta").json()
    assert meta["resolves_task_ids"] == [tid]


def test_a_rejection_reopens_the_task_with_the_managers_note(http, world, gateway, live_db):
    tid = _new_task(http, world).json()["id"]
    for step in ("claimed", "in_progress"):
        _status(gateway, tid, step)
    _status(gateway, tid, "proposed", result_version_id=str(world.v2.id))
    assert http(world.owner).post(f"/ext/tasks/{tid}/reject", json={}).status_code == 422   # a note is required
    seq = _last_seq(live_db)
    rejected = http(world.owner).post(f"/ext/tasks/{tid}/reject", json={"note": "Still too long"})
    assert rejected.status_code == 200
    body = rejected.json()
    assert body["status"] == "open" and body["result_version_id"] is None and body["claimed_by"] is None
    notes = http(world.editor).get(f"/ext/tasks/{tid}/notes").json()
    assert [(n["kind"], n["body"], n["author_person_id"]) for n in notes] == [
        ("review", "Still too long", str(world.owner.id)),
    ]
    kinds = [(t, p.get("rejected")) for t, p in _events(live_db, seq)]
    assert kinds == [("task_note.created", None), ("task.status_changed", True)]


def test_only_a_manager_approves_rejects_or_cancels(http, world, gateway):
    tid = _new_task(http, world).json()["id"]
    for step in ("claimed", "in_progress"):
        _status(gateway, tid, step)
    _status(gateway, tid, "proposed", result_version_id=str(world.v2.id))
    for path in ("approve", "reject"):
        assert gateway.post(f"/ext/tasks/{tid}/{path}", json={"note": "x"}).status_code == 403
        assert http(world.editor).post(f"/ext/tasks/{tid}/{path}", json={"note": "x"}).status_code == 403
        assert http(world.reviewer).post(f"/ext/tasks/{tid}/{path}", json={"note": "x"}).status_code == 404
    for target in ("approved", "cancelled"):
        assert _status(gateway, tid, target).status_code == 403
        assert _status(http(world.editor), tid, target).status_code == 403
    # An editor does not move a task by hand at all; their work moves through the worker.
    assert _status(http(world.editor), tid, "needs_info", note="?").status_code == 403
    assert http(world.admin).post(f"/ext/tasks/{tid}/approve", json={}).status_code == 200


def test_a_question_parks_the_task_and_the_answer_resumes_it(http, world, gateway):
    tid = _new_task(http, world).json()["id"]
    _status(gateway, tid, "claimed")
    assert _status(gateway, tid, "needs_info").status_code == 422        # the question is the point
    asked = _status(gateway, tid, "needs_info", note="Which pause? There are two.",
                    note_author_type="agent", note_author_label="agent on garlopc for Eddie").json()
    assert asked["status"] == "needs_info" and asked["resume_status"] == "claimed"
    assert _status(gateway, tid, "proposed", result_version_id=str(world.v2.id)).status_code == 403
    resumed = _status(http(world.owner), tid, "claimed", note="The first one.").json()
    assert resumed["status"] == "claimed" and resumed["resume_status"] is None
    notes = http(world.owner).get(f"/ext/tasks/{tid}/notes").json()
    assert [(n["kind"], n["author_type"], n["author_label"]) for n in notes] == [
        ("question", "agent", "agent on garlopc for Eddie"), ("review", "person", None),
    ]


def test_a_result_version_must_belong_to_the_tasks_asset(http, world, gateway, seed):
    other = seed.media(world.project, world.owner, name="another-video")
    other_version = seed.versions_of(other)[0]
    tid = _new_task(http, world).json()["id"]
    for step in ("claimed", "in_progress"):
        _status(gateway, tid, step)
    assert _status(gateway, tid, "proposed", result_version_id=str(other_version.id)).status_code == 422


def test_a_task_under_way_cannot_be_reassigned_underneath_its_holder(http, world, gateway):
    tid = _new_task(http, world).json()["id"]
    _status(gateway, tid, "claimed")
    moved = http(world.owner).patch(f"/ext/tasks/{tid}", json={
        "assignee_type": "person", "assignee_person_id": str(world.editor.id),
    })
    assert moved.status_code == 409
    reworded = http(world.owner).patch(f"/ext/tasks/{tid}", json={"instruction": "Trim both pauses"})
    assert reworded.status_code == 200 and reworded.json()["instruction"] == "Trim both pauses"


def test_listing_is_limited_to_projects_where_the_caller_is_on_the_team(http, world, seed, gateway):
    from apps.api.models.project import ProjectRole

    elsewhere = seed.project(seed.user(name="someone else"), name="Not Eddie's")
    other_asset = seed.media(elsewhere, world.admin, name="theirs")
    mine = _new_task(http, world).json()["id"]
    theirs = http(world.admin).post("/ext/tasks", json={
        "asset_id": str(other_asset.id), "kind": "music", "instruction": "x", "stage": "rough",
        "assignee_type": "agent",
    }).json()["id"]
    assert [t["id"] for t in http(world.editor).get("/ext/tasks").json()] == [mine]
    assert http(world.reviewer).get("/ext/tasks").json() == []
    assert {t["id"] for t in gateway.get("/ext/tasks").json()} >= {mine, theirs}
    assert {t["id"] for t in http(world.admin).get("/ext/tasks").json()} >= {mine, theirs}
    assert http(world.editor).get(f"/ext/tasks/{theirs}").status_code == 404
    filtered = gateway.get(f"/ext/tasks?asset_id={world.asset.id}&status=open,claimed&stage=rough").json()
    assert [t["id"] for t in filtered] == [mine]
    seed.member(elsewhere, world.editor, ProjectRole.editor)
    assert {t["id"] for t in http(world.editor).get("/ext/tasks").json()} == {mine, theirs}


# ── Notes are internal, always ─────────────────────────────────────────────────

def test_no_client_reads_or_writes_a_task_note(http, world):
    tid = _new_task(http, world).json()["id"]
    http(world.editor).post(f"/ext/tasks/{tid}/notes", json={"kind": "editor_note", "body": "SECRET-EDITOR-NOTE"})
    for client in (world.reviewer, world.viewer, world.outsider):
        for response in (
            http(client).get(f"/ext/tasks/{tid}/notes"),
            http(client).get(f"/ext/tasks/{tid}"),
            http(client).get(f"/ext/tasks?asset_id={world.asset.id}"),
            http(client).post(f"/ext/tasks/{tid}/notes", json={"kind": "reply", "body": "x"}),
        ):
            assert "SECRET-EDITOR-NOTE" not in response.text
            assert response.status_code in (404, 200) and response.text in ('{"detail":"Not found"}', "[]")
    assert http().get(f"/ext/tasks/{tid}/notes").status_code in (401, 403)


def test_a_note_has_nowhere_to_store_client_visibility(http, world, live_db):
    """Not a rule that could be bypassed: the table has no such column, and a
    request that asks for one is stored without it."""
    assert "visibility" not in ReviewTaskNote.__table__.columns
    tid = _new_task(http, world).json()["id"]
    response = http(world.owner).post(f"/ext/tasks/{tid}/notes", json={
        "kind": "editor_note", "body": "for the team", "visibility": "public", "client_visible": True,
    })
    assert response.status_code == 201
    assert "visibility" not in response.json() and "client_visible" not in response.json()


def test_a_person_cannot_post_a_note_as_someone_else(http, world):
    tid = _new_task(http, world).json()["id"]
    note = http(world.editor).post(f"/ext/tasks/{tid}/notes", json={
        "kind": "editor_note", "body": "x", "author_person_id": str(world.owner.id),
        "author_type": "agent", "author_label": "Olga",
    }).json()
    assert (note["author_person_id"], note["author_type"], note["author_label"]) == (str(world.editor.id), "person", None)


# ── Events ─────────────────────────────────────────────────────────────────────

def test_each_change_writes_exactly_one_event(http, world, gateway, live_db):
    seq = _last_seq(live_db)
    tid = _new_task(http, world).json()["id"]
    http(world.owner).patch(f"/ext/tasks/{tid}", json={"instruction": "new words"})
    http(world.owner).patch(f"/ext/tasks/{tid}", json={"instruction": "new words"})      # no change, no event
    _status(gateway, tid, "claimed")
    http(world.editor).post(f"/ext/tasks/{tid}/notes", json={"kind": "editor_note", "body": "on it"})
    http(world.owner).post(f"/assets/{world.asset.id}/comments",
                           json={"version_id": str(world.v1.id), "body": "a comment"})
    got = _events(live_db, seq)
    assert [t for t, _ in got] == [
        "task.created", "task.updated", "task.status_changed", "task_note.created", "comment.created",
    ]
    assert got[1][1]["changed"] == ["instruction"]
    assert (got[2][1]["from"], got[2][1]["to"], got[2][1]["by"]) == ("open", "claimed", "gateway")
    assert got[4][1]["visibility"] == "internal"


def test_a_refused_change_writes_no_event(http, world, gateway, live_db):
    tid = _new_task(http, world).json()["id"]
    seq = _last_seq(live_db)
    assert _status(gateway, tid, "approved").status_code == 403
    assert _new_task(http, world, stage="fine", assignee_type="agent").status_code == 422
    assert http(world.reviewer).post(f"/ext/tasks/{tid}/notes", json={"kind": "reply", "body": "x"}).status_code == 404
    assert _events(live_db, seq) == []


def test_an_event_is_in_the_same_transaction_as_its_change(real_db, seed_real):
    """Roll the change back and the event is gone with it: there is no window in
    which the gateway could be told about something that did not happen."""
    asset, version, owner = seed_real
    task = ReviewTask(
        project_id=asset.project_id, asset_id=asset.id, created_on_version_id=version.id,
        kind="music", instruction="x", stage="rough", assignee_type="agent", created_by=owner.id,
    )
    savepoint = real_db.begin_nested()
    real_db.add(task)
    real_db.flush()
    from apps.api.ext import events
    event = events.emit(real_db, events.TASK_CREATED, task.project_id, {"task": tasks.task_payload(task)})
    seq, task_id = event.seq, task.id
    assert seq is not None
    savepoint.rollback()
    real_db.expire_all()
    assert real_db.get(ReviewTask, task_id) is None
    assert real_db.query(ExtEvent).filter(ExtEvent.seq == seq).count() == 0


@pytest.fixture
def seed_real(real_db):
    from apps.api.tests.conftest import MediaSeed
    seed = MediaSeed(real_db)
    owner = seed.user()
    project = seed.project(owner)
    asset = seed.asset(project, owner)
    version, _ = seed.version(asset, owner)
    return asset, version, owner


def test_the_catch_up_endpoint_returns_events_in_order_from_since(http, world, gateway, live_db):
    seq = _last_seq(live_db)
    for n in range(5):
        _new_task(http, world, instruction=f"task {n}")
    page = gateway.get(f"/ext/events?since={seq}&limit=3").json()
    seqs = [e["seq"] for e in page["events"]]
    assert seqs == sorted(seqs) and len(seqs) == 3 and all(s > seq for s in seqs)
    assert page["last_seq"] == seqs[-1]
    rest = gateway.get(f"/ext/events?since={page['last_seq']}").json()
    assert [e["payload"]["task"]["instruction"] for e in page["events"] + rest["events"]] == [
        f"task {n}" for n in range(5)
    ]
    nothing = gateway.get(f"/ext/events?since={rest['last_seq']}").json()
    assert nothing == {"events": [], "last_seq": rest["last_seq"]}


def test_events_are_the_gateways_to_read(http, world):
    for user in (world.owner, world.editor, world.reviewer):
        assert http(user).get("/ext/events").status_code == 403
    assert http(world.admin).get("/ext/events").status_code == 200
    assert http().get("/ext/events").status_code in (401, 403)


def test_the_team_is_told_live_and_clients_are_not(http, world):
    http.published.clear()
    _new_task(http, world)
    assert http.published == [(f"{world.project.id}:internal", "review_tasks_changed", {})]
