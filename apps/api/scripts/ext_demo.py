"""Walk the review-task flow against a running FreeFrame (Garlomedia fork).

    comment → task → proposed version → approve → publish

Run it against a development or staging instance, never production: it creates
a comment, a task and a version on the asset you point it at.

    python apps/api/scripts/ext_demo.py \\
        --base-url http://localhost:8000 \\
        --owner-token <access token of a project owner> \\
        --gateway-token ffgw_... \\
        --asset-id <uuid of a video in that project> \\
        --file some-480p-render.mp4 \\
        [--client-token <access token of a reviewer on the project>]

With `--client-token` it also prints what the client sees after each step, which
is the point of the exercise: nothing internal until "Publish to client".

Behind the production proxy the API is under /api, so there the base URL is
https://<host>/api. Tokens are read from the arguments and never printed.
"""
import argparse
import mimetypes
import os
import sys
import time

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--owner-token", required=True)
    parser.add_argument("--gateway-token", required=True)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--file", required=True, help="the new version's video file")
    parser.add_argument("--client-token")
    parser.add_argument("--timeout", type=int, default=300, help="seconds to wait for the transcode")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")

    def client(token):
        return httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=30)

    owner, gateway = client(args.owner_token), client(args.gateway_token)
    reviewer = client(args.client_token) if args.client_token else None
    asset_id = args.asset_id
    step = 0

    def say(text):
        nonlocal step
        step += 1
        print(f"\n{step}. {text}")

    def ok(response, expected=(200, 201, 204)):
        if response.status_code not in expected:
            print(f"   FAILED: {response.request.method} {response.request.url.path} → "
                  f"{response.status_code} {response.text[:300]}")
            raise SystemExit(1)
        return response.json() if response.content else None

    def client_view(label):
        if reviewer is None:
            return
        versions = ok(reviewer.get(f"/assets/{asset_id}/versions"))
        latest = max(versions, key=lambda v: v["version_number"]) if versions else None
        comments = ok(reviewer.get(f"/assets/{asset_id}/comments", params={"version_id": latest["id"]})) if latest else []
        tasks = reviewer.get("/ext/tasks", params={"asset_id": asset_id})
        print(f"   the client sees ({label}): versions {sorted(v['version_number'] for v in versions)}, "
              f"{len(comments)} comment(s) on the newest, tasks: {tasks.json() if tasks.status_code == 200 else tasks.status_code}")

    since = ok(gateway.get("/ext/events", params={"since": 0, "limit": 1000}))["last_seq"]
    me = ok(owner.get(f"/ext/assets/{asset_id}/context"))
    if not me["is_manager"]:
        print("--owner-token is not a manager (project owner or admin) on this asset's project")
        return 1
    versions = ok(owner.get(f"/assets/{asset_id}/versions"))
    ready = [v for v in versions if v["processing_status"] == "ready"]
    if not ready:
        print("The asset has no ready version to comment on")
        return 1
    current = max(ready, key=lambda v: v["version_number"])
    author_id = current["created_by"]

    say("A client-visible comment on the current version")
    comment = ok(owner.post(f"/assets/{asset_id}/comments", json={
        "version_id": current["id"], "body": "The intro is too long, trim the pause.",
        "timecode_start": 1.0, "visibility": "public",
    }))
    print(f"   comment {comment['id']} ({comment['visibility']}) on v{current['version_number']}")
    client_view("after the comment")

    say("The owner turns it into a task for any editor on the project")
    task = ok(owner.post("/ext/tasks", json={
        "asset_id": asset_id, "source_comment_id": comment["id"], "kind": "cut_trim",
        "stage": "review", "assignee_type": "role_pool", "assignee_role": "editor",
    }))
    print(f"   task {task['id']}: {task['status']}, frames {task['frame_in']}–{task['frame_out']}, "
          f"“{task['instruction']}”")

    say("The gateway hands it to a machine and reports progress")
    for status, extra in (("claimed", {"claimed_by": "demo editor on demo-pc"}), ("in_progress", {})):
        task = ok(gateway.post(f"/ext/tasks/{task['id']}/status", json={"status": status, **extra}))
        print(f"   → {task['status']}" + (f" ({task['claimed_by']})" if task.get("claimed_by") else ""))
    refused = gateway.post(f"/ext/tasks/{task['id']}/approve", json={})
    print(f"   the gateway trying to approve: {refused.status_code} (it must be refused)")
    if refused.status_code != 403:
        return 1

    say("The gateway uploads the new version through one presigned URL")
    size = os.path.getsize(args.file)
    mime = mimetypes.guess_type(args.file)[0] or "video/mp4"
    created = ok(gateway.post(f"/ext/assets/{asset_id}/versions", json={
        "original_filename": os.path.basename(args.file), "mime_type": mime, "file_size_bytes": size,
        "author_person_id": author_id, "author_type": "person",
        "resolve_project": "DEMO_Project", "resolve_timeline": f"demo v{current['version_number'] + 1}",
        "start_timecode": "01:00:00:00", "fps": 25, "resolves_task_ids": [task["id"]],
    }))
    with open(args.file, "rb") as handle:
        put = httpx.put(created["upload_url"], content=handle.read(), headers={"Content-Type": mime}, timeout=300)
    if put.status_code not in (200, 201):
        print(f"   FAILED: PUT to storage → {put.status_code} {put.text[:200]}")
        return 1
    finalized = ok(gateway.post(f"/ext/versions/{created['version_id']}/finalize"))
    print(f"   v{created['version_number']} uploaded ({size} bytes), {finalized['status']}")
    deadline = time.time() + args.timeout
    while True:
        status = next(v["processing_status"] for v in ok(owner.get(f"/assets/{asset_id}/versions"))
                      if v["id"] == created["version_id"])
        if status in ("ready", "failed") or time.time() > deadline:
            break
        time.sleep(2)
    print(f"   transcode: {status}")
    if status != "ready":
        return 1
    client_view("the new version exists, unpublished")

    say("The gateway proposes the version as the task's result, with the editor's note")
    task = ok(gateway.post(f"/ext/tasks/{task['id']}/status", json={
        "status": "proposed", "result_version_id": created["version_id"],
        "note": "Trimmed 12 frames off the intro.", "note_author_type": "person",
        "note_author_person_id": author_id,
    }))
    print(f"   → {task['status']}, result v{created['version_number']}")

    say("The owner approves the task")
    task = ok(owner.post(f"/ext/tasks/{task['id']}/approve", json={"note": "Good."}))
    notes = ok(owner.get(f"/ext/tasks/{task['id']}/notes"))
    print(f"   → {task['status']}; {len(notes)} internal note(s) on the task")
    client_view("approved, still unpublished")

    say("The owner publishes the version to the client")
    published = ok(owner.post(f"/ext/versions/{created['version_id']}/publish"))
    print(f"   v{created['version_number']} is now {published['visibility']}")
    client_view("after publishing")

    say("What the gateway was told, in order")
    events = ok(gateway.get("/ext/events", params={"since": since, "limit": 1000}))["events"]
    for event in events:
        detail = event["payload"].get("to") or event["payload"].get("visibility") or ""
        print(f"   #{event['seq']} {event['type']} {detail}")
    expected = [
        "comment.created", "task.created", "task.status_changed", "task.status_changed",
        "version.created", "task_note.created", "task.status_changed",
        "task_note.created", "task.status_changed", "version.published",
    ]
    got = [e["type"] for e in events]
    if got != expected:
        print(f"\nUnexpected event sequence.\n   expected {expected}\n   got      {got}")
        return 1
    print("\nDone: comment → task → proposed version → approved → published.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
