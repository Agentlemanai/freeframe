"""Clients never see internal data: comments, versions, annotations, counts.

The acceptance test for the fork's visibility rule (apps/api/ext/visibility.py).
One world (see ext_fixtures.world) holds a published v1, an internal v2, and
comments on both sides. Every endpoint a client, a guest or a share link can
reach is read as each of them, and nothing internal may appear anywhere in what
comes back: not a comment's text, not a drawing, not an id, not a count.

The same reads as the owner and the editor must *contain* the internal rows,
or a test that finds nothing would prove nothing.
"""
import pytest

from apps.api.tests.ext_fixtures import (  # noqa: F401 - fixtures
    CLIENT_NOTE, INTERNAL_NOTE, INTERNAL_REPLY, V2_NOTE, http, world,
)

# (label, path template). Every GET that returns comments, annotations,
# versions, or something derived from them, to a signed-in caller.
MEMBER_READS = [
    ("comment list", "/assets/{asset}/comments"),
    ("comment list, one version", "/assets/{asset}/comments?version_id={v1}"),
    ("comment list, asked for internal", "/assets/{asset}/comments?visibility=internal"),
    ("comment export csv", "/assets/{asset}/comments/export?format=csv&version_id={v1}"),
    ("comment export edl", "/assets/{asset}/comments/export?format=edl&version_id={v1}&fps=25"),
    ("comment export fcpxml", "/assets/{asset}/comments/export?format=fcpxml&version_id={v1}&fps=25"),
    ("comment export premiere", "/assets/{asset}/comments/export?format=premiere_xml&version_id={v1}&fps=25"),
    ("asset", "/assets/{asset}"),
    ("asset batch", "/assets/batch?ids={asset}"),
    ("project assets", "/projects/{project}/assets"),
    ("versions", "/assets/{asset}/versions"),
    ("stream, default version", "/assets/{asset}/stream"),
    ("downloads", "/assets/{asset}/downloads"),
    ("verdicts", "/assets/{asset}/verdicts?version_id={v1}"),
    ("my assets", "/me/assets"),
    ("notifications", "/me/notifications"),
]

# The same, addressed at internal rows by id. A client must be told they do not exist.
MEMBER_READS_OF_INTERNAL_ROWS = [
    ("internal version's comments", "/assets/{asset}/comments?version_id={v2}"),
    ("export of the internal version", "/assets/{asset}/comments/export?format=csv&version_id={v2}"),
    ("stream of the internal version", "/assets/{asset}/stream?version_id={v2}"),
    ("downloads of the internal version", "/assets/{asset}/downloads?version_id={v2}"),
    ("verdicts on the internal version", "/assets/{asset}/verdicts?version_id={v2}"),
    ("reactions on an internal comment", "/comments/{c_internal}/reactions"),
    ("deep link to an internal comment", "/assets/{asset}/comments/{c_internal}/link"),
]

SHARE_READS = [
    ("share: validate", "/share/{token}"),
    ("share: comments", "/share/{token}/comments"),
    ("share: comments, latest only", "/share/{token}/comments?latest_only=true"),
    ("share: comments of the internal version", "/share/{token}/comments?version_id={v2}"),
    ("share: stream", "/share/{token}/stream/{asset}"),
    ("share: stream of the internal version", "/share/{token}/stream/{asset}?version_id={v2}"),
    ("share: thumbnail", "/share/{token}/thumbnail/{asset}"),
    ("share: versions", "/share/{token}/assets/{asset}/versions"),
    ("share: downloads", "/share/{token}/downloads/{asset}"),
    ("share: verdicts", "/share/{token}/verdicts"),
    ("project share: assets", "/share/{project_token}/assets"),
    ("project share: comments", "/share/{project_token}/comments?asset_id={asset}"),
    ("project share: versions", "/share/{project_token}/assets/{asset}/versions"),
]


def _url(template, w):
    return template.format(
        asset=w.asset.id, project=w.project.id, v1=w.v1.id, v2=w.v2.id,
        c_internal=w.c_internal.id, token=w.link.token, project_token=w.project_link.token,
    )


def _leaks(response, w):
    return [s for s in w.secrets if s in response.text]


@pytest.mark.parametrize("who", ["reviewer", "viewer"])
@pytest.mark.parametrize("label,template", MEMBER_READS, ids=[r[0] for r in MEMBER_READS])
def test_a_client_member_is_sent_nothing_internal(http, world, who, label, template):
    response = http(getattr(world, who)).get(_url(template, world))
    assert response.status_code == 200, response.text
    assert _leaks(response, world) == []


@pytest.mark.parametrize("who", ["reviewer", "viewer"])
@pytest.mark.parametrize(
    "label,template", MEMBER_READS_OF_INTERNAL_ROWS, ids=[r[0] for r in MEMBER_READS_OF_INTERNAL_ROWS],
)
def test_a_client_member_asking_for_an_internal_row_by_id_gets_nothing(http, world, who, label, template):
    response = http(getattr(world, who)).get(_url(template, world))
    assert _leaks(response, world) == [], response.text
    # Either "no such thing", or an answer about nothing: never the row.
    assert response.status_code in (200, 404, 400, 422), response.text
    if response.status_code == 200:
        assert INTERNAL_NOTE not in response.text and V2_NOTE not in response.text


@pytest.mark.parametrize("caller", ["guest", "reviewer", "owner", "admin"])
@pytest.mark.parametrize("label,template", SHARE_READS, ids=[r[0] for r in SHARE_READS])
def test_a_share_link_shows_nothing_internal_whoever_opens_it(http, world, caller, label, template):
    """A share link is the client's view. The owner checking their own link has
    to see what the client will, not what the owner may."""
    user = None if caller == "guest" else getattr(world, caller)
    response = http(user).get(_url(template, world))
    assert _leaks(response, world) == [], response.text
    assert response.status_code < 500, response.text


def test_the_public_project_visitor_is_a_client_too(http, world, live_db):
    world.project.is_public = True
    live_db.flush()
    for label, template in MEMBER_READS[:12]:
        response = http(world.outsider).get(_url(template, world))
        assert response.status_code == 200, (label, response.text)
        assert _leaks(response, world) == [], label


@pytest.mark.parametrize("who", ["owner", "editor", "admin"])
def test_the_team_still_sees_everything(http, world, who):
    """The control: these same reads must find the internal rows."""
    client = http(getattr(world, who))
    comments = client.get(f"/assets/{world.asset.id}/comments")
    assert comments.status_code == 200
    assert INTERNAL_NOTE in comments.text and INTERNAL_REPLY in comments.text and CLIENT_NOTE in comments.text
    versions = client.get(f"/assets/{world.asset.id}/versions").json()
    assert {v["version_number"]: v["visibility"] for v in versions} == {1: "public", 2: "internal"}
    asset = client.get(f"/assets/{world.asset.id}").json()
    assert asset["latest_version"]["id"] == str(world.v2.id)
    on_v2 = client.get(f"/assets/{world.asset.id}/comments?version_id={world.v2.id}")
    assert V2_NOTE in on_v2.text
    export = client.get(f"/assets/{world.asset.id}/comments/export?format=csv&version_id={world.v1.id}")
    assert INTERNAL_NOTE in export.text


def test_a_client_sees_the_published_version_as_the_latest(http, world):
    asset = http(world.reviewer).get(f"/assets/{world.asset.id}").json()
    assert asset["latest_version"]["id"] == str(world.v1.id)
    versions = http(world.reviewer).get(f"/assets/{world.asset.id}/versions").json()
    assert [v["version_number"] for v in versions] == [1]


def test_comment_counts_leave_internal_comments_out_for_a_client(http, world, live_db):
    """A count is a leak too: "3 comments" beside one visible comment says two more exist."""
    from apps.api.routers.assets import _comment_counts

    assert _comment_counts(live_db, [world.v1.id]) == {world.v1.id: 3}   # nobody bound: all of them
    asset = http(world.reviewer).get(f"/assets/{world.asset.id}").json()
    assert asset["latest_version"]["id"] == str(world.v1.id)
    assert asset["comment_count"] == 1
    listed = http(world.reviewer).get(f"/projects/{world.project.id}/assets").json()
    assert [a["comment_count"] for a in listed] == [1]


# ── Writes a client could use to touch or reveal an internal row ───────────────

@pytest.mark.parametrize("who", ["reviewer", "viewer"])
def test_a_client_cannot_act_on_an_internal_comment(http, world, who, live_db):
    from apps.api.models.comment import Comment

    client = http(getattr(world, who))
    cid = world.c_internal.id
    attempts = [
        client.patch(f"/comments/{cid}", json={"body": "edited"}),
        client.delete(f"/comments/{cid}"),
        client.post(f"/comments/{cid}/resolve"),
        client.post(f"/comments/{cid}/react", json={"emoji": "👍"}),
        client.post(f"/comments/{cid}/attachments",
                    json={"file_name": "a.png", "content_type": "image/png", "file_size": 10}),
        client.post(f"/assets/{world.asset.id}/comments/{cid}/replies",
                    json={"version_id": str(world.v1.id), "body": "a reply"}),
        client.post(f"/assets/{world.asset.id}/comments",
                    json={"version_id": str(world.v1.id), "body": "a reply", "parent_id": str(cid)}),
        client.post(f"/assets/{world.asset.id}/comments",
                    json={"version_id": str(world.v2.id), "body": "on the internal version"}),
    ]
    for response in attempts:
        assert response.status_code in (400, 403, 404), response.text
        assert _leaks(response, world) == []
    live_db.expire_all()
    stored = live_db.query(Comment).filter(Comment.id == cid).one()
    assert stored.body == INTERNAL_NOTE and stored.deleted_at is None and stored.resolved is False


@pytest.mark.parametrize("who", ["reviewer", "viewer"])
def test_a_client_cannot_write_an_internal_comment(http, world, who):
    response = http(getattr(world, who)).post(
        f"/assets/{world.asset.id}/comments",
        json={"version_id": str(world.v1.id), "body": "sneaky", "visibility": "internal"},
    )
    assert response.status_code == 403


def test_a_client_member_cannot_write_an_internal_comment_through_a_share_link(http, world):
    response = http(world.reviewer).post(
        f"/share/{world.link.token}/comment", json={"body": "sneaky", "visibility": "internal"},
    )
    assert response.status_code == 403


# ── Defaults for new comments ──────────────────────────────────────────────────

def _post_comment(http, world, user, **extra):
    response = http(user).post(
        f"/assets/{world.asset.id}/comments",
        json={"version_id": str(world.v1.id), "body": "new", **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()["visibility"]


def test_a_clients_comment_is_public(http, world):
    assert _post_comment(http, world, world.reviewer) == "public"


def test_the_teams_comment_is_internal_unless_a_manager_says_otherwise(http, world):
    assert _post_comment(http, world, world.owner) == "internal"
    assert _post_comment(http, world, world.owner, visibility="public") == "public"
    assert _post_comment(http, world, world.admin, visibility="public") == "public"


def test_an_editors_comment_is_internal_even_when_they_ask_for_public(http, world):
    """Only a manager shows something to the client. Stored internal rather
    than refused: the composer sends "public" unless it is toggled."""
    assert _post_comment(http, world, world.editor) == "internal"
    assert _post_comment(http, world, world.editor, visibility="public") == "internal"


def test_a_reply_under_an_internal_comment_is_internal_whoever_asks(http, world):
    response = http(world.owner).post(
        f"/assets/{world.asset.id}/comments/{world.c_internal.id}/replies",
        json={"version_id": str(world.v1.id), "body": "re", "visibility": "public"},
    )
    assert response.status_code == 201 and response.json()["visibility"] == "internal"


def test_an_editor_commenting_through_a_share_link_stays_internal(http, world):
    response = http(world.editor).post(
        f"/share/{world.link.token}/comment", json={"body": "from the editor", "visibility": "public"},
    )
    assert response.status_code == 201 and response.json()["visibility"] == "internal"
    listed = http().get(f"/share/{world.link.token}/comments")
    assert "from the editor" not in listed.text


# ── Notifications, mentions and live events ────────────────────────────────────

def test_an_internal_comment_notifies_no_client(http, world, live_db):
    """The asset was created by a client here, and a client is mentioned in the
    comment. Neither may be told an internal comment exists."""
    from apps.api.models.activity import Mention, Notification

    world.asset.created_by = world.reviewer.id
    live_db.flush()
    with pytest.MonkeyPatch.context() as mp:
        sent = []
        import apps.api.routers.comments as comments_module
        mp.setattr(comments_module, "send_task_safe", lambda task, *a, **k: sent.append(k))
        response = http(world.owner).post(f"/assets/{world.asset.id}/comments", json={
            "version_id": str(world.v1.id), "body": "team only, cc the client and the editor",
            "mention_user_ids": [str(world.viewer.id), str(world.editor.id)],
        })
    assert response.status_code == 201 and response.json()["visibility"] == "internal"
    comment_id = response.json()["id"]
    told = {n.user_id for n in live_db.query(Notification).filter(Notification.comment_id == comment_id)}
    mentioned = {m.mentioned_user_id for m in live_db.query(Mention).filter(Mention.comment_id == comment_id)}
    assert told == {world.editor.id} and mentioned == {world.editor.id}
    assert [k["to_email"] for k in sent] == [world.editor.email]


def test_an_internal_reply_does_not_notify_the_client_it_answers(http, world, live_db):
    from apps.api.models.activity import Notification

    response = http(world.editor).post(
        f"/assets/{world.asset.id}/comments/{world.c_client.id}/replies",
        json={"version_id": str(world.v1.id), "body": "noted, will fix"},
    )
    assert response.status_code == 201 and response.json()["visibility"] == "internal"
    assert live_db.query(Notification).filter(
        Notification.comment_id == response.json()["id"], Notification.user_id == world.reviewer.id,
    ).count() == 0


def test_an_old_notification_about_an_internal_comment_is_not_listed_to_a_client(http, world, live_db):
    """Rows written before this rule existed, or before a comment was made internal."""
    from apps.api.models.activity import Notification, NotificationType

    for comment in (world.c_internal, world.c_client):
        for user in (world.reviewer, world.editor):
            live_db.add(Notification(user_id=user.id, type=NotificationType.comment,
                                     asset_id=world.asset.id, comment_id=comment.id))
    live_db.flush()
    seen_by_client = http(world.reviewer).get("/me/notifications").json()
    assert [n["comment_id"] for n in seen_by_client] == [str(world.c_client.id)]
    seen_by_editor = {n["comment_id"] for n in http(world.editor).get("/me/notifications").json()}
    assert seen_by_editor == {str(world.c_client.id), str(world.c_internal.id)}


def test_live_events_about_internal_comments_stay_off_the_clients_channel(http, world):
    http(world.owner).post(f"/assets/{world.asset.id}/comments",
                           json={"version_id": str(world.v1.id), "body": "internal"})
    http(world.reviewer).post(f"/assets/{world.asset.id}/comments",
                              json={"version_id": str(world.v1.id), "body": "public"})
    http(world.owner).post(f"/comments/{world.c_internal.id}/resolve")
    channels = [(channel, event) for channel, event, _ in http.published]
    project = str(world.project.id)
    assert channels == [
        (f"{project}:internal", "new_comment"),
        (project, "new_comment"),
        (f"{project}:internal", "comment_resolved"),
    ]


def test_only_the_internal_tier_is_subscribed_to_the_internal_channel(world, live_db):
    from apps.api.ext import live

    project = world.project.id
    answers = {
        who: live.sees_internal_events(live_db, project, getattr(world, who).id)
        for who in ("owner", "editor", "admin", "reviewer", "viewer", "outsider")
    }
    assert answers == {
        "owner": True, "editor": True, "admin": True,
        "reviewer": False, "viewer": False, "outsider": False,
    }
    assert live.stream_channels(project, internal=False) == [f"project:{project}"]
    assert live.stream_channels(project, internal=True) == [f"project:{project}", f"project:{project}:internal"]


def test_a_transcode_event_for_an_internal_version_goes_to_the_internal_channel(world, live_db, monkeypatch):
    from apps.api.ext import live

    monkeypatch.setattr(live, "_version_is_internal", lambda version_id: str(version_id) == str(world.v2.id))
    project = world.project.id
    assert live.transcode_channel_key(project, world.v1.id) == project
    assert live.transcode_channel_key(project, world.v2.id) == f"{project}:internal"

    def broken(version_id):
        raise RuntimeError("database is down")
    monkeypatch.setattr(live, "_version_is_internal", broken)
    assert live.transcode_channel_key(project, world.v1.id) == f"{project}:internal"


# ── Bookkeeping that must not be filtered ──────────────────────────────────────

def test_version_numbers_count_internal_versions_for_every_caller(world, live_db):
    """A caller who cannot see v2 must still not be handed its number."""
    from apps.api.ext import visibility
    from apps.api.services.versions import next_version_number

    visibility.bind_user(live_db, world.reviewer)
    try:
        assert next_version_number(live_db, world.asset.id) == 3
    finally:
        visibility.unbind(live_db)


def test_a_request_session_reads_as_a_client_until_someone_signs_in(world, live_db):
    """`get_db` is where a request's session starts. Unauthenticated, it must
    not return internal rows even to a query that forgot to filter."""
    from apps.api.database import _bind_public_viewer
    from apps.api.ext import visibility
    from apps.api.models.asset import AssetVersion
    from apps.api.models.comment import Comment

    asset_id, v2_id = world.asset.id, world.v2.id
    live_db.expunge_all()   # a request's session starts empty; this one held the seeded rows
    _bind_public_viewer(live_db)
    try:
        versions = live_db.query(AssetVersion).filter(AssetVersion.asset_id == asset_id).all()
        comments = live_db.query(Comment).filter(Comment.asset_id == asset_id).all()
        assert [v.version_number for v in versions] == [1]
        assert [c.body for c in comments] == [CLIENT_NOTE]
        assert live_db.get(AssetVersion, v2_id) is None
    finally:
        visibility.unbind(live_db)


def test_a_stream_gets_the_internal_channel_by_tier_and_loses_it_on_demotion(monkeypatch):
    """GET /events/{project}: the tier is decided at connect, and a subscriber
    who leaves the internal tier has the stream revoked so the reconnect opens
    one without the internal channel."""
    import asyncio
    import time
    import uuid

    from apps.api.routers import events as events_router
    from apps.api.services.permissions import EVENTS_ACCESS_REVOKED

    project_id, user_id = uuid.uuid4(), uuid.uuid4()
    tier = {"internal": True}
    opened = {}

    def fake_stream(project, authorize, **kwargs):
        opened.update(kwargs, authorize=authorize)

        async def nothing():
            if False:
                yield ""
        return nothing()

    monkeypatch.setattr(events_router, "_access_claims", lambda *tokens: (user_id, 1, time.time() + 600))
    monkeypatch.setattr(events_router, "_denial", lambda *args: None)
    monkeypatch.setattr(events_router, "_sees_internal", lambda p, u: tier["internal"])
    monkeypatch.setattr(events_router, "event_stream", fake_stream)

    async def scenario():
        await events_router.stream_events(project_id, token="t", credentials=None)
        assert opened["channels"] == [f"project:{project_id}", f"project:{project_id}:internal"]
        assert await opened["authorize"]() is None
        tier["internal"] = False                       # demoted to reviewer while the tab is open
        assert await opened["authorize"]() == EVENTS_ACCESS_REVOKED
        await events_router.stream_events(project_id, token="t", credentials=None)   # the reconnect
        assert opened["channels"] == [f"project:{project_id}"]
        assert await opened["authorize"]() is None

    asyncio.run(scenario())
