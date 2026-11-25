"""The gateway's service account: what its token may do and, above all, what it
may never do (apps/api/ext/auth.py, router.py)."""
import uuid

import pytest

from apps.api.ext import auth as ext_auth
from apps.api.ext.models import ExtEvent, GatewayToken, VersionExt
from apps.api.tests.ext_fixtures import INTERNAL_NOTE, http, world  # noqa: F401 - fixtures


@pytest.fixture
def token(world, live_db):
    row, token = ext_auth.create_token(live_db, "garlomedia-gateway", world.admin.id)
    live_db.flush()
    return row, token


@pytest.fixture
def gateway(http, token):
    return http(token=token[1])


@pytest.fixture
def storage(monkeypatch):
    """Stand-ins for the object store and the transcoder: what was asked of them."""
    import apps.api.routers.upload as upload_module
    from apps.api.services import s3_service

    calls = {"presigned": [], "processing": [], "size": None}
    monkeypatch.setattr(
        s3_service, "generate_presigned_put_url",
        lambda key, content_type=None, expires_in=3600: calls["presigned"].append((key, content_type, expires_in))
        or f"https://storage.test/{key}?signature=abc",
    )
    monkeypatch.setattr(s3_service, "head_object_size", lambda key: calls["size"])
    monkeypatch.setattr(upload_module, "_trigger_processing", lambda a, v: calls["processing"].append((a, v)))
    return calls


def _version_body(world, **overrides):
    body = {
        "original_filename": "hero-cut_v3_480p.mp4", "mime_type": "video/mp4", "file_size_bytes": 4_000_000,
        "author_person_id": str(world.editor.id), "author_type": "person",
        "resolve_project": "2026-11-20_GARLO_Campaign", "resolve_timeline": "hero-cut v3",
        "start_timecode": "01:00:00:00", "fps": 25,
    }
    body.update(overrides)
    return body


# ── The token itself ───────────────────────────────────────────────────────────

def test_only_the_hash_of_a_token_is_stored(token, live_db):
    row, plain = token
    assert plain.startswith("ffgw_") and len(plain) > 40
    stored = live_db.query(GatewayToken).filter(GatewayToken.id == row.id).one()
    assert stored.token_hash == ext_auth.hash_token(plain) and len(stored.token_hash) == 64
    assert plain not in repr(vars(stored))


def test_an_admin_creates_a_token_and_sees_it_once(http, world, live_db):
    for user in (world.owner, world.editor, world.reviewer):
        assert http(user).post("/ext/gateway-tokens", json={"name": "x"}).status_code == 403
    created = http(world.admin).post("/ext/gateway-tokens", json={"name": "gateway"})
    assert created.status_code == 201, created.text
    plain = created.json()["token"]
    assert created.json()["scopes"] == list(ext_auth.ALL_SCOPES)
    listed = http(world.admin).get("/ext/gateway-tokens")
    assert plain not in listed.text and "token_hash" not in listed.text
    assert http(token=plain).get("/ext/tasks").status_code == 200
    assert http(world.admin).post("/ext/gateway-tokens", json={"name": "x", "scopes": ["approve"]}).status_code == 422


def test_a_revoked_or_made_up_token_is_refused(http, world, token, live_db):
    row, plain = token
    assert http(token=plain).get("/ext/tasks").status_code == 200
    assert http(world.admin).delete(f"/ext/gateway-tokens/{row.id}").status_code == 204
    assert http(token=plain).get("/ext/tasks").status_code == 401
    assert http(token="ffgw_" + "a" * 43).get("/ext/tasks").status_code == 401


def test_a_gateway_token_is_not_a_login(gateway, world):
    """It opens /ext and nothing else: every upstream route wants a person."""
    for path in (f"/assets/{world.asset.id}", f"/assets/{world.asset.id}/comments",
                 f"/projects/{world.project.id}/assets", "/me/assets", "/users"):
        assert gateway.get(path).status_code == 401, path


def test_a_token_without_a_scope_cannot_use_it(http, world, live_db, storage):
    _, read_only = ext_auth.create_token(live_db, "read only", world.admin.id, [ext_auth.SCOPE_READ])
    live_db.flush()
    client = http(token=read_only)
    task = http(world.owner).post("/ext/tasks", json={
        "asset_id": str(world.asset.id), "kind": "music", "instruction": "x", "stage": "rough",
        "assignee_type": "agent",
    }).json()
    assert client.get(f"/ext/tasks/{task['id']}").status_code == 200
    assert client.post(f"/ext/tasks/{task['id']}/status", json={"status": "claimed"}).status_code == 403
    assert client.post(f"/ext/tasks/{task['id']}/notes", json={"kind": "agent_note", "body": "x"}).status_code == 403
    assert client.post(f"/ext/assets/{world.asset.id}/versions", json=_version_body(world)).status_code == 403
    assert client.put(f"/ext/versions/{world.v2.id}/meta", json={}).status_code == 403


# ── What it is for ─────────────────────────────────────────────────────────────

def test_the_gateway_reads_internal_data_on_every_project(gateway, world):
    meta = gateway.get(f"/ext/versions/{world.v2.id}/meta")
    assert meta.status_code == 200 and meta.json()["visibility"] == "internal"
    events = gateway.get("/ext/events")
    assert events.status_code == 200


def test_the_gateway_uploads_a_version_through_one_presigned_url(gateway, world, storage, live_db):
    from apps.api.models.asset import AssetVersion, MediaFile, ProcessingStatus

    created = gateway.post(f"/ext/assets/{world.asset.id}/versions", json=_version_body(world))
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["version_number"] == 3
    key = f"raw/{world.project.id}/{world.asset.id}/{body['version_id']}/original.mp4"
    assert body["s3_key"] == key and body["upload_url"].startswith("https://storage.test/")
    assert storage["presigned"] == [(key, "video/mp4", 3600)]
    # Nothing about the storage account leaves the server: a URL and a key, no credentials.
    assert set(body) == {"asset_id", "version_id", "version_number", "s3_key", "upload_url", "upload_expires_in"}

    version = live_db.query(AssetVersion).filter(AssetVersion.id == body["version_id"]).one()
    assert version.visibility == "internal" and version.created_by == world.editor.id
    assert version.processing_status == ProcessingStatus.uploading
    ext = live_db.get(VersionExt, version.id)
    assert (ext.resolve_project, ext.resolve_timeline, ext.start_timecode, ext.fps, ext.author_type) == (
        "2026-11-20_GARLO_Campaign", "hero-cut v3", "01:00:00:00", 25.0, "person",
    )

    # Finalize before the file is there: refused, nothing started.
    assert gateway.post(f"/ext/versions/{version.id}/finalize").status_code == 409
    assert storage["processing"] == []

    storage["size"] = 4_100_000        # what storage really holds wins over what was declared
    seq = live_db.query(ExtEvent).count()
    done = gateway.post(f"/ext/versions/{version.id}/finalize")
    assert done.status_code == 200 and done.json()["status"] == "processing"
    assert storage["processing"] == [(world.asset.id, version.id)]
    live_db.expire_all()
    assert live_db.query(MediaFile).filter(MediaFile.version_id == version.id).one().file_size_bytes == 4_100_000
    created_events = [e for e in live_db.query(ExtEvent).order_by(ExtEvent.seq) if e.type == "version.created"]
    assert [e.payload["version_id"] for e in created_events] == [str(version.id)]
    assert created_events[0].payload["visibility"] == "internal"
    assert live_db.query(ExtEvent).count() == seq + 1

    # Again: the same answer, and no second transcode.
    again = gateway.post(f"/ext/versions/{version.id}/finalize")
    assert again.status_code == 200 and storage["processing"] == [(world.asset.id, version.id)]


def test_a_gateway_version_stays_hidden_from_the_client_until_published(gateway, http, world, storage, live_db):
    from apps.api.models.asset import AssetVersion, ProcessingStatus

    version_id = gateway.post(f"/ext/assets/{world.asset.id}/versions", json=_version_body(world)).json()["version_id"]
    storage["size"] = 4_000_000
    gateway.post(f"/ext/versions/{version_id}/finalize")
    live_db.query(AssetVersion).filter(AssetVersion.id == version_id).one().processing_status = ProcessingStatus.ready
    live_db.flush()
    assert [v["version_number"] for v in http(world.reviewer).get(f"/assets/{world.asset.id}/versions").json()] == [1]
    assert version_id in http(world.editor).get(f"/assets/{world.asset.id}/versions").text


def test_the_version_author_must_be_on_the_projects_team(gateway, world, storage):
    for person in (world.reviewer, world.outsider):
        refused = gateway.post(f"/ext/assets/{world.asset.id}/versions",
                               json=_version_body(world, author_person_id=str(person.id)))
        assert refused.status_code == 422, refused.text
    assert gateway.post(f"/ext/assets/{world.asset.id}/versions",
                        json=_version_body(world, mime_type="application/x-msdownload")).status_code == 400
    assert storage["presigned"] == []


def test_the_gateway_writes_version_metadata_and_people_do_not(gateway, http, world, live_db):
    task = http(world.owner).post("/ext/tasks", json={
        "asset_id": str(world.asset.id), "kind": "music", "instruction": "x", "stage": "rough",
        "assignee_type": "agent",
    }).json()
    meta = {"resolve_project": "P", "resolve_timeline": "T v2", "start_timecode": "00:59:58:00", "fps": 23.976,
            "author_type": "agent", "author_person_id": str(world.editor.id), "agent_machine": "garlopc",
            "resolves_task_ids": [task["id"]]}
    assert http(world.owner).put(f"/ext/versions/{world.v2.id}/meta", json=meta).status_code == 403
    written = gateway.put(f"/ext/versions/{world.v2.id}/meta", json=meta)
    assert written.status_code == 200, written.text
    read = http(world.editor).get(f"/ext/versions/{world.v2.id}/meta").json()
    assert (read["author_type"], read["agent_machine"], read["resolves_task_ids"]) == ("agent", "garlopc", [task["id"]])
    assert http(world.reviewer).get(f"/ext/versions/{world.v2.id}/meta").status_code == 404
    assert gateway.put(f"/ext/versions/{world.v2.id}/meta",
                       json={**meta, "resolves_task_ids": [str(uuid.uuid4())]}).status_code == 422


# ── What it may never do ───────────────────────────────────────────────────────

def test_the_gateway_can_never_approve_publish_or_change_visibility(gateway, http, world, live_db):
    from apps.api.models.asset import AssetVersion
    from apps.api.models.comment import Comment

    task = http(world.owner).post("/ext/tasks", json={
        "asset_id": str(world.asset.id), "kind": "music", "instruction": "x", "stage": "rough",
        "assignee_type": "agent",
    }).json()
    for step, extra in (("claimed", {}), ("in_progress", {}), ("proposed", {"result_version_id": str(world.v2.id)})):
        assert gateway.post(f"/ext/tasks/{task['id']}/status", json={"status": step, **extra}).status_code == 200
    forbidden = [
        gateway.post(f"/ext/tasks/{task['id']}/approve", json={}),
        gateway.post(f"/ext/tasks/{task['id']}/reject", json={"note": "x"}),
        gateway.post(f"/ext/tasks/{task['id']}/status", json={"status": "approved"}),
        gateway.post(f"/ext/tasks/{task['id']}/status", json={"status": "cancelled"}),
        gateway.post(f"/ext/versions/{world.v2.id}/publish"),
        gateway.patch(f"/ext/comments/{world.c_internal.id}/visibility", json={"visibility": "public"}),
        gateway.post("/ext/gateway-tokens", json={"name": "another"}),
        gateway.get("/ext/gateway-tokens"),
        gateway.post("/ext/tasks", json={"asset_id": str(world.asset.id), "kind": "music", "instruction": "x",
                                          "stage": "rough", "assignee_type": "agent"}),
        gateway.patch(f"/ext/tasks/{task['id']}", json={"instruction": "rewritten by the gateway"}),
    ]
    assert [r.status_code for r in forbidden] == [403] * len(forbidden), [r.text for r in forbidden]
    live_db.expire_all()
    assert live_db.query(AssetVersion).filter(AssetVersion.id == world.v2.id).one().visibility == "internal"
    assert live_db.query(Comment).filter(Comment.id == world.c_internal.id).one().visibility == "internal"
    assert http(world.owner).get(f"/ext/tasks/{task['id']}").json()["status"] == "proposed"


# ── Publish to client, and comment visibility (a manager's, never the gateway's) ─

def test_publish_to_client_is_the_only_way_a_version_becomes_visible(http, world, live_db):
    from apps.api.models.asset import AssetVersion

    for user in (world.editor, world.reviewer):
        refused = http(user).post(f"/ext/versions/{world.v2.id}/publish")
        assert refused.status_code in (403, 404)
    # the team has checked v1's comments on v2 (apps/api/ext/fixcheck.py)
    for comment in (world.c_client, world.c_internal):
        checked = http(world.editor).put(f"/ext/versions/{world.v2.id}/fix-checks/{comment.id}", json={"status": "fixed"})
        assert checked.status_code == 200, checked.text
    http.published.clear()
    seq = live_db.query(ExtEvent).count()
    published = http(world.owner).post(f"/ext/versions/{world.v2.id}/publish")
    assert published.status_code == 200, published.text
    assert published.json()["visibility"] == "public" and published.json()["published_by"] == str(world.owner.id)
    versions = http(world.reviewer).get(f"/assets/{world.asset.id}/versions").json()
    assert [v["version_number"] for v in versions] == [2, 1] or [v["version_number"] for v in versions] == [1, 2]
    events = [e for e in live_db.query(ExtEvent).order_by(ExtEvent.seq)][seq:]
    assert [e.type for e in events] == ["version.published"]
    assert (str(world.project.id), "version_published",
            {"asset_id": str(world.asset.id), "version_id": str(world.v2.id)}) in http.published
    # Again: nothing more happens.
    http(world.owner).post(f"/ext/versions/{world.v2.id}/publish")
    assert live_db.query(ExtEvent).count() == seq + 1


def test_publishing_a_version_sends_the_client_none_of_the_teams_notes(http, world):
    """Not even as a summary: the internal comments on the published version
    stay internal, and nothing is written for the client on the team's behalf."""
    assert http(world.owner).post(f"/ext/versions/{world.v2.id}/publish?force=true").status_code == 200
    for user in (world.reviewer, world.viewer):
        on_v2 = http(user).get(f"/assets/{world.asset.id}/comments?version_id={world.v2.id}")
        assert on_v2.status_code == 200 and on_v2.json() == []
        notifications = http(user).get("/me/notifications")
        assert notifications.json() == []
    guest = http().get(f"/share/{world.link.token}/comments?version_id={world.v2.id}")
    assert guest.json() == []


def test_a_version_still_transcoding_cannot_be_published(http, world, live_db):
    from apps.api.models.asset import ProcessingStatus

    world.v2.processing_status = ProcessingStatus.processing
    live_db.flush()
    assert http(world.owner).post(f"/ext/versions/{world.v2.id}/publish").status_code == 409


def test_a_manager_toggles_a_comments_visibility(http, world, live_db):
    assert http(world.editor).patch(f"/ext/comments/{world.c_internal.id}/visibility",
                                    json={"visibility": "public"}).status_code == 403
    assert http(world.reviewer).patch(f"/ext/comments/{world.c_client.id}/visibility",
                                      json={"visibility": "internal"}).status_code == 404
    # A reply cannot be shown while the comment it answers is hidden.
    assert http(world.owner).patch(f"/ext/comments/{world.c_internal_reply.id}/visibility",
                                   json={"visibility": "public"}).status_code == 409
    seq = live_db.query(ExtEvent).count()
    shown = http(world.owner).patch(f"/ext/comments/{world.c_internal.id}/visibility", json={"visibility": "public"})
    assert shown.status_code == 200 and shown.json()["visibility"] == "public"
    listed = http(world.reviewer).get(f"/assets/{world.asset.id}/comments").text
    assert INTERNAL_NOTE in listed                     # the comment the owner chose to show
    assert world.secrets[1] not in listed              # its reply was not chosen, and stays internal

    # Hiding a comment hides the whole thread under it.
    reply = http(world.reviewer).post(
        f"/assets/{world.asset.id}/comments/{world.c_client.id}/replies",
        json={"version_id": str(world.v1.id), "body": "client reply"},
    ).json()
    hidden = http(world.owner).patch(f"/ext/comments/{world.c_client.id}/visibility", json={"visibility": "internal"})
    assert set(hidden.json()["changed"]) == {str(world.c_client.id), reply["id"]}
    assert "client reply" not in http(world.viewer).get(f"/assets/{world.asset.id}/comments").text
    events = [e for e in live_db.query(ExtEvent).order_by(ExtEvent.seq)][seq:]
    assert [e.type for e in events] == [
        "comment.visibility_changed", "comment.created", "comment.visibility_changed",
    ]


# ── What the gateway reads to route a task and brief a machine ─────────────────

def test_the_gateway_matches_people_by_email_or_id(gateway, http, world):
    by_email = gateway.get(f"/ext/people?emails={world.editor.email.upper()},nobody@example.com")
    assert by_email.status_code == 200, by_email.text
    assert by_email.json() == [{"id": str(world.editor.id), "email": world.editor.email, "name": world.editor.name}]
    by_id = gateway.get(f"/ext/people?ids={world.owner.id},{world.reviewer.id}").json()
    assert {p["email"] for p in by_id} == {world.owner.email, world.reviewer.email}
    assert gateway.get("/ext/people").status_code == 422              # no listing everyone
    assert gateway.get("/ext/people?ids=not-an-id").status_code == 422
    for user in (world.owner, world.editor, world.reviewer):
        assert http(user).get(f"/ext/people?emails={world.editor.email}").status_code == 403
    assert http(world.admin).get(f"/ext/people?emails={world.editor.email}").status_code == 200


def test_the_gateway_reads_one_poster_per_asset(gateway, http, world, live_db, seed, monkeypatch):
    from apps.api.models.asset import AssetType, MediaFile
    from apps.api.services import s3_service

    monkeypatch.setattr(s3_service, "generate_presigned_get_url",
                        lambda key, expires_in=3600, download_filename=None: f"https://storage.test/{key}?expires={expires_in}")
    files = {v.id: live_db.query(MediaFile).filter(MediaFile.version_id == v.id).one() for v in (world.v1, world.v2)}
    files[world.v1.id].s3_key_thumbnail, files[world.v2.id].s3_key_thumbnail = "thumbs/v1.jpg", None
    bare = seed.asset(world.project, world.owner, name="no upload yet")
    sound = seed.asset(world.project, world.owner, name="voice-over")
    sound.asset_type = AssetType.audio
    sound_v, _ = seed.version(sound, world.owner, number=1, s3_keys=True)
    live_db.query(MediaFile).filter(MediaFile.version_id == sound_v.id).one().s3_key_thumbnail = "waveforms/vo.json"
    live_db.flush()
    ask = f"/ext/posters?asset_ids={world.asset.id},{bare.id},{sound.id},{uuid.uuid4()}"

    # the newest version has no poster yet (still transcoding): the one before it serves
    got = gateway.get(ask)
    assert got.status_code == 200, got.text
    assert got.json() == [{"asset_id": str(world.asset.id), "version_id": str(world.v1.id), "version_number": 1,
                           "url": "https://storage.test/thumbs/v1.jpg?expires=3600", "expires_in": 3600}]
    # once the newest has one, it is the newest's, internal or not: the gateway's list is the team's
    files[world.v2.id].s3_key_thumbnail = "thumbs/v2.jpg"
    live_db.flush()
    assert [(p["version_number"], p["url"]) for p in gateway.get(ask).json()] == [(2, "https://storage.test/thumbs/v2.jpg?expires=3600")]
    # a deleted version or asset has none
    world.v2.deleted_at = world.v1.created_at
    live_db.flush()
    assert [p["version_number"] for p in gateway.get(ask).json()] == [1]
    world.asset.deleted_at = world.v1.created_at
    live_db.flush()
    assert gateway.get(ask).json() == []
    world.asset.deleted_at = None
    live_db.flush()

    assert gateway.get("/ext/posters").status_code == 422
    assert gateway.get("/ext/posters?asset_ids=,").status_code == 422
    assert gateway.get("/ext/posters?asset_ids=not-an-id").status_code == 422
    assert gateway.get("/ext/posters?asset_ids=" + ",".join(str(uuid.uuid4()) for _ in range(101))).status_code == 422
    # people get posters through the app; only the gateway (and an admin) may ask here
    for user in (world.owner, world.editor, world.reviewer, world.outsider):
        assert http(user).get(ask).status_code == 403
    assert http(world.admin).get(ask).status_code == 200


def test_a_tasks_context_is_one_read(gateway, http, world, live_db):
    from apps.api.models.asset import MediaFile

    live_db.query(MediaFile).filter(MediaFile.version_id == world.v1.id).one().fps = 25.0
    live_db.flush()
    gateway.put(f"/ext/versions/{world.v1.id}/meta", json={
        "resolve_project": "2026-11-20_GARLO_Campaign", "resolve_timeline": "hero-cut v1",
        "start_timecode": "01:00:00:00", "fps": 25,
    })
    task = http(world.owner).post("/ext/tasks", json={
        "asset_id": str(world.asset.id), "source_comment_id": str(world.c_client.id), "kind": "cut_trim",
        "stage": "review", "assignee_type": "person", "assignee_person_id": str(world.editor.id),
    }).json()
    http(world.owner).post(f"/ext/tasks/{task['id']}/notes", json={"kind": "editor_note", "body": "use take 3"})
    context = gateway.get(f"/ext/tasks/{task['id']}/context")
    assert context.status_code == 200, context.text
    body = context.json()
    assert body["task"]["id"] == task["id"] and (body["task"]["frame_in"], body["task"]["frame_out"]) == (50, 50)
    assert body["project"] == {"id": str(world.project.id), "name": "Campaign film"}
    assert body["asset"]["name"] == "hero-cut"
    assert body["version"]["version_number"] == 1 and body["version"]["fps"] == 25.0
    assert (body["version"]["resolve_project"], body["version"]["resolve_timeline"]) == (
        "2026-11-20_GARLO_Campaign", "hero-cut v1")
    assert body["source_comment"]["body"] == world.c_client.body and body["source_comment"]["author_name"] == "Rita Client"
    assert [n["body"] for n in body["notes"]] == ["use take 3"]
    assert body["assignee"]["email"] == world.editor.email and body["created_by"]["email"] == world.owner.email
    # The team may read it; a client is told there is no such task.
    assert http(world.editor).get(f"/ext/tasks/{task['id']}/context").status_code == 200
    assert http(world.reviewer).get(f"/ext/tasks/{task['id']}/context").status_code == 404
