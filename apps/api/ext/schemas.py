import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from .models import AssigneeType, AuthorType, FixCheckStatus, NoteKind, TaskKind, TaskStage, TaskStatus


class TaskCreate(BaseModel):
    asset_id: uuid.UUID
    # Left out: the source comment's version, else the newest version the caller can see.
    version_id: Optional[uuid.UUID] = None
    source_comment_id: Optional[uuid.UUID] = None
    frame_in: Optional[int] = Field(default=None, ge=0)
    frame_out: Optional[int] = Field(default=None, ge=0)
    kind: TaskKind
    # Left out with a source comment: the comment's text.
    instruction: Optional[str] = Field(default=None, max_length=10000)
    stage: TaskStage
    assignee_type: AssigneeType
    assignee_person_id: Optional[uuid.UUID] = None
    assignee_role: Optional[str] = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def _range(self):
        if self.frame_in is not None and self.frame_out is not None and self.frame_out < self.frame_in:
            raise ValueError("frame_out is before frame_in")
        return self


class TaskUpdate(BaseModel):
    frame_in: Optional[int] = Field(default=None, ge=0)
    frame_out: Optional[int] = Field(default=None, ge=0)
    kind: Optional[TaskKind] = None
    instruction: Optional[str] = Field(default=None, min_length=1, max_length=10000)
    stage: Optional[TaskStage] = None
    assignee_type: Optional[AssigneeType] = None
    assignee_person_id: Optional[uuid.UUID] = None
    assignee_role: Optional[str] = Field(default=None, max_length=50)


class TaskStatusChange(BaseModel):
    status: TaskStatus
    # Required for `proposed`: the version that carries the work.
    result_version_id: Optional[uuid.UUID] = None
    # The gateway's own words for who has the task: "Laci on garlopc".
    claimed_by: Optional[str] = Field(default=None, max_length=255)
    # Stored as a task note. Required when a task goes to `needs_info`.
    note: Optional[str] = Field(default=None, max_length=10000)
    note_author_type: AuthorType = AuthorType.person
    note_author_person_id: Optional[uuid.UUID] = None
    note_author_label: Optional[str] = Field(default=None, max_length=255)


class TaskDecision(BaseModel):
    note: Optional[str] = Field(default=None, max_length=10000)


class TaskResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    asset_id: uuid.UUID
    created_on_version_id: Optional[uuid.UUID]
    frame_in: Optional[int]
    frame_out: Optional[int]
    kind: str
    instruction: str
    stage: str
    assignee_type: str
    assignee_person_id: Optional[uuid.UUID]
    assignee_role: Optional[str]
    status: str
    resume_status: Optional[str]
    source_comment_id: Optional[uuid.UUID]
    result_version_id: Optional[uuid.UUID]
    claimed_by: Optional[str]
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class NoteCreate(BaseModel):
    kind: NoteKind
    body: str = Field(min_length=1, max_length=10000)
    # The gateway says who wrote it; a signed-in person is always themselves.
    author_type: AuthorType = AuthorType.person
    author_person_id: Optional[uuid.UUID] = None
    author_label: Optional[str] = Field(default=None, max_length=255)


class NoteResponse(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    kind: str
    body: str
    author_type: str
    author_person_id: Optional[uuid.UUID]
    author_label: Optional[str]
    created_at: datetime
    model_config = {"from_attributes": True}


class VersionMeta(BaseModel):
    resolve_project: Optional[str] = Field(default=None, max_length=255)
    resolve_timeline: Optional[str] = Field(default=None, max_length=255)
    start_timecode: Optional[str] = Field(default=None, pattern=r"^\d{2}[:;]\d{2}[:;]\d{2}[:;]\d{2}$")
    fps: Optional[float] = Field(default=None, gt=0, le=1000)
    author_type: AuthorType = AuthorType.person
    author_person_id: Optional[uuid.UUID] = None
    agent_machine: Optional[str] = Field(default=None, max_length=255)
    resolves_task_ids: list[uuid.UUID] = []


class VersionMetaResponse(VersionMeta):
    version_id: uuid.UUID
    visibility: str
    published_by: Optional[uuid.UUID] = None
    published_at: Optional[datetime] = None


class GatewayVersionCreate(VersionMeta):
    original_filename: str = Field(min_length=1, max_length=500)
    mime_type: str = Field(min_length=1, max_length=100)
    file_size_bytes: int = Field(gt=0)
    # The person the work is by, or the person an agent worked for. A version
    # row needs an owner, and the gateway is not a user.
    author_person_id: uuid.UUID


class GatewayVersionCreated(BaseModel):
    asset_id: uuid.UUID
    version_id: uuid.UUID
    version_number: int
    s3_key: str
    upload_url: str
    upload_expires_in: int


class VersionFinalized(BaseModel):
    asset_id: uuid.UUID
    version_id: uuid.UUID
    status: str


class CommentVisibilityChange(BaseModel):
    visibility: str = Field(pattern=r"^(public|internal)$")


class EventResponse(BaseModel):
    seq: int
    type: str
    project_id: uuid.UUID
    payload: dict
    created_at: datetime
    model_config = {"from_attributes": True}


class EventsPage(BaseModel):
    events: list[EventResponse]
    # Pass as `since` on the next call. Equal to the request's `since` when
    # there was nothing new.
    last_seq: int


class GatewayTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: Optional[list[str]] = None


class GatewayTokenResponse(BaseModel):
    id: uuid.UUID
    name: str
    scopes: list[str]
    created_at: datetime
    last_used_at: Optional[datetime]
    revoked_at: Optional[datetime]
    model_config = {"from_attributes": True}


class GatewayTokenCreated(GatewayTokenResponse):
    # Shown once. Only its hash is stored.
    token: str


class PersonResponse(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    model_config = {"from_attributes": True}


class TaskContext(BaseModel):
    """Everything a machine needs to start on a task, in one read."""
    task: TaskResponse
    project: dict
    asset: dict
    # The version the task was created on: its number, frame rate, length and
    # where it came from in Resolve, if the gateway recorded that.
    version: Optional[dict]
    source_comment: Optional[dict]
    notes: list[NoteResponse]
    assignee: Optional[PersonResponse]
    created_by: Optional[PersonResponse]


class ReviewContext(BaseModel):
    """What the review screen needs to know about the caller on this asset."""
    sees_internal: bool
    is_manager: bool
    default_comment_visibility: str
    can_choose_comment_visibility: bool


class MemberPut(BaseModel):
    """HQ puts someone on a project: by email, because FreeFrame accounts are made at
    their first sign-in (OIDC) and HQ may get there first."""
    email: str = Field(min_length=3, max_length=255)
    name: Optional[str] = Field(default=None, max_length=255)
    role: str = Field(pattern="^(editor|reviewer|viewer)$")


class MemberResult(BaseModel):
    user_id: uuid.UUID
    email: str
    role: str
    account_created: bool
    changed: bool


# ── Fix check ──────────────────────────────────────────────────────────────────

class FixCheckPut(BaseModel):
    """status None clears the check (back to "not checked")."""
    status: Optional[FixCheckStatus] = None
    note: Optional[str] = Field(default=None, max_length=10000)


class FixCheckItem(BaseModel):
    comment_id: uuid.UUID
    version_id: uuid.UUID
    version_number: Optional[int] = None
    timecode_start: Optional[float] = None
    timecode_end: Optional[float] = None
    body: str
    author_name: Optional[str] = None
    visibility: str
    drawing: Optional[dict] = None
    task_id: Optional[uuid.UUID] = None
    task_status: Optional[str] = None
    not_fixed_in: Optional[int] = None
    status: Optional[str] = None
    note: Optional[str] = None
    checked_by_name: Optional[str] = None
    checked_at: Optional[datetime] = None


class FixChecklist(BaseModel):
    version_id: uuid.UUID
    version_number: int
    previous_version_id: Optional[uuid.UUID] = None
    total: int
    checked: int
    items: list[FixCheckItem]
