"""The comment feed (apps/api/ext/feed.py): a video's comments with frames, and the
same marker files as the review screen's export, for the gateway and the team.

World (ext_fixtures.world): published v1 with a client comment (2 s) and an
internal comment (4 s, drawing, internal reply); internal v2 with an internal
comment without a timecode.
"""
import pytest

from apps.api.ext import auth as ext_auth
from apps.api.ext.models import VersionExt
from apps.api.tests.ext_fixtures import (  # noqa: F401 - fixtures
    CLIENT_NOTE, INTERNAL_NOTE, INTERNAL_REPLY, V2_NOTE, http, world,
)


@pytest.fixture
def gateway(http, world, live_db):
    _, token = ext_auth.create_token(live_db, "garlomedia-gateway", world.admin.id)
    live_db.add(VersionExt(version_id=world.v1.id, resolve_project="Teaser", resolve_timeline="Teaser_v01",
                           start_timecode="01:00:00:00", fps=25.0))
    live_db.flush()
    return http(token=token)


def test_the_gateway_reads_a_versions_comments_with_frames(gateway, world):
    r = gateway.get(f"/ext/assets/{world.asset.id}/comments?version_id={world.v1.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"]["version_number"] == 1
    assert (body["version"]["resolve_project"], body["version"]["resolve_timeline"], body["version"]["start_timecode"], body["version"]["fps"]) \
        == ("Teaser", "Teaser_v01", "01:00:00:00", 25.0)
    got = [(c["body"], c["frame_in"], c["visibility"], c["has_drawing"]) for c in body["comments"]]
    assert got == [(CLIENT_NOTE, 50, "public", False), (INTERNAL_NOTE, 100, "internal", True)], "in video order, replies folded in"
    assert [x["body"] for x in body["comments"][1]["replies"]] == [INTERNAL_REPLY]
    assert body["comments"][0]["author_name"] == "Rita Client"
    assert [v["version_number"] for v in body["versions"]] == [2, 1]


def test_left_out_version_means_the_newest_ready_one(gateway, world):
    body = gateway.get(f"/ext/assets/{world.asset.id}/comments").json()
    assert body["version"]["version_number"] == 2
    assert [c["body"] for c in body["comments"]] == [V2_NOTE]
    assert body["comments"][0]["frame_in"] is None, "no timecode, no frame"


def test_resolved_comments_only_when_asked(gateway, world, live_db):
    world.c_client.resolved = True
    live_db.flush()
    plain = gateway.get(f"/ext/assets/{world.asset.id}/comments?version_id={world.v1.id}").json()
    assert [c["body"] for c in plain["comments"]] == [INTERNAL_NOTE]
    every = gateway.get(f"/ext/assets/{world.asset.id}/comments?version_id={world.v1.id}&include_resolved=true").json()
    assert [(c["body"], c["resolved"]) for c in every["comments"]] == [(CLIENT_NOTE, True), (INTERNAL_NOTE, False)]


def test_the_team_reads_it_and_clients_are_told_it_does_not_exist(http, world, gateway):
    editor = http(world.editor).get(f"/ext/assets/{world.asset.id}/comments?version_id={world.v1.id}")
    assert editor.status_code == 200 and INTERNAL_NOTE in editor.text
    for user in (world.reviewer, world.viewer, world.outsider, None):
        for path in (f"/ext/assets/{world.asset.id}/comments",
                     f"/ext/assets/{world.asset.id}/comments/export?format=csv&version_id={world.v1.id}"):
            r = http(user).get(path)
            assert r.status_code in (401, 404), (user and user.name, path, r.status_code)
            assert INTERNAL_NOTE not in r.text and CLIENT_NOTE not in r.text


def test_marker_files_for_resolve_and_premiere(gateway, world):
    edl = gateway.get(f"/ext/assets/{world.asset.id}/comments/export?format=edl&version_id={world.v1.id}")
    assert edl.status_code == 200, edl.text
    assert "01:00:02:00" in edl.text, "starts at the version's Resolve start timecode; the client note sits at 2 s"
    assert INTERNAL_NOTE in edl.text and CLIENT_NOTE in edl.text, "the team's file has both kinds"
    xml = gateway.get(f"/ext/assets/{world.asset.id}/comments/export?format=premiere_xml&version_id={world.v1.id}")
    assert xml.status_code == 200 and CLIENT_NOTE in xml.text
    assert 'filename="hero-cut_v1_comments.xml"' in xml.headers["content-disposition"]


def test_a_version_without_timecoded_comments_says_so(gateway, world):
    r = gateway.get(f"/ext/assets/{world.asset.id}/comments/export?format=edl&version_id={world.v2.id}&fps=25")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "no_timecoded_comments"


def test_an_unknown_format_is_refused(gateway, world):
    r = gateway.get(f"/ext/assets/{world.asset.id}/comments/export?format=docx")
    assert r.status_code == 422
