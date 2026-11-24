# Review tasks, client / internal visibility, events (Garlomedia fork)

This is the fork's own feature, not upstream FreeFrame's. It is phase 1 of
Garlomedia's review-driven editing: a review comment becomes a **task**, the
task is done by a person or an agent, comes back as a **version**, and is
approved in FreeFrame. FreeFrame only holds the review side. Handing tasks to
machines, DaVinci Resolve and proxies belong to the gateway and Garlo Worker.

All of it lives in `apps/api/ext` and `apps/web/components/ext`. The upstream
files it touches are listed at the end, so a merge from upstream knows where to
look.

## Who sees what

| Tier | Who | Sees |
| --- | --- | --- |
| Manager | project **owner**, instance admin | everything; may publish a version, make a comment client-visible, approve, reject and cancel tasks |
| Team | project **editor** | everything internal; may create tasks and write notes; cannot show anything to the client |
| Client | project **reviewer** and **viewer**, a direct share, a public project's visitors, every share link, guests | only `public` comments and versions |

A comment or a version is `public` (the client may see it) or `internal`.
Tasks and their notes are always internal; a note has no visibility column, so
there is nothing that could make one client-visible.

Defaults:

- A client's comment is `public`.
- A manager's comment is `internal` unless they choose `public`.
- An editor's comment is always `internal`. A request for `public` is stored as
  `internal`, not refused.
- A reply under an internal comment is `internal`, whoever writes it.
- A version uploaded by an editor or by the gateway is `internal` until a
  manager presses **Publish to client**. A manager's own upload is `public`,
  as it was before.
- A share link shows the client's view to everyone, the owner included.

### How it is enforced

Not per endpoint. Every request's database session carries a *viewer*, and one
SQLAlchemy `do_orm_execute` listener (`ext/visibility.py`) adds the visibility
condition to every ORM `SELECT` on `comments` and `asset_versions`. An endpoint
that forgets to filter still cannot return an internal row to a client. A
request session starts as "public only" (`database.get_db`), signing in binds
the user (`middleware/auth.py`), and a share link puts it back to "public only"
(`permissions.validate_share_link`). Celery tasks and scripts are not filtered.

Live events about internal rows go on a second Redis channel,
`project:<id>:internal`, which only the team's streams subscribe to.

`apps/api/tests/test_ext_visibility.py` reads every client-reachable endpoint
as a reviewer, a viewer, a public-project visitor and through a share link, and
fails if anything internal appears. Switching the listener off makes 64 of
those tests fail.

## Tasks

```
open → claimed → in_progress → proposed → approved
                                  └─ rejected: back to open, with the manager's note
open / claimed / in_progress / proposed ⇄ needs_info   (a question, with a note)
anything not finished → cancelled                       (a manager)
```

- The **gateway** may set `claimed`, `in_progress`, `proposed`, `needs_info`,
  and back to `open` when a lease runs out.
- Only a **manager** approves, rejects or cancels.
- An **agent** can be assigned at the `radio`, `rough` and `review` stages only.
  `selects`, `fine`, `finish` and `deliver` are for people.
- Nothing is assigned on a version a manager has approved.
- A task under way cannot be reassigned; the gateway returns it to `open` first.

Kinds: `cut_trim`, `b_roll`, `music`, `sfx`, `caption_title`, `mograph`,
`audio`, `colour`, `question`, `global`. Frames are on the version the task was
created on; equal in and out is a point, both empty is the whole video.

## API

Served under `/api/ext` in production. Full schemas are in FastAPI's `/docs`
under "ext: review tasks". "Team" means owner, editor or admin on the project.

| Method and path | Who | What |
| --- | --- | --- |
| `GET /ext/assets/{id}/context` | anyone with access | what the caller may do on this asset |
| `POST /ext/tasks` | team | create, from a comment (`source_comment_id`) or new |
| `GET /ext/tasks` | team, gateway | filters: `project_id`, `asset_id`, `version_id`, `status` (comma-separated), `stage`, `assignee_type`, `assignee_person_id` |
| `GET /ext/tasks/{id}` | team, gateway | |
| `GET /ext/tasks/{id}/context` | team, gateway | the task with its project, video, version (fps, Resolve timeline), source comment and notes |
| `GET /ext/people?ids=&emails=` | gateway, admin | match FreeFrame accounts with the gateway's people; nothing is listed without a key |
| `PUT /ext/projects/{id}/members` | gateway (`members:write`) | `{email, name?, role: editor\|reviewer\|viewer}`: put a person on the project; makes a waiting account if there is none (their first sign-in opens it); never changes an owner; sends no email |
| `DELETE /ext/projects/{id}/members?email=` | gateway (`members:write`) | take a person off the project; an owner is refused (409) |
| `PATCH /ext/tasks/{id}` | team | instruction, kind, stage, frames, assignee |
| `POST /ext/tasks/{id}/status` | gateway, manager | move a task; `result_version_id` is required for `proposed`, a `note` for `needs_info` |
| `POST /ext/tasks/{id}/approve` | manager | |
| `POST /ext/tasks/{id}/reject` | manager | needs a `note` |
| `GET` / `POST /ext/tasks/{id}/notes` | team, gateway | the internal thread |
| `GET /ext/versions/{id}/meta` | team, gateway | |
| `PUT /ext/versions/{id}/meta` | gateway | Resolve project and timeline, start timecode, fps, author, tasks resolved |
| `POST /ext/versions/{id}/publish` | manager | publish to client |
| `PATCH /ext/comments/{id}/visibility` | manager | `public` or `internal`; hiding a comment hides its replies |
| `GET /ext/events?since=&limit=` | gateway, admin | catch-up |
| `POST /ext/assets/{id}/versions` | gateway | start an internal version, returns one presigned PUT URL |
| `POST /ext/versions/{id}/finalize` | gateway | the file is in storage: start the transcode |
| `POST` / `GET` / `DELETE /ext/gateway-tokens` | admin | create (shown once), list, revoke |

A client asking for a task, a note or version metadata is answered `404`, as if
it did not exist.

## The gateway account

An instance admin creates a token (`POST /ext/gateway-tokens`); it starts with
`ffgw_`, is shown once, and only its SHA-256 is stored. The gateway sends it as
`Authorization: Bearer ffgw_…`. It works on `/ext` only.

Scopes: `read`, `versions:write`, `notes:write`, `tasks:status`. No scope lets
the gateway approve, reject, cancel, publish, change a comment's visibility,
create or edit a task, or manage tokens; those routes refuse a gateway token.

Upload: `POST /ext/assets/{id}/versions` with the file's name, type and size,
the `author_person_id` (an owner or editor of the project: the editor, or the
person an agent worked for) and the Resolve metadata. `PUT` the file to the
returned `upload_url` with the same `Content-Type`, then call `finalize`. One
PUT per file, so at most 5 GiB. Storage keys never leave the server.

## Events

Every change writes one row to `ext_events` in the same database transaction,
so there is an event exactly when the change committed. `seq` rises in commit
order.

| Type | When |
| --- | --- |
| `task.created`, `task.updated`, `task.status_changed` | `status_changed` carries `from`, `to`, `by` and `rejected` |
| `task_note.created` | |
| `comment.created`, `comment.visibility_changed` | |
| `version.created` | an upload was completed, by a person or the gateway |
| `version.published` | |

**Webhook.** Set `EXT_GATEWAY_WEBHOOK_URL` and `EXT_GATEWAY_WEBHOOK_SECRET`.
Each event is one `POST` with the JSON body
`{"seq", "type", "project_id", "payload", "created_at"}` and the headers
`X-FreeFrame-Signature: sha256=<HMAC-SHA256 of the body with the secret>`,
`X-FreeFrame-Event` and `X-FreeFrame-Seq`. Events are sent in `seq` order; the
next is not sent until the gateway answers 2xx. A failure is retried after 5 s,
doubling to 15 minutes, and given up on after 12 attempts (it stays readable
through the catch-up endpoint). The receiver should verify the signature on the
raw body and ignore a `seq` it has already processed.

**Catch-up.** `GET /ext/events?since=<last seq seen>` returns what came after,
in order, with `last_seq` to pass next time.

Delivery runs on the `email_high` Celery queue, so no new worker is needed, and
from the beat schedule every minute as the safety net.

## Walk-through

`apps/api/scripts/ext_demo.py` walks comment → task → proposed version →
approve → publish against a running instance, as the owner and as the gateway.

## Upstream files this touches

Each edit is a call into `apps/api/ext` or `components/ext`, marked `Fork` in
the source.

| File | Why |
| --- | --- |
| `apps/api/database.py` | a request session starts as a public-only viewer |
| `apps/api/middleware/auth.py` | signing in binds the user as the session's viewer |
| `apps/api/services/permissions.py` | a share link puts the session back to public-only |
| `apps/api/models/asset.py`, `schemas/asset.py` | `asset_versions.visibility` |
| `apps/api/models/__init__.py` | registers the ext tables |
| `apps/api/schemas/comment.py` | `visibility` left out means "by role", not "public" |
| `apps/api/routers/comments.py` | visibility by role; no notification or mention to a client about an internal comment; `comment.created` events; internal live channel |
| `apps/api/routers/upload.py`, `routers/assets.py` | visibility of a new version; `version.created` event |
| `apps/api/routers/notifications.py` | a notification whose comment the caller cannot read is not listed |
| `apps/api/services/versions.py` | the next version number counts internal versions for every caller |
| `apps/api/services/event_service.py`, `routers/events.py` | the team's streams also listen on the internal channel |
| `apps/api/services/verdicts.py`, `tasks/transcode_tasks.py` | events about an internal version go on the internal channel |
| `apps/api/tasks/celery_app.py` | registers, routes and schedules webhook delivery |
| `apps/api/config.py`, `.env.example` | the two webhook settings |
| `apps/api/main.py` | mounts the `/ext` router |
| `apps/api/tests/test_cleanup_soft_deleted.py` | lists the ext foreign keys, which delete by `ON DELETE` rules |
| `apps/api/tests/test_share_comment_scope.py`, `test_share_comment_replies.py`, `test_superadmin_access.py` | one test each adjusted to the tiers above |
| `apps/web/types/index.ts` | `AssetVersion.visibility` |
| `apps/web/hooks/use-sse.ts` | three ext event types |
| `apps/web/components/review/comment-input.tsx` | default visibility by role; an editor has no toggle |
| `apps/web/components/review/comment-panel.tsx` | mounts the comment actions |
| `apps/web/components/review/version-switcher.tsx` | lock on an internal version |
| `apps/web/app/(dashboard)/projects/[id]/assets/[assetId]/page.tsx` | mounts the provider, the task panel, the dialog and Publish to client |

Migration: `apps/api/alembic/versions/e1x7a2b3c4d5_ext_review_tasks_phase1.py`.
Existing versions become `public`. Existing comments keep their visibility,
and a reply stored `public` under an internal comment becomes `internal`.
