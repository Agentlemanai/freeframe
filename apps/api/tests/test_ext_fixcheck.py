"""Fix check (apps/api/ext/fixcheck.py): before a new version goes to the client,
the team marks each comment from earlier versions fixed / not fixed / won't fix.

World (ext_fixtures.world): published v1 with a client comment (2 s) and an
internal comment (4 s, with a drawing and an internal reply); internal v2 with
an internal comment of its own.
"""
from apps.api.ext.models import ExtEvent, FixCheck
from apps.api.tests.ext_fixtures import (  # noqa: F401 - fixtures
    INTERNAL_NOTE, http, world,
)


OWNER, EDITOR = object(), object()


def _who(world, user):
    return world.owner if user is OWNER else world.editor if user is EDITOR else user


def _checks(http, world, user=OWNER, version=None):
    return http(_who(world, user)).get(f"/ext/versions/{(version or world.v2).id}/fix-checks")


def _put(http, world, comment, status, note=None, user=EDITOR, version=None):
    return http(_who(world, user)).put(
        f"/ext/versions/{(version or world.v2).id}/fix-checks/{comment.id}",
        json={"status": status, "note": note},
    )


def _events(live_db, kind):
    return [e.payload for e in live_db.query(ExtEvent).filter(ExtEvent.type == kind).order_by(ExtEvent.seq)]


def test_the_list_holds_the_earlier_top_level_comments_both_kinds(http, world):
    r = _checks(http, world)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["previous_version_id"] == str(world.v1.id)
    assert [i["comment_id"] for i in body["items"]] == [str(world.c_client.id), str(world.c_internal.id)], \
        "client and internal comments of v1, in video order; not the reply, not v2's own comment"
    assert (body["total"], body["checked"]) == (2, 0)
    internal = body["items"][1]
    assert internal["visibility"] == "internal" and internal["drawing"] == {"marker": "INTERNAL-DRAWING"}
    assert internal["version_number"] == 1 and internal["timecode_start"] == 4.0
    assert body["items"][0]["author_name"] == "Rita Client"


def test_v1_has_nothing_to_check(http, world):
    r = _checks(http, world, version=world.v1)
    assert r.json()["total"] == 0


def test_clients_and_outsiders_are_told_it_does_not_exist(http, world):
    for user in (world.reviewer, world.viewer, world.outsider, None):
        r = _checks(http, world, user=user)
        assert r.status_code in (401, 404), (user and user.name, r.status_code)
        assert INTERNAL_NOTE not in r.text
        w = _put(http, world, world.c_client, "fixed", user=user)
        assert w.status_code in (401, 404), (user and user.name, w.status_code)


def test_publish_waits_for_the_check_then_resolves_what_was_fixed(http, world, live_db):
    refused = http(world.owner).post(f"/ext/versions/{world.v2.id}/publish")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "fix_check_incomplete"
    assert refused.json()["detail"]["unchecked"] == 2
    assert world.v2.visibility == "internal"

    ok = _put(http, world, world.c_client, "fixed", note="new cut at 0:02")
    assert ok.status_code == 200, ok.text
    assert (ok.json()["status"], ok.json()["note"], ok.json()["checked_by_name"]) == ("fixed", "new cut at 0:02", "Eddie Editor")
    assert _put(http, world, world.c_internal, "wont_fix", note="keeping it").status_code == 200
    assert _checks(http, world).json()["checked"] == 2
    assert [p["status"] for p in _events(live_db, "fix_check.updated")] == ["fixed", "wont_fix"]
    assert world.c_client.resolved is False, "nothing is resolved before the client sees the new version"

    published = http(world.owner).post(f"/ext/versions/{world.v2.id}/publish")
    assert published.status_code == 200, published.text
    live_db.refresh(world.c_client)
    live_db.refresh(world.c_internal)
    assert world.c_client.resolved is True, "fixed: resolved with the publish"
    assert world.c_internal.resolved is True, "won't fix, internal: resolved"
    payload = _events(live_db, "version.published")[-1]
    assert sorted(payload["resolved_comment_ids"]) == sorted([str(world.c_client.id), str(world.c_internal.id)])
    assert payload["unchecked_comments"] == 0

    closed = _put(http, world, world.c_client, "not_fixed", user=world.owner)
    assert closed.status_code == 409, "a published version's check is closed"


def test_wont_fix_on_a_client_comment_leaves_it_open_for_a_reply(http, world, live_db):
    _put(http, world, world.c_client, "wont_fix")
    _put(http, world, world.c_internal, "fixed")
    assert http(world.owner).post(f"/ext/versions/{world.v2.id}/publish").status_code == 200
    live_db.refresh(world.c_client)
    assert world.c_client.resolved is False


def test_the_owner_can_publish_anyway(http, world, live_db):
    _put(http, world, world.c_client, "fixed")
    r = http(world.owner).post(f"/ext/versions/{world.v2.id}/publish?force=true")
    assert r.status_code == 200, r.text
    assert _events(live_db, "version.published")[-1]["unchecked_comments"] == 1
    live_db.refresh(world.c_client)
    live_db.refresh(world.c_internal)
    assert (world.c_client.resolved, world.c_internal.resolved) == (True, False), "unchecked stays open"


def test_only_comments_on_the_list_can_be_checked(http, world):
    assert _put(http, world, world.c_v2, "fixed").status_code == 404, "v2's own comment is not an earlier one"
    assert _put(http, world, world.c_internal_reply, "fixed").status_code == 404, "replies go with their comment"
    assert _put(http, world, world.c_client, "maybe").status_code == 422


def test_clearing_a_check(http, world, live_db):
    _put(http, world, world.c_client, "fixed")
    r = _put(http, world, world.c_client, None)
    assert r.status_code == 200 and r.json()["status"] is None
    assert live_db.query(FixCheck).count() == 0


def test_not_fixed_comes_back_on_the_next_version_fixed_does_not(http, world, seed, live_db):
    _put(http, world, world.c_client, "not_fixed", note="still too long")
    _put(http, world, world.c_internal, "fixed")
    v3, _ = seed.version(world.asset, world.editor, number=3, s3_keys=True)
    v3.visibility = "internal"
    live_db.flush()
    items = _checks(http, world, version=v3).json()["items"]
    ids = [i["comment_id"] for i in items]
    assert str(world.c_client.id) in ids and str(world.c_internal.id) not in ids, \
        "found not fixed on v2: checked again on v3; fixed on v2 (not yet published): not again"
    assert str(world.c_v2.id) in ids, "v2's own comment is an earlier one for v3"
    client = next(i for i in items if i["comment_id"] == str(world.c_client.id))
    assert client["not_fixed_in"] == 2

    # v2 never goes out; v3 does, and takes v2's findings with it
    _put(http, world, world.c_client, "fixed", version=v3)
    _put(http, world, world.c_v2, "fixed", version=v3)
    assert http(world.owner).post(f"/ext/versions/{v3.id}/publish").status_code == 200
    for c in (world.c_client, world.c_internal, world.c_v2):
        live_db.refresh(c)
    assert (world.c_client.resolved, world.c_internal.resolved, world.c_v2.resolved) == (True, True, True)


def test_the_gateway_can_read_but_not_decide(http, world, live_db):
    from apps.api.ext import auth as ext_auth
    _, token = ext_auth.create_token(live_db, "garlomedia-gateway", world.admin.id)
    live_db.flush()
    gw = http(token=token)
    assert gw.get(f"/ext/versions/{world.v2.id}/fix-checks").status_code == 200
    r = gw.put(f"/ext/versions/{world.v2.id}/fix-checks/{world.c_client.id}", json={"status": "fixed"})
    assert r.status_code == 403
    assert live_db.query(FixCheck).count() == 0
