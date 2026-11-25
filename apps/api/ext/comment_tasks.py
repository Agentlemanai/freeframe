"""Every comment is a task (Laci, 25 Nov 2026): "if somebody commented, that's
already a task". There is no step to turn a comment into one.

- A new top-level comment, the client's or the team's, becomes an open task for
  the project's editors (a role pool), in the same transaction as the comment.
  Replies never do. Comments written before this stay as they were (their
  "Make task" button remains).
- The comment's text is the task's instruction while nobody has rewritten it:
  editing the comment edits the task too; deleting the comment cancels an open
  task.
- "Not a task" (praise, chat): the team cancels it with that note.
- The fix check closes them: on publish, a comment found fixed approves its task
  (with the published version as the result), a won't-fix cancels it.
"""
import uuid
from datetime import datetime, timezone
from typing import Iterable, Optional

from fastapi import HTTPException

from ..models.asset import Asset, AssetVersion, MediaFile
from ..models.comment import Comment
from ..models.user import User
from . import events
from .models import (
    AssigneeType, FixCheck, FixCheckStatus, NoteKind, ReviewTask, TaskKind, TaskStage, TaskStatus as S,
    VersionExt, VersionResolvedTask,
)
from .tasks import ACTIVE, TERMINAL, add_note, task_payload, version_is_approved

POOL = "editor"
NOT_A_TASK = "Not a task"


def _fps(db, version_id) -> Optional[float]:
    ext = db.get(VersionExt, version_id)
    if ext is not None and ext.fps:
        return ext.fps
    media = db.query(MediaFile).filter(MediaFile.version_id == version_id).first()
    return media.fps if media is not None and media.fps else None


def _frame(seconds, fps):
    return None if seconds is None or not fps else int(round(seconds * fps))


def _status_event(db, task: ReviewTask, from_status: str, by: str) -> None:
    events.emit(db, events.TASK_STATUS_CHANGED, task.project_id, {
        "task": task_payload(task), "from": from_status, "to": task.status, "rejected": False, "by": by,
    })


def on_comment_created(db, comment: Comment, asset: Asset) -> Optional[ReviewTask]:
    """The comment's event, and its task when it is a top-level comment. Never commits."""
    events.emit(db, events.COMMENT_CREATED, asset.project_id, events.comment_payload(comment, asset))
    if comment.parent_id is not None or not (comment.body or "").strip():
        return None
    # nothing more is assigned on a version a manager has approved
    if version_is_approved(db, asset.project_id, comment.version_id):
        return None
    fps = _fps(db, comment.version_id)
    frame_in = _frame(comment.timecode_start, fps)
    frame_out = _frame(comment.timecode_end, fps) if comment.timecode_end is not None else frame_in
    task = ReviewTask(
        project_id=asset.project_id, asset_id=asset.id, created_on_version_id=comment.version_id,
        frame_in=frame_in, frame_out=frame_out, kind=TaskKind.comment.value, instruction=comment.body.strip(),
        stage=TaskStage.review.value, assignee_type=AssigneeType.role_pool.value, assignee_role=POOL,
        status=S.open.value, source_comment_id=comment.id,
        # a guest has no account: the task is the video's owner's to answer for
        created_by=comment.author_id or asset.created_by,
    )
    db.add(task)
    db.flush()
    events.emit(db, events.TASK_CREATED, task.project_id, {"task": task_payload(task)})
    return task


def _open_tasks_of(db, comment_id) -> list[ReviewTask]:
    return db.query(ReviewTask).filter(
        ReviewTask.source_comment_id == comment_id, ReviewTask.status.notin_(list(TERMINAL)),
    ).all()


def on_comment_edited(db, comment: Comment, old_body: str) -> None:
    """A task whose instruction is still the comment's old text follows the edit."""
    for task in _open_tasks_of(db, comment.id):
        if task.instruction.strip() == (old_body or "").strip() and comment.body.strip():
            task.instruction = comment.body.strip()
            db.flush()
            events.emit(db, events.TASK_UPDATED, task.project_id, {"task": task_payload(task)})


def on_comment_deleted(db, comment: Comment) -> None:
    """A deleted comment takes its task with it while nobody has started on it."""
    for task in _open_tasks_of(db, comment.id):
        if task.status != S.open.value:
            continue
        task.status = S.cancelled.value
        db.flush()
        add_note(db, task, kind=NoteKind.review.value, body="Its comment was deleted.",
                 author_label="FreeFrame")
        _status_event(db, task, S.open.value, "comment_deleted")


def dismiss(db, task: ReviewTask, user: User, note: Optional[str]) -> ReviewTask:
    """'Not a task': the team cancels an open task that came from a comment."""
    if task.source_comment_id is None:
        raise HTTPException(status_code=409, detail="Only a task made from a comment can be dismissed this way")
    if task.status != S.open.value:
        raise HTTPException(status_code=409, detail=f"Someone is already on it ({task.status}); a manager can cancel it")
    task.status = S.cancelled.value
    db.flush()
    add_note(db, task, kind=NoteKind.review.value, body=(note or "").strip() or NOT_A_TASK,
             author_person_id=user.id)
    _status_event(db, task, S.open.value, "team")
    return task


def close_on_publish(db, version: AssetVersion, resolved_comment_ids: Iterable[uuid.UUID], user: User) -> dict:
    """What the fix check found, applied to the comments' tasks when the version goes out."""
    resolved = set(resolved_comment_ids)
    checks = db.query(FixCheck.comment_id, FixCheck.status).join(
        AssetVersion, AssetVersion.id == FixCheck.version_id,
    ).filter(
        AssetVersion.asset_id == version.asset_id, AssetVersion.version_number <= version.version_number,
    ).all()
    latest = {}
    for comment_id, status in checks:
        latest[comment_id] = status
    done = {"approved": [], "cancelled": []}
    for comment_id, status in latest.items():
        if status == FixCheckStatus.fixed.value and comment_id in resolved:
            to, note = S.approved.value, f"Fixed in v{version.version_number} (fix check)."
        elif status == FixCheckStatus.wont_fix.value:
            to, note = S.cancelled.value, "Won't fix (fix check)."
        else:
            continue
        for task in _open_tasks_of(db, comment_id):
            before = task.status
            task.status = to
            task.claimed_by = None
            if to == S.approved.value:
                task.result_version_id = version.id
                if db.get(VersionResolvedTask, (version.id, task.id)) is None:
                    db.add(VersionResolvedTask(version_id=version.id, task_id=task.id))
            db.flush()
            add_note(db, task, kind=NoteKind.review.value, body=note, author_person_id=user.id)
            _status_event(db, task, before, "manager")
            done[to].append(task.id)
    return done
