"""The /ext API: review tasks, notes, version metadata and publishing, the event
catch-up endpoint, the gateway's upload path and its tokens.

Served as /api/ext/… behind the production proxy, like every other route.

Every write commits once and then dispatches webhook delivery; the events were
added to the same transaction by the functions in `tasks.py` and `events.py`.
"""
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from ..database import get_db
from ..models.asset import Asset, AssetVersion, FileType, MediaFile, ProcessingStatus, AssetType
from ..models.comment import Comment
from ..models.project import Project, ProjectMember
from ..models.user import User
from ..services import event_service, s3_service
from ..services.permissions import require_asset_access, require_live_project
from ..services.storage import upload_guard_error
from ..services.versions import next_version_number
from . import auth, events, feed, fixcheck, live, tasks, visibility
from .auth import Actor, get_actor
from .models import (
    ExtEvent, GatewayToken, ReviewTask, ReviewTaskNote, TaskStatus, VersionExt, VersionResolvedTask,
)
from .schemas import (
    CommentVisibilityChange, EventsPage, FixCheckItem, FixChecklist, FixCheckPut, GatewayTokenCreate, GatewayTokenCreated,
    GatewayTokenResponse, GatewayVersionCreate, GatewayVersionCreated, MemberPut, MemberResult, NoteCreate, NoteResponse,
    PersonResponse, ReviewContext, TaskContext, TaskCreate, TaskDecision, TaskResponse,
    TaskStatusChange, TaskUpdate, VersionFinalized, VersionMeta, VersionMetaResponse,
)

router = APIRouter(prefix="/ext", tags=["ext: review tasks"])

UPLOAD_URL_SECONDS = 3600
# One PUT per file. S3 and R2 both cap a single PUT at 5 GiB.
SINGLE_PUT_MAX_BYTES = 5 * 1024 ** 3


# ── Access ─────────────────────────────────────────────────────────────────────

def _live_asset(db: Session, asset_id: uuid.UUID) -> Asset:
    asset = db.query(Asset).filter(Asset.id == asset_id, Asset.deleted_at.is_(None)).first()
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    require_live_project(db, asset.project_id)
    return asset


def _require_internal(db: Session, project_id: uuid.UUID, actor: Actor) -> None:
    """Tasks, notes and version metadata are the team's. The gateway reads them
    with the `read` scope; a person needs the project's internal tier. A client
    is answered as if the thing did not exist."""
    if actor.is_gateway:
        auth.require_scope(actor, auth.SCOPE_READ)
        return
    if not visibility.sees_internal(db, project_id, actor.user):
        raise HTTPException(status_code=404, detail="Not found")


def _require_manager(db: Session, project_id: uuid.UUID, actor: Actor) -> User:
    user = auth.require_user(actor)
    if not visibility.sees_internal(db, project_id, user):
        raise HTTPException(status_code=404, detail="Not found")
    if not visibility.is_manager(db, project_id, user):
        raise HTTPException(status_code=403, detail="Only a project owner or an admin can do this")
    return user


def _task(db: Session, task_id: uuid.UUID, actor: Actor) -> ReviewTask:
    task = db.get(ReviewTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not found")
    _require_internal(db, task.project_id, actor)
    require_live_project(db, task.project_id)
    return task


def _done(db: Session, project_id=None) -> None:
    """Commit the change with its events, then tell the gateway and the team."""
    db.commit()
    events.dispatch()
    if project_id is not None:
        event_service.publish_sync(live.channel_key(project_id, internal=True), "review_tasks_changed", {})


# ── What the review screen needs to know ───────────────────────────────────────

@router.get("/assets/{asset_id}/context", response_model=ReviewContext)
def review_context(asset_id: uuid.UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    user = auth.require_user(actor)
    asset = _live_asset(db, asset_id)
    require_asset_access(db, asset, user)
    internal = visibility.sees_internal(db, asset.project_id, user)
    manager = visibility.is_manager(db, asset.project_id, user)
    return ReviewContext(
        sees_internal=internal,
        is_manager=manager,
        default_comment_visibility=visibility.INTERNAL if internal else visibility.PUBLIC,
        can_choose_comment_visibility=manager,
    )


# ── Tasks ──────────────────────────────────────────────────────────────────────

@router.post("/tasks", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
def create_task(body: TaskCreate, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    user = auth.require_user(actor)
    asset = _live_asset(db, body.asset_id)
    _require_internal(db, asset.project_id, actor)
    task = tasks.create_task(db, asset, body, user)
    _done(db, task.project_id)
    return task


@router.get("/tasks", response_model=list[TaskResponse])
def list_tasks(
    project_id: Optional[uuid.UUID] = None,
    asset_id: Optional[uuid.UUID] = None,
    version_id: Optional[uuid.UUID] = Query(default=None, description="Created on, or resolved by, this version"),
    status_: Optional[str] = Query(default=None, alias="status"),
    stage: Optional[str] = None,
    assignee_type: Optional[str] = None,
    assignee_person_id: Optional[uuid.UUID] = None,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    query = db.query(ReviewTask)
    if actor.is_gateway:
        auth.require_scope(actor, auth.SCOPE_READ)
    elif not visibility.is_admin(actor.user):
        # Only projects where the caller is on the internal tier. Filtering
        # rather than refusing, so one list serves "my tasks" across projects.
        query = query.filter(ReviewTask.project_id.in_(
            select(ProjectMember.project_id).where(
                ProjectMember.user_id == actor.user.id,
                ProjectMember.deleted_at.is_(None),
                ProjectMember.role.in_(visibility.INTERNAL_ROLES),
            )
        ))
    if project_id is not None:
        query = query.filter(ReviewTask.project_id == project_id)
    if asset_id is not None:
        query = query.filter(ReviewTask.asset_id == asset_id)
    if version_id is not None:
        query = query.filter(
            (ReviewTask.created_on_version_id == version_id) | (ReviewTask.result_version_id == version_id)
        )
    if status_ is not None:
        query = query.filter(ReviewTask.status.in_([s.strip() for s in status_.split(",") if s.strip()]))
    if stage is not None:
        query = query.filter(ReviewTask.stage == stage)
    if assignee_type is not None:
        query = query.filter(ReviewTask.assignee_type == assignee_type)
    if assignee_person_id is not None:
        query = query.filter(ReviewTask.assignee_person_id == assignee_person_id)
    return query.order_by(ReviewTask.created_at, ReviewTask.id).offset(offset).limit(limit).all()


@router.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task(task_id: uuid.UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    return _task(db, task_id, actor)


@router.get("/tasks/{task_id}/context", response_model=TaskContext)
def get_task_context(task_id: uuid.UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    """The task with what surrounds it: the video, the version it was made on,
    the comment it came from and its notes. What the gateway hands a machine."""
    task = _task(db, task_id, actor)
    asset = db.query(Asset).filter(Asset.id == task.asset_id).first()
    project = db.query(Project).filter(Project.id == task.project_id).first()
    version = None
    if task.created_on_version_id is not None:
        row = db.query(AssetVersion).filter(AssetVersion.id == task.created_on_version_id).first()
        if row is not None:
            media = db.query(MediaFile).filter(MediaFile.version_id == row.id).first()
            ext = db.get(VersionExt, row.id)
            version = {
                "id": row.id, "version_number": row.version_number, "visibility": row.visibility,
                "fps": media.fps if media is not None else None,
                "duration_seconds": media.duration_seconds if media is not None else None,
                "resolve_project": ext.resolve_project if ext is not None else None,
                "resolve_timeline": ext.resolve_timeline if ext is not None else None,
                "start_timecode": ext.start_timecode if ext is not None else None,
            }
    source = None
    if task.source_comment_id is not None:
        comment = db.query(Comment).filter(Comment.id == task.source_comment_id).first()
        if comment is not None:
            author = db.query(User).filter(User.id == comment.author_id).first() if comment.author_id else None
            source = {
                "id": comment.id, "body": comment.body, "visibility": comment.visibility,
                "timecode_start": comment.timecode_start, "timecode_end": comment.timecode_end,
                "author_name": author.name if author is not None else "Guest",
            }

    def person(user_id):
        return db.query(User).filter(User.id == user_id).first() if user_id is not None else None

    return TaskContext(
        task=task,
        project={"id": task.project_id, "name": project.name if project is not None else None},
        asset={"id": task.asset_id, "name": asset.name if asset is not None else None,
               "asset_type": asset.asset_type.value if asset is not None else None},
        version=version,
        source_comment=source,
        notes=db.query(ReviewTaskNote).filter(ReviewTaskNote.task_id == task.id).order_by(
            ReviewTaskNote.created_at, ReviewTaskNote.id).all(),
        assignee=person(task.assignee_person_id),
        created_by=person(task.created_by),
    )


@router.patch("/tasks/{task_id}", response_model=TaskResponse)
def update_task(task_id: uuid.UUID, body: TaskUpdate, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    auth.require_user(actor)
    task = _task(db, task_id, actor)
    tasks.update_task(db, task, body)
    _done(db, task.project_id)
    return task


@router.post("/tasks/{task_id}/status", response_model=TaskResponse)
def change_task_status(
    task_id: uuid.UUID, body: TaskStatusChange, db: Session = Depends(get_db), actor: Actor = Depends(get_actor),
):
    task = _task(db, task_id, actor)
    if actor.is_gateway:
        auth.require_scope(actor, auth.SCOPE_TASK_STATUS)
        who = tasks.GATEWAY
        note_author = dict(
            note_author_type=body.note_author_type.value,
            note_author_person_id=body.note_author_person_id,
            note_author_label=body.note_author_label or actor.label,
        )
    else:
        # A person changes status here only as a manager. Editors' work moves
        # through the worker, which is the gateway.
        user = _require_manager(db, task.project_id, actor)
        who = tasks.MANAGER
        note_author = dict(note_author_person_id=user.id)
    tasks.change_status(
        db, task, body.status.value, who,
        result_version_id=body.result_version_id, claimed_by=body.claimed_by,
        note=body.note, **note_author,
    )
    _done(db, task.project_id)
    return task


@router.post("/tasks/{task_id}/approve", response_model=TaskResponse)
def approve_task(task_id: uuid.UUID, body: TaskDecision, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    task = _task(db, task_id, actor)
    user = _require_manager(db, task.project_id, actor)
    tasks.change_status(
        db, task, TaskStatus.approved.value, tasks.MANAGER, note=body.note, note_author_person_id=user.id,
    )
    _done(db, task.project_id)
    return task


@router.post("/tasks/{task_id}/reject", response_model=TaskResponse)
def reject_task(task_id: uuid.UUID, body: TaskDecision, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    task = _task(db, task_id, actor)
    user = _require_manager(db, task.project_id, actor)
    if task.status != TaskStatus.proposed.value:
        raise HTTPException(status_code=409, detail="Only a proposed task can be rejected")
    tasks.change_status(
        db, task, TaskStatus.open.value, tasks.MANAGER, note=body.note, note_author_person_id=user.id,
    )
    _done(db, task.project_id)
    return task


# ── Notes: internal, always. There is no route that shows one to a client. ─────

@router.get("/tasks/{task_id}/notes", response_model=list[NoteResponse])
def list_notes(task_id: uuid.UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    task = _task(db, task_id, actor)
    return db.query(ReviewTaskNote).filter(ReviewTaskNote.task_id == task.id).order_by(
        ReviewTaskNote.created_at, ReviewTaskNote.id,
    ).all()


@router.post("/tasks/{task_id}/notes", response_model=NoteResponse, status_code=status.HTTP_201_CREATED)
def add_note(task_id: uuid.UUID, body: NoteCreate, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    task = _task(db, task_id, actor)
    if actor.is_gateway:
        auth.require_scope(actor, auth.SCOPE_NOTES)
        author = dict(
            author_type=body.author_type.value, author_person_id=body.author_person_id,
            author_label=body.author_label or actor.label,
        )
    else:
        # A signed-in person is themselves, whatever the body says.
        author = dict(author_person_id=actor.user.id)
    note = tasks.add_note(db, task, kind=body.kind.value, body=body.body, **author)
    _done(db, task.project_id)
    return note


# ── Versions: metadata and publishing ──────────────────────────────────────────

def _version(db: Session, version_id: uuid.UUID) -> tuple[AssetVersion, Asset]:
    version = db.query(AssetVersion).filter(
        AssetVersion.id == version_id, AssetVersion.deleted_at.is_(None),
    ).first()
    if version is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return version, _live_asset(db, version.asset_id)


def _meta_response(db: Session, version: AssetVersion, ext: Optional[VersionExt]) -> VersionMetaResponse:
    task_ids = [row[0] for row in db.query(VersionResolvedTask.task_id).filter(
        VersionResolvedTask.version_id == version.id,
    ).all()]
    fields = {} if ext is None else {
        name: getattr(ext, name) for name in (
            "resolve_project", "resolve_timeline", "start_timecode", "fps", "author_type",
            "author_person_id", "agent_machine", "published_by", "published_at",
        )
    }
    return VersionMetaResponse(
        version_id=version.id, visibility=version.visibility, resolves_task_ids=task_ids, **fields,
    )


def _write_meta(db: Session, version: AssetVersion, asset: Asset, body: VersionMeta) -> VersionExt:
    ext = db.get(VersionExt, version.id)
    if ext is None:
        ext = VersionExt(version_id=version.id)
        db.add(ext)
    for name in ("resolve_project", "resolve_timeline", "start_timecode", "fps", "agent_machine"):
        setattr(ext, name, getattr(body, name))
    ext.author_type = body.author_type.value
    ext.author_person_id = body.author_person_id
    wanted = set(body.resolves_task_ids)
    if wanted:
        found = {row[0] for row in db.query(ReviewTask.id).filter(
            ReviewTask.id.in_(wanted), ReviewTask.asset_id == asset.id,
        ).all()}
        if found != wanted:
            raise HTTPException(status_code=422, detail="resolves_task_ids holds a task that is not on this asset")
    have = {row[0] for row in db.query(VersionResolvedTask.task_id).filter(
        VersionResolvedTask.version_id == version.id,
    ).all()}
    for task_id in wanted - have:
        db.add(VersionResolvedTask(version_id=version.id, task_id=task_id))
    if have - wanted:
        db.query(VersionResolvedTask).filter(
            VersionResolvedTask.version_id == version.id,
            VersionResolvedTask.task_id.in_(have - wanted),
        ).delete(synchronize_session=False)
    db.flush()
    return ext


@router.get("/versions/{version_id}/meta", response_model=VersionMetaResponse)
def get_version_meta(version_id: uuid.UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    version, asset = _version(db, version_id)
    _require_internal(db, asset.project_id, actor)
    return _meta_response(db, version, db.get(VersionExt, version.id))


@router.put("/versions/{version_id}/meta", response_model=VersionMetaResponse)
def put_version_meta(
    version_id: uuid.UUID, body: VersionMeta, db: Session = Depends(get_db), actor: Actor = Depends(get_actor),
):
    """The gateway's record of where a version came from. Gateway only."""
    if not actor.is_gateway:
        raise HTTPException(status_code=403, detail="Version metadata is written by the gateway")
    auth.require_scope(actor, auth.SCOPE_VERSIONS)
    version, asset = _version(db, version_id)
    ext = _write_meta(db, version, asset, body)
    db.commit()
    return _meta_response(db, version, ext)


@router.post("/versions/{version_id}/publish", response_model=VersionMetaResponse)
def publish_version(
    version_id: uuid.UUID,
    force: bool = Query(False, description="Publish although earlier comments are still unchecked"),
    db: Session = Depends(get_db), actor: Actor = Depends(get_actor),
):
    """Publish to client: the only way an internal version becomes visible to
    the client tier. Nothing is sent with it; the team's notes stay where they are.

    Waits for the fix check: while comments from earlier versions are unchecked on
    this one, it answers 409 (code `fix_check_incomplete`) unless `force`. On
    publish, the comments the team found fixed are resolved."""
    version, asset = _version(db, version_id)
    user = _require_manager(db, asset.project_id, actor)
    ext = db.get(VersionExt, version.id)
    if version.visibility == visibility.PUBLIC:
        return _meta_response(db, version, ext)
    if version.processing_status != ProcessingStatus.ready:
        raise HTTPException(status_code=409, detail="This version is not ready to be shown yet")
    left = fixcheck.unchecked_count(db, version)
    if left and not force:
        raise HTTPException(status_code=409, detail={
            "code": "fix_check_incomplete", "unchecked": left,
            "message": f"{left} earlier comment{'s' if left != 1 else ''} not checked on this version yet",
        })
    resolved = fixcheck.resolve_on_publish(db, version)
    version.visibility = visibility.PUBLIC
    if ext is None:
        ext = VersionExt(version_id=version.id)
        db.add(ext)
    ext.published_by = user.id
    ext.published_at = datetime.now(timezone.utc)
    events.emit(db, events.VERSION_PUBLISHED, asset.project_id, {
        **events.version_payload(version, asset), "published_by": user.id,
        "resolved_comment_ids": resolved, "unchecked_comments": left,
    })
    _done(db, asset.project_id)
    live.forget_version(version.id)
    # The client's screens learn there is something new to load.
    event_service.publish_sync(asset.project_id, "version_published", {
        "asset_id": str(asset.id), "version_id": str(version.id),
    })
    return _meta_response(db, version, ext)


# ── Comment feed: a video's comments with frames, and as marker files ─────────

@router.get("/assets/{asset_id}/comments")
def get_comment_feed(
    asset_id: uuid.UUID,
    version_id: Optional[uuid.UUID] = Query(None, description="Left out: the newest ready version"),
    include_resolved: bool = Query(False),
    db: Session = Depends(get_db), actor: Actor = Depends(get_actor),
):
    """A version's comments with their frames, and where the version came from in
    Resolve, for the team's tools: Garlo Worker puts them on the timeline, an agent
    reads them. Internal: a client gets 404."""
    asset = _live_asset(db, asset_id)
    _require_internal(db, asset.project_id, actor)
    return feed.feed(db, asset, version_id, include_resolved)


@router.get("/assets/{asset_id}/comments/export")
def export_comment_feed(
    asset_id: uuid.UUID,
    format: str = Query(..., description="edl (Resolve markers), premiere_xml, fcpxml or csv"),
    version_id: Optional[uuid.UUID] = Query(None),
    fps: Optional[float] = Query(None, gt=0),
    start_tc: Optional[str] = Query(None),
    include_resolved: bool = Query(False),
    db: Session = Depends(get_db), actor: Actor = Depends(get_actor),
):
    """The same files as the review screen's export, for the gateway and the team.
    The EDL starts at the version's Resolve start timecode when the gateway recorded one."""
    from ..routers.comments import build_comment_export  # upstream's export, split out for this
    asset = _live_asset(db, asset_id)
    _require_internal(db, asset.project_id, actor)
    version = feed.pick_version(db, asset, version_id)
    timing = feed.version_timing(db, version)
    if start_tc is None and format == "edl" and timing["start_timecode"]:
        start_tc = timing["start_timecode"]
    return build_comment_export(db, asset, format, version.id, fps or timing["fps"], start_tc, include_resolved)


# ── Fix check: earlier comments, checked on a new version ──────────────────────

@router.get("/versions/{version_id}/fix-checks", response_model=FixChecklist)
def get_fix_checks(version_id: uuid.UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    """The comments from earlier versions to check on this one, with what the team
    found so far. Internal: a client gets 404."""
    version, asset = _version(db, version_id)
    _require_internal(db, asset.project_id, actor)
    return fixcheck.checklist(db, version)


@router.put("/versions/{version_id}/fix-checks/{comment_id}", response_model=FixCheckItem)
def put_fix_check(
    version_id: uuid.UUID, comment_id: uuid.UUID, body: FixCheckPut,
    db: Session = Depends(get_db), actor: Actor = Depends(get_actor),
):
    """Mark one earlier comment fixed / not fixed / won't fix on this version, or
    clear it. The team does this, not the gateway; and only before the version
    is published, after that the record stays as it was."""
    if actor.is_gateway:
        raise HTTPException(status_code=403, detail="The team checks fixes; the gateway can read them")
    version, asset = _version(db, version_id)
    _require_internal(db, asset.project_id, actor)
    user = auth.require_user(actor)
    if version.visibility == visibility.PUBLIC:
        raise HTTPException(status_code=409, detail="This version is already with the client; its check is closed")
    try:
        row = fixcheck.set_check(db, version, comment_id, body.status.value if body.status else None, body.note, user.id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    events.emit(db, events.FIX_CHECK_UPDATED, asset.project_id, {
        "version_id": version.id, "asset_id": asset.id, "comment_id": comment_id,
        "status": row.status if row else None, "checked_by": user.id,
    })
    _done(db, asset.project_id)
    item = next(i for i in fixcheck.checklist(db, version)["items"] if i["comment_id"] == comment_id)
    return item


# ── Comment visibility ─────────────────────────────────────────────────────────

@router.patch("/comments/{comment_id}/visibility")
def set_comment_visibility(
    comment_id: uuid.UUID, body: CommentVisibilityChange,
    db: Session = Depends(get_db), actor: Actor = Depends(get_actor),
):
    comment = db.query(Comment).filter(Comment.id == comment_id, Comment.deleted_at.is_(None)).first()
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    asset = _live_asset(db, comment.asset_id)
    _require_manager(db, asset.project_id, actor)
    if comment.visibility == body.visibility:
        return {"comment_id": str(comment.id), "visibility": comment.visibility, "changed": [str(comment.id)]}

    changed = [comment.id]
    if body.visibility == visibility.PUBLIC:
        if comment.parent_id is not None:
            parent = db.query(Comment).filter(Comment.id == comment.parent_id).first()
            if parent is not None and parent.visibility == visibility.INTERNAL:
                raise HTTPException(
                    status_code=409,
                    detail="A reply under an internal comment stays internal; make the comment above it client-visible first",
                )
        comment.visibility = visibility.PUBLIC
    else:
        # A reply to a team-only comment is team-only too, all the way down.
        frontier = [comment.id]
        while frontier:
            children = [row[0] for row in db.query(Comment.id).filter(
                Comment.parent_id.in_(frontier), Comment.visibility != visibility.INTERNAL,
            ).all()]
            changed.extend(children)
            frontier = children
        db.execute(update(Comment).where(Comment.id.in_(changed)).values(visibility=visibility.INTERNAL))
        db.refresh(comment)

    events.emit(db, events.COMMENT_VISIBILITY_CHANGED, asset.project_id, {
        "comment_id": comment.id, "asset_id": asset.id, "version_id": comment.version_id,
        "visibility": body.visibility, "changed_comment_ids": changed,
    })
    _done(db)
    # On the client channel in both directions: a client's screen has to drop a
    # comment that became internal, and load one that became visible. The
    # payload is ids only, of a comment they could see before or can see now.
    event_service.publish_sync(asset.project_id, "comment_visibility_changed", {
        "asset_id": str(asset.id), "comment_id": str(comment.id),
    })
    return {"comment_id": str(comment.id), "visibility": body.visibility, "changed": [str(c) for c in changed]}


# ── People: FreeFrame's accounts, for the gateway to match with its own ────────

@router.get("/people", response_model=list[PersonResponse])
def list_people(
    ids: Optional[str] = Query(default=None, description="Comma-separated user ids"),
    emails: Optional[str] = Query(default=None, description="Comma-separated email addresses"),
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    """Look accounts up by id or by email. The gateway knows people by email;
    tasks and versions here name them by id. Nothing is listed without a key."""
    if actor.is_gateway:
        auth.require_scope(actor, auth.SCOPE_READ)
    elif not visibility.is_admin(actor.user):
        raise HTTPException(status_code=403, detail="People are looked up by the gateway")
    wanted_ids = []
    for raw in (ids or "").split(","):
        if raw.strip():
            try:
                wanted_ids.append(uuid.UUID(raw.strip()))
            except ValueError:
                raise HTTPException(status_code=422, detail=f"{raw.strip()} is not an id") from None
    wanted_emails = [e.strip().lower() for e in (emails or "").split(",") if e.strip()]
    if not wanted_ids and not wanted_emails:
        raise HTTPException(status_code=422, detail="Give ids or emails")
    if len(wanted_ids) + len(wanted_emails) > 200:
        raise HTTPException(status_code=422, detail="At most 200 at a time")
    return db.query(User).filter(
        User.deleted_at.is_(None),
        or_(User.id.in_(wanted_ids), func.lower(User.email).in_(wanted_emails)),
    ).order_by(User.email).all()


# ── Events: catch-up ───────────────────────────────────────────────────────────

@router.get("/events", response_model=EventsPage)
def list_events(
    since: int = Query(default=0, ge=0, description="Return events with seq greater than this"),
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    if actor.is_gateway:
        auth.require_scope(actor, auth.SCOPE_READ)
    elif not visibility.is_admin(actor.user):
        raise HTTPException(status_code=403, detail="Events are read by the gateway")
    rows = db.query(ExtEvent).filter(ExtEvent.seq > since).order_by(ExtEvent.seq).limit(limit).all()
    return EventsPage(events=rows, last_seq=rows[-1].seq if rows else since)


# ── The gateway's upload path ──────────────────────────────────────────────────

def _require_gateway_upload(actor: Actor) -> None:
    if not actor.is_gateway:
        raise HTTPException(status_code=403, detail="This upload path is the gateway's; people upload in the app")
    auth.require_scope(actor, auth.SCOPE_VERSIONS)


@router.post(
    "/assets/{asset_id}/versions", response_model=GatewayVersionCreated, status_code=status.HTTP_201_CREATED,
)
def gateway_create_version(
    asset_id: uuid.UUID, body: GatewayVersionCreate, db: Session = Depends(get_db), actor: Actor = Depends(get_actor),
):
    """Start a new, internal version of an existing asset and hand back one
    presigned PUT URL for its file. The storage keys never leave the server."""
    from ..routers.upload import ALLOWED_MIME_TYPES

    _require_gateway_upload(actor)
    asset = _live_asset(db, asset_id)
    if body.mime_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {body.mime_type}")
    if body.file_size_bytes > SINGLE_PUT_MAX_BYTES:
        raise HTTPException(status_code=400, detail="This upload path takes files up to 5 GiB")
    guard_error = upload_guard_error(db, body.file_size_bytes)
    if guard_error:
        raise HTTPException(status_code=400, detail=guard_error)
    author = db.query(User).filter(User.id == body.author_person_id, User.deleted_at.is_(None)).first()
    if author is None or not visibility.sees_internal(db, asset.project_id, author):
        raise HTTPException(status_code=422, detail="author_person_id must be an owner or editor of this project")

    version = AssetVersion(
        asset_id=asset.id,
        version_number=next_version_number(db, asset.id),
        processing_status=ProcessingStatus.uploading,
        created_by=author.id,
        visibility=visibility.INTERNAL,
        last_activity_at=datetime.now(timezone.utc),
    )
    db.add(version)
    db.flush()
    extension = os.path.splitext(body.original_filename)[1].lower()
    s3_key = f"raw/{asset.project_id}/{asset.id}/{version.id}/original{extension}"
    file_types = {
        AssetType.image: FileType.image, AssetType.audio: FileType.audio,
        AssetType.video: FileType.video, AssetType.image_carousel: FileType.image,
    }
    db.add(MediaFile(
        version_id=version.id, file_type=file_types.get(asset.asset_type, FileType.video),
        original_filename=body.original_filename, mime_type=body.mime_type,
        file_size_bytes=body.file_size_bytes, s3_key_raw=s3_key,
    ))
    _write_meta(db, version, asset, body)
    upload_url = s3_service.generate_presigned_put_url(
        s3_key, content_type=body.mime_type, expires_in=UPLOAD_URL_SECONDS,
    )
    db.commit()
    return GatewayVersionCreated(
        asset_id=asset.id, version_id=version.id, version_number=version.version_number,
        s3_key=s3_key, upload_url=upload_url, upload_expires_in=UPLOAD_URL_SECONDS,
    )


@router.post("/versions/{version_id}/finalize", response_model=VersionFinalized)
def gateway_finalize_version(version_id: uuid.UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    """The file is in storage: start the transcode. Safe to call again; only the
    first call that finds the version still uploading starts anything."""
    from ..routers.upload import _trigger_processing

    _require_gateway_upload(actor)
    version, asset = _version(db, version_id)
    if version.processing_status != ProcessingStatus.uploading:
        return VersionFinalized(asset_id=asset.id, version_id=version.id, status=version.processing_status.value)
    media = db.query(MediaFile).filter(MediaFile.version_id == version.id).first()
    if media is None:
        raise HTTPException(status_code=409, detail="This version has no file record")
    try:
        stored = s3_service.head_object_size(media.s3_key_raw)
    except Exception as exc:  # noqa: BLE001 - storage unreachable is a retry, not a verdict
        raise HTTPException(
            status_code=503, detail="Could not reach storage to finalize this upload. Please retry.",
            headers={"Retry-After": "5"},
        ) from exc
    if stored is None:
        raise HTTPException(status_code=409, detail="The file has not been uploaded yet")
    if stored != media.file_size_bytes:
        media.file_size_bytes = stored   # storage is the truth about what arrived

    asset_id, claimed_version_id = asset.id, version.id
    claimed = db.query(AssetVersion).filter(
        AssetVersion.id == claimed_version_id,
        AssetVersion.processing_status == ProcessingStatus.uploading,
    ).update({AssetVersion.processing_status: ProcessingStatus.processing}, synchronize_session=False)
    if claimed:
        events.emit_version_created(db, version)
    db.commit()
    if claimed:
        events.dispatch()
        _trigger_processing(asset_id, claimed_version_id)
    return VersionFinalized(asset_id=asset_id, version_id=claimed_version_id, status=ProcessingStatus.processing.value)


# ── Project members from HQ ────────────────────────────────────────────────────
# HQ's project team is the source: the gateway puts a person on the FreeFrame project
# and takes them off again. By email, because the account may not exist yet: it is
# made here, waiting, and the person's first sign-in through Authentik (OIDC, matched
# by email) opens it. FreeFrame sends no email of its own; HQ's invitation did that.
# Owners are never made, changed or removed here, and nor is an admin's account.

def _require_members_gateway(actor: Actor) -> None:
    if not actor.is_gateway:
        raise HTTPException(status_code=403, detail="Only the gateway uses this; people use the project's member list")
    auth.require_scope(actor, auth.SCOPE_MEMBERS)


def _clean_email(raw: str) -> str:
    email = raw.strip()
    if "@" not in email or " " in email or len(email) > 255:
        raise HTTPException(status_code=422, detail="email: not an email address")
    return email


@router.put("/projects/{project_id}/members", response_model=MemberResult)
def put_project_member(project_id: uuid.UUID, body: MemberPut, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    from ..models.project import ProjectRole
    from ..models.user import UserStatus
    from ..services.auth_service import get_email_owner, get_user_by_email

    _require_members_gateway(actor)
    require_live_project(db, project_id)
    email = _clean_email(body.email)
    role = ProjectRole(body.role)

    created = False
    user = get_user_by_email(db, email)
    if user is None:
        if get_email_owner(db, email) is not None:
            raise HTTPException(status_code=409, detail=f"{email} belonged to a deleted FreeFrame account; restore or remove it there first")
        user = User(email=email.lower(), name=(body.name or "").strip() or email.split("@")[0],
                    status=UserStatus.pending_invite, email_verified=False, can_create_projects=False)
        db.add(user)
        db.flush()
        created = True
    elif user.status == UserStatus.deactivated:
        raise HTTPException(status_code=409, detail=f"{email} is deactivated in FreeFrame")

    member = db.query(ProjectMember).filter(ProjectMember.project_id == project_id, ProjectMember.user_id == user.id).first()
    changed = True
    if member is None:
        member = ProjectMember(project_id=project_id, user_id=user.id, role=role, invited_by=None)
        db.add(member)
    elif member.deleted_at is not None:
        member.deleted_at = None
        member.role = role
        member.invited_by = None
        member.invited_at = func.now()
    elif member.role == ProjectRole.owner or member.role == role:
        changed = False      # an owner stays an owner; the same role is nothing to do
    else:
        member.role = role
    db.commit()
    db.refresh(member)
    return MemberResult(user_id=user.id, email=user.email, role=member.role.value, account_created=created, changed=changed)


@router.delete("/projects/{project_id}/members", response_model=MemberResult)
def remove_project_member_by_email(project_id: uuid.UUID, email: str = Query(..., min_length=3), db: Session = Depends(get_db),
                                   actor: Actor = Depends(get_actor)):
    from ..models.project import ProjectRole
    from ..services.auth_service import get_user_by_email

    _require_members_gateway(actor)
    require_live_project(db, project_id)
    user = get_user_by_email(db, _clean_email(email))
    if user is None:
        raise HTTPException(status_code=404, detail="No such FreeFrame account")
    member = db.query(ProjectMember).filter(
        ProjectMember.project_id == project_id, ProjectMember.user_id == user.id, ProjectMember.deleted_at.is_(None),
    ).first()
    if member is None:
        return MemberResult(user_id=user.id, email=user.email, role="none", account_created=False, changed=False)
    if member.role == ProjectRole.owner:
        raise HTTPException(status_code=409, detail="An owner is not removed from HQ; do that in FreeFrame")
    member.deleted_at = datetime.now(timezone.utc)
    db.commit()
    return MemberResult(user_id=user.id, email=user.email, role="none", account_created=False, changed=True)


# ── Gateway tokens: an instance admin's to hand out ────────────────────────────

def _require_admin(actor: Actor) -> User:
    user = auth.require_user(actor)
    if not visibility.is_admin(user):
        raise HTTPException(status_code=403, detail="Only an instance admin manages gateway tokens")
    return user


@router.post("/gateway-tokens", response_model=GatewayTokenCreated, status_code=status.HTTP_201_CREATED)
def create_gateway_token(body: GatewayTokenCreate, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    user = _require_admin(actor)
    try:
        row, token = auth.create_token(db, body.name, user.id, body.scopes or auth.ALL_SCOPES)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    db.refresh(row)
    return GatewayTokenCreated(
        id=row.id, name=row.name, scopes=row.scopes, created_at=row.created_at,
        last_used_at=row.last_used_at, revoked_at=row.revoked_at, token=token,
    )


@router.get("/gateway-tokens", response_model=list[GatewayTokenResponse])
def list_gateway_tokens(db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    _require_admin(actor)
    return db.query(GatewayToken).order_by(GatewayToken.created_at).all()


@router.delete("/gateway-tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_gateway_token(token_id: uuid.UUID, db: Session = Depends(get_db), actor: Actor = Depends(get_actor)):
    _require_admin(actor)
    row = db.get(GatewayToken, token_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        db.commit()
