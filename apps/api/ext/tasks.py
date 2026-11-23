"""Review tasks: the rules. The routes in `router.py` only parse and authorize.

A task's life:

    open → claimed → in_progress → proposed → approved
                                      └─ rejected: back to open, with the manager's note
    any of the four above ⇄ needs_info      (a question; answering puts it back)
    anything not finished → cancelled       (a manager)

Who may make which change is decided here, on the server:

- the gateway moves work along: claimed, in_progress, proposed, needs_info, and
  back to open when a lease runs out;
- only a manager (project owner, instance admin) approves, rejects or cancels;
- an agent may only be assigned at the radio, rough and review stages, and
  nothing is assigned on a version a manager has approved.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

from ..models.approval import Approval, ApprovalStatus
from ..models.asset import Asset, AssetVersion, MediaFile
from ..models.comment import Comment
from ..models.user import User
from . import events, visibility
from .models import (
    AssigneeType, AuthorType, NoteKind, ReviewTask, ReviewTaskNote, TaskStage, TaskStatus,
    VersionResolvedTask,
)

GATEWAY = "gateway"
MANAGER = "manager"

S = TaskStatus
ACTIVE = (S.open.value, S.claimed.value, S.in_progress.value, S.proposed.value)
TERMINAL = (S.approved.value, S.cancelled.value)

#: Stages an agent may be assigned at. Everything else is humans only.
AGENT_STAGES = (TaskStage.radio.value, TaskStage.rough.value, TaskStage.review.value)

#: from → to → who. `needs_info` is not a key: leaving it is `_leave_needs_info`.
TRANSITIONS: dict[str, dict[str, tuple[str, ...]]] = {
    S.open.value: {
        S.claimed.value: (GATEWAY,),
        S.needs_info.value: (GATEWAY, MANAGER),
        S.cancelled.value: (MANAGER,),
    },
    S.claimed.value: {
        S.in_progress.value: (GATEWAY,),
        S.open.value: (GATEWAY,),            # the lease ran out
        S.needs_info.value: (GATEWAY, MANAGER),
        S.cancelled.value: (MANAGER,),
    },
    S.in_progress.value: {
        S.proposed.value: (GATEWAY,),
        S.open.value: (GATEWAY,),            # the lease ran out
        S.needs_info.value: (GATEWAY, MANAGER),
        S.cancelled.value: (MANAGER,),
    },
    S.proposed.value: {
        S.approved.value: (MANAGER,),
        S.open.value: (MANAGER,),            # rejected
        S.needs_info.value: (GATEWAY, MANAGER),
        S.cancelled.value: (MANAGER,),
    },
}


def allowed(from_status: str, to_status: str, who: str, resume_status: Optional[str] = None) -> bool:
    """Whether `who` may move a task from one status to another."""
    if from_status == S.needs_info.value:
        if to_status == S.cancelled.value:
            return who == MANAGER
        # Back to where the question was asked from, or to open if that work
        # has been given up in the meantime.
        return to_status in (resume_status, S.open.value) and to_status in ACTIVE
    return who in TRANSITIONS.get(from_status, {}).get(to_status, ())


# ── Assignment ─────────────────────────────────────────────────────────────────

def version_is_approved(db, project_id: uuid.UUID, version_id: uuid.UUID) -> bool:
    """A version counts as approved once a manager has given it an approved
    verdict. A client's approval does not close a version to further work."""
    approvers = db.query(User).join(Approval, Approval.user_id == User.id).filter(
        Approval.version_id == version_id,
        Approval.status == ApprovalStatus.approved,
        Approval.deleted_at.is_(None),
    ).all()
    return any(visibility.is_manager(db, project_id, user) for user in approvers)


def check_assignment(
    db, project_id: uuid.UUID, *, stage: str, assignee_type: str,
    assignee_person_id: Optional[uuid.UUID], assignee_role: Optional[str],
    version_id: Optional[uuid.UUID],
) -> None:
    """Refuse an assignment the stage rules forbid. Raises 4xx; returns nothing."""
    if assignee_type == AssigneeType.agent.value and stage not in AGENT_STAGES:
        raise HTTPException(
            status_code=422,
            detail=f"An agent cannot be assigned at the {stage} stage; it is for people only",
        )
    if assignee_type == AssigneeType.person.value:
        if assignee_person_id is None:
            raise HTTPException(status_code=422, detail="assignee_person_id is required for a person")
        person = db.query(User).filter(User.id == assignee_person_id, User.deleted_at.is_(None)).first()
        if person is None or not visibility.sees_internal(db, project_id, person):
            raise HTTPException(
                status_code=422,
                detail="The assignee must be an owner or editor of this project",
            )
    if assignee_type == AssigneeType.role_pool.value and not assignee_role:
        raise HTTPException(status_code=422, detail="assignee_role is required for a role pool")
    if version_id is not None and version_is_approved(db, project_id, version_id):
        raise HTTPException(status_code=409, detail="This version is approved; nothing more is assigned on it")


# ── Payloads ───────────────────────────────────────────────────────────────────

_TASK_FIELDS = (
    "id", "project_id", "asset_id", "created_on_version_id", "frame_in", "frame_out", "kind",
    "instruction", "stage", "assignee_type", "assignee_person_id", "assignee_role", "status",
    "resume_status", "source_comment_id", "result_version_id", "claimed_by", "created_by",
)


def task_payload(task: ReviewTask) -> dict:
    return {field: getattr(task, field) for field in _TASK_FIELDS}


def note_payload(note: ReviewTaskNote) -> dict:
    return {
        "note_id": note.id, "task_id": note.task_id, "kind": note.kind, "body": note.body,
        "author_type": note.author_type, "author_person_id": note.author_person_id,
        "author_label": note.author_label,
    }


# ── Writes. None of these commit: the route commits once, then dispatches. ─────

def _frame(seconds: Optional[float], fps: Optional[float]) -> Optional[int]:
    if seconds is None or not fps:
        return None
    return int(round(seconds * fps))


def create_task(db, asset: Asset, body, creator: User) -> ReviewTask:
    comment = None
    if body.source_comment_id is not None:
        comment = db.query(Comment).filter(
            Comment.id == body.source_comment_id,
            Comment.asset_id == asset.id,
            Comment.deleted_at.is_(None),
        ).first()
        if comment is None:
            raise HTTPException(status_code=404, detail="Source comment not found on this asset")

    version_id = body.version_id or (comment.version_id if comment is not None else None)
    version_query = db.query(AssetVersion).filter(
        AssetVersion.asset_id == asset.id, AssetVersion.deleted_at.is_(None),
    )
    if version_id is not None:
        version = version_query.filter(AssetVersion.id == version_id).first()
    else:
        version = version_query.order_by(AssetVersion.version_number.desc()).first()
    if version is None:
        raise HTTPException(status_code=400, detail="version_id does not belong to this asset")

    instruction = (body.instruction or (comment.body if comment is not None else "") or "").strip()
    if not instruction:
        raise HTTPException(status_code=422, detail="instruction is required")

    frame_in, frame_out = body.frame_in, body.frame_out
    if frame_in is None and frame_out is None and comment is not None:
        # A comment is timed in seconds; a task is cut in frames.
        media = db.query(MediaFile).filter(MediaFile.version_id == version.id).first()
        fps = media.fps if media is not None else None
        frame_in = _frame(comment.timecode_start, fps)
        frame_out = _frame(comment.timecode_end, fps)
    if frame_in is not None and frame_out is None:
        frame_out = frame_in          # a point
    if frame_out is not None and frame_in is None:
        frame_in = frame_out

    check_assignment(
        db, asset.project_id, stage=body.stage.value, assignee_type=body.assignee_type.value,
        assignee_person_id=body.assignee_person_id, assignee_role=body.assignee_role,
        version_id=version.id,
    )
    task = ReviewTask(
        project_id=asset.project_id, asset_id=asset.id, created_on_version_id=version.id,
        frame_in=frame_in, frame_out=frame_out, kind=body.kind.value, instruction=instruction,
        stage=body.stage.value, assignee_type=body.assignee_type.value,
        assignee_person_id=body.assignee_person_id if body.assignee_type == AssigneeType.person else None,
        assignee_role=body.assignee_role if body.assignee_type != AssigneeType.person else None,
        status=S.open.value, source_comment_id=comment.id if comment is not None else None,
        created_by=creator.id,
    )
    db.add(task)
    db.flush()
    events.emit(db, events.TASK_CREATED, task.project_id, {"task": task_payload(task)})
    return task


def update_task(db, task: ReviewTask, body) -> ReviewTask:
    if task.status in TERMINAL:
        raise HTTPException(status_code=409, detail=f"A task that is {task.status} cannot be changed")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return task
    for field in ("kind", "stage", "assignee_type"):
        if changes.get(field) is not None:
            changes[field] = changes[field].value

    merged = {
        "stage": changes.get("stage", task.stage),
        "assignee_type": changes.get("assignee_type", task.assignee_type),
        "assignee_person_id": changes.get("assignee_person_id", task.assignee_person_id),
        "assignee_role": changes.get("assignee_role", task.assignee_role),
    }
    if merged["assignee_type"] == AssigneeType.person.value:
        merged["assignee_role"] = None
    else:
        merged["assignee_person_id"] = None
    assignment_changed = any(merged[k] != getattr(task, k) for k in merged)
    if assignment_changed:
        # Work already under way cannot be handed to someone else underneath
        # whoever holds it; the gateway returns it to open first.
        if merged["assignee_type"] != task.assignee_type or merged["assignee_person_id"] != task.assignee_person_id:
            if task.status not in (S.open.value, S.needs_info.value):
                raise HTTPException(status_code=409, detail=f"A task that is {task.status} cannot be reassigned")
        check_assignment(db, task.project_id, version_id=task.created_on_version_id, **merged)

    frame_in = changes.get("frame_in", task.frame_in)
    frame_out = changes.get("frame_out", task.frame_out)
    if frame_in is not None and frame_out is not None and frame_out < frame_in:
        raise HTTPException(status_code=422, detail="frame_out is before frame_in")

    before = task_payload(task)
    for field, value in {**changes, **merged}.items():
        setattr(task, field, value)
    db.flush()
    after = task_payload(task)
    changed = sorted(k for k in after if after[k] != before[k])
    if changed:
        events.emit(db, events.TASK_UPDATED, task.project_id, {"task": after, "changed": changed})
    return task


def add_note(
    db, task: ReviewTask, *, kind: str, body: str, author_type: str = AuthorType.person.value,
    author_person_id: Optional[uuid.UUID] = None, author_label: Optional[str] = None,
) -> ReviewTaskNote:
    note = ReviewTaskNote(
        task_id=task.id, kind=kind, body=body, author_type=author_type,
        author_person_id=author_person_id, author_label=author_label,
        # The wall clock, not the column default: now() is the transaction's
        # start, and a thread is read in the order it was written.
        created_at=datetime.now(timezone.utc),
    )
    db.add(note)
    db.flush()
    events.emit(db, events.TASK_NOTE_CREATED, task.project_id, {"note": note_payload(note)})
    return note


def change_status(
    db, task: ReviewTask, to_status: str, who: str, *,
    result_version_id: Optional[uuid.UUID] = None, claimed_by: Optional[str] = None,
    note: Optional[str] = None, note_kind: Optional[str] = None,
    note_author_type: str = AuthorType.person.value,
    note_author_person_id: Optional[uuid.UUID] = None, note_author_label: Optional[str] = None,
) -> ReviewTask:
    """Move a task, or refuse. `who` is GATEWAY or MANAGER; the route decides
    which the caller is."""
    from_status = task.status
    if to_status == from_status:
        raise HTTPException(status_code=409, detail=f"The task is already {from_status}")
    if not allowed(from_status, to_status, who, task.resume_status):
        raise HTTPException(
            status_code=409 if from_status in TERMINAL else 403,
            detail=f"{who} may not move a task from {from_status} to {to_status}",
        )

    rejected = from_status == S.proposed.value and to_status == S.open.value
    if to_status == S.needs_info.value and not (note or "").strip():
        raise HTTPException(status_code=422, detail="A question is required to ask for information")
    if rejected and not (note or "").strip():
        raise HTTPException(status_code=422, detail="A note is required to reject a proposal")

    if to_status == S.proposed.value:
        if result_version_id is None:
            raise HTTPException(status_code=422, detail="result_version_id is required to propose")
        version = db.query(AssetVersion).filter(
            AssetVersion.id == result_version_id,
            AssetVersion.asset_id == task.asset_id,
            AssetVersion.deleted_at.is_(None),
        ).first()
        if version is None:
            raise HTTPException(status_code=422, detail="result_version_id is not a version of this task's asset")
        task.result_version_id = version.id
        if db.get(VersionResolvedTask, (version.id, task.id)) is None:
            db.add(VersionResolvedTask(version_id=version.id, task_id=task.id))

    if to_status == S.needs_info.value:
        task.resume_status = from_status
    elif from_status == S.needs_info.value:
        task.resume_status = None

    if to_status == S.open.value:
        # Nobody holds it any more, and a rejected proposal resolved nothing.
        task.claimed_by = None
        task.result_version_id = None
    elif claimed_by is not None:
        task.claimed_by = claimed_by

    task.status = to_status
    db.flush()

    if (note or "").strip():
        if note_kind is None:
            note_kind = NoteKind.question.value if to_status == S.needs_info.value else (
                NoteKind.review.value if who == MANAGER else NoteKind.editor_note.value
            )
        add_note(
            db, task, kind=note_kind, body=note.strip(), author_type=note_author_type,
            author_person_id=note_author_person_id, author_label=note_author_label,
        )

    events.emit(db, events.TASK_STATUS_CHANGED, task.project_id, {
        "task": task_payload(task), "from": from_status, "to": to_status,
        "rejected": rejected, "by": who,
    })
    return task
