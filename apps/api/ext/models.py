"""Tables the review-task extension owns. Nothing upstream reads them.

Every foreign key into a table that upstream's retention GC hard-deletes
(projects, assets, versions, comments) carries ON DELETE CASCADE or SET NULL,
so the nightly purge needs no knowledge of these tables.
"""
import uuid
from datetime import datetime
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import (
    BigInteger, DateTime, Float, ForeignKey, Index, Integer, String, Text, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

try:
    from ..database import Base
except ImportError:  # alembic imports the models as a top-level package
    from database import Base


class TaskKind(str, PyEnum):
    cut_trim = "cut_trim"
    b_roll = "b_roll"
    music = "music"
    sfx = "sfx"
    caption_title = "caption_title"
    mograph = "mograph"
    audio = "audio"
    colour = "colour"
    question = "question"
    global_ = "global"


class TaskStage(str, PyEnum):
    selects = "selects"
    radio = "radio"
    rough = "rough"
    fine = "fine"
    finish = "finish"
    review = "review"
    deliver = "deliver"


class AssigneeType(str, PyEnum):
    person = "person"
    role_pool = "role_pool"
    agent = "agent"


class TaskStatus(str, PyEnum):
    open = "open"
    claimed = "claimed"
    in_progress = "in_progress"
    proposed = "proposed"
    needs_info = "needs_info"
    approved = "approved"
    cancelled = "cancelled"
    # There is no `rejected`: rejecting a proposal puts the task back to `open`
    # with the manager's note, and the event says it was a rejection.


class NoteKind(str, PyEnum):
    editor_note = "editor_note"
    agent_note = "agent_note"
    question = "question"
    reply = "reply"
    review = "review"       # a manager's note on approving or rejecting


class AuthorType(str, PyEnum):
    person = "person"
    agent = "agent"


class ReviewTask(Base):
    """One thing to change on a video, and its approval.

    A task is what needs changing. The attempts to do it (jobs, leases, which
    machine) belong to the gateway and are not stored here; `claimed_by` is the
    gateway's own description of who has it, kept only to be shown.
    """
    __tablename__ = "review_tasks"
    __table_args__ = (
        Index("ix_review_tasks_project_status", "project_id", "status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True)
    # NULL once that version has been purged; the task and its instruction stay.
    created_on_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("asset_versions.id", ondelete="SET NULL"), nullable=True)
    # Frames on `created_on_version`. Equal = a point. Both NULL = the whole video.
    frame_in: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    frame_out: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(String(20), nullable=False)
    assignee_type: Mapped[str] = mapped_column(String(20), nullable=False)
    assignee_person_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)
    assignee_role: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=TaskStatus.open.value, server_default=TaskStatus.open.value)
    # The status `needs_info` was entered from, so answering the question puts
    # the task back where it was.
    resume_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    source_comment_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("comments.id", ondelete="SET NULL"), nullable=True, index=True)
    result_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("asset_versions.id", ondelete="SET NULL"), nullable=True)
    claimed_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ReviewTaskNote(Base):
    """The internal thread on a task. There is no visibility column on purpose:
    a note cannot be made client-visible because nothing stores that it is."""
    __tablename__ = "review_task_notes"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("review_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    author_type: Mapped[str] = mapped_column(String(20), nullable=False, default=AuthorType.person.value)
    author_person_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    # For an agent: where it ran, as the gateway words it ("agent on garlopc for Laci").
    author_label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VersionExt(Base):
    """What the gateway knows about a version that upstream has no column for."""
    __tablename__ = "version_ext"
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("asset_versions.id", ondelete="CASCADE"), primary_key=True)
    resolve_project: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    resolve_timeline: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    start_timecode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    fps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    author_type: Mapped[str] = mapped_column(String(20), nullable=False, default=AuthorType.person.value)
    author_person_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    agent_machine: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    published_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class VersionResolvedTask(Base):
    """A version resolves these tasks."""
    __tablename__ = "version_resolved_tasks"
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("asset_versions.id", ondelete="CASCADE"), primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("review_tasks.id", ondelete="CASCADE"), primary_key=True, index=True)


class ExtEvent(Base):
    """The outbox. A row is written in the same transaction as the change it
    describes, so there is an event if and only if the change committed. `seq`
    is the order the gateway reads them in and its idempotency key."""
    __tablename__ = "ext_events"
    __table_args__ = (
        Index("ix_ext_events_undelivered", "seq", postgresql_where=text("delivered_at IS NULL")),
    )
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    # No foreign key: the log outlives a project that is purged from the trash.
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Webhook delivery. NULL delivered_at = still owed to the gateway.
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class GatewayToken(Base):
    """A service account for the Garlomedia gateway. Only the SHA-256 of the
    token is stored; the token itself is shown once, when it is created."""
    __tablename__ = "gateway_tokens"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    scopes: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
