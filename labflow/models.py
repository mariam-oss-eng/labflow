"""SQLAlchemy ORM models for LabFlow's decision/task graph.

The schema is intentionally normalized so that:
  * every entity belongs to a ``Team`` (workspace) — multi-tenancy is
    enforced at the row level.
  * meetings own decisions, tasks, experiments, assumptions, and blockers
  * tasks reference an owner and may depend on other tasks (DAG)
  * evidence items attach to tasks and drive automatic completion
  * decisions persist across meetings, forming a long-term decision graph
  * an ``AuditEvent`` row is appended for every meaningful state change so
    the system has a queryable history.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .crypto import EncryptedText
from .db import Base
from .time_utils import now_utc


# ---------------------------------------------------------------------------
# Association tables
# ---------------------------------------------------------------------------
task_dependency = Table(
    "task_dependency",
    Base.metadata,
    Column("task_id", ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
    Column("depends_on_id", ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
)


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------
class Team(Base):
    """A workspace. All tenant-scoped data has a ``team_id`` FK to this row."""

    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class ApiKey(Base):
    """A long-lived API key for a :class:`Team`. Plaintext is never stored."""

    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_team", "team_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(128))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    team: Mapped["Team"] = relationship()


# ---------------------------------------------------------------------------
# Core entities
# ---------------------------------------------------------------------------
class Owner(Base):
    """A person referenced in transcripts. Scoped to a team."""

    __tablename__ = "owners"
    __table_args__ = (
        UniqueConstraint("team_id", "handle", name="uq_owners_team_handle"),
        Index("ix_owners_team", "team_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    handle: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    tasks: Mapped[list["Task"]] = relationship(back_populates="owner")


class Meeting(Base):
    __tablename__ = "meetings"
    __table_args__ = (
        Index("ix_meetings_team_occurred_at", "team_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(255))
    meeting_type: Mapped[str] = mapped_column(String(64), default="standup")
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    transcript: Mapped[str] = mapped_column(EncryptedText, default="")
    notes: Mapped[str] = mapped_column(EncryptedText, default="")
    finalized: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    decisions: Mapped[list["Decision"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan"
    )
    tasks: Mapped[list["Task"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan"
    )
    experiments: Mapped[list["Experiment"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan"
    )
    assumptions: Mapped[list["Assumption"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan"
    )
    blockers: Mapped[list["Blocker"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan"
    )


class Decision(Base):
    __tablename__ = "decisions"
    __table_args__ = (Index("ix_decisions_team", "team_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"))
    statement: Mapped[str] = mapped_column(Text)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.8)
    superseded_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("decisions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    meeting: Mapped["Meeting"] = relationship(back_populates="decisions")


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_team_status", "team_id", "status"),
        Index("ix_tasks_due_date", "due_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("owners.id"), nullable=True)
    due_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="open")
    kind: Mapped[str] = mapped_column(String(32), default="task")
    uncertainty: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.8)
    source_span: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    meeting: Mapped["Meeting"] = relationship(back_populates="tasks")
    owner: Mapped[Optional["Owner"]] = relationship(back_populates="tasks")
    evidence: Mapped[list["Evidence"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    depends_on: Mapped[list["Task"]] = relationship(
        "Task",
        secondary=task_dependency,
        primaryjoin=id == task_dependency.c.task_id,
        secondaryjoin=id == task_dependency.c.depends_on_id,
        backref="dependents",
    )


class Experiment(Base):
    __tablename__ = "experiments"
    __table_args__ = (Index("ix_experiments_team", "team_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    hypothesis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    method: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metrics: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    dataset: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="proposed")
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("owners.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    meeting: Mapped["Meeting"] = relationship(back_populates="experiments")
    owner: Mapped[Optional["Owner"]] = relationship()


class Assumption(Base):
    __tablename__ = "assumptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"))
    statement: Mapped[str] = mapped_column(Text)
    risk: Mapped[str] = mapped_column(String(16), default="medium")
    validated: Mapped[bool] = mapped_column(Boolean, default=False)

    meeting: Mapped["Meeting"] = relationship(back_populates="assumptions")


class Blocker(Base):
    __tablename__ = "blockers"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"))
    description: Mapped[str] = mapped_column(Text)
    blocked_task_id: Mapped[Optional[int]] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)

    meeting: Mapped["Meeting"] = relationship(back_populates="blockers")


class Evidence(Base):
    """An artifact that may verify completion of a task."""

    __tablename__ = "evidence"
    __table_args__ = (
        UniqueConstraint("task_id", "uri", name="uq_evidence_task_uri"),
        Index("ix_evidence_team", "team_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    uri: Mapped[str] = mapped_column(String(512))
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    task: Mapped["Task"] = relationship(back_populates="evidence")


# ---------------------------------------------------------------------------
# v0.3 — jobs, audit, webhooks
# ---------------------------------------------------------------------------
class Job(Base):
    """Background job tracking row.

    Used by the in-process worker for asynchronous extraction and other
    long-running operations. The state machine is:

        queued → running → (completed | failed | cancelled)
    """

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_team_status", "team_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # JSON
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)


class AuditEvent(Base):
    """Append-only audit row written for every meaningful state transition."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_team_created", "team_id", "created_at"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    actor: Mapped[str] = mapped_column(String(128), default="system")
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class WebhookSubscription(Base):
    """Outbound webhook subscription for a team."""

    __tablename__ = "webhook_subscriptions"
    __table_args__ = (Index("ix_webhooks_team", "team_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(String(512))
    event: Mapped[str] = mapped_column(String(64))  # "*" matches all events
    secret: Mapped[str] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class WebhookDelivery(Base):
    """Record of one outbound webhook delivery attempt."""

    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("webhook_subscriptions.id", ondelete="CASCADE")
    )
    event: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)
    status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    response_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


# ---------------------------------------------------------------------------
# v0.4 — embeddings, idempotency
# ---------------------------------------------------------------------------
class Embedding(Base):
    """A semantic vector attached to an entity (decision, task, …).

    Stored as a JSON-serialized ``list[float]`` so we don't depend on a
    pgvector extension. ``model`` records which embedder produced the
    vector — when an operator switches embedders, vectors with the wrong
    ``model`` value are ignored (and re-computed lazily) so similarity
    search never mixes incompatible spaces.
    """

    __tablename__ = "embeddings"
    __table_args__ = (
        UniqueConstraint("team_id", "entity_type", "entity_id", "model",
                         name="uq_embeddings_entity_model"),
        Index("ix_embeddings_team_entity", "team_id", "entity_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    entity_type: Mapped[str] = mapped_column(String(32))   # "decision" | "task"
    entity_id: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(String(64))         # e.g. "hash-bow"
    dim: Mapped[int] = mapped_column(Integer)
    vector: Mapped[str] = mapped_column(Text)              # JSON-encoded list[float]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class IdempotencyRecord(Base):
    """Replay-protection cache for ``POST`` requests with ``Idempotency-Key``.

    A key is unique per ``(team_id, key)`` and stores the request hash plus
    the response so identical retries return the same payload. Mismatched
    bodies for the same key return 409 to surface programmer errors.
    """

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("team_id", "key", name="uq_idempotency_team_key"),
        Index("ix_idempotency_expires", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(255))
    status_code: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[str] = mapped_column(Text)
    response_content_type: Mapped[str] = mapped_column(String(64), default="application/json")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


# ---------------------------------------------------------------------------
# v0.5 — RBAC
# ---------------------------------------------------------------------------
class Membership(Base):
    """Binds an :class:`ApiKey` to a role within a :class:`Team`.

    The default role is ``"member"``. Production deployments should
    explicitly create ``admin`` keys for human operators and ``viewer``
    keys for read-only integrations.

    A row is implicitly created for the bootstrap key with role ``admin``;
    additional roles are managed via the CLI (``labflow keys grant``).
    """

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("api_key_id", name="uq_membership_api_key"),
        Index("ix_membership_team_role", "team_id", "role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16), default="member")  # admin | member | viewer
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


# ---------------------------------------------------------------------------
# v0.6 — collaboration & insights
# ---------------------------------------------------------------------------
class Comment(Base):
    """A threaded comment on a decision or task.

    Authorship records the API key id (``actor_key_id``) and a denormalized
    ``actor`` label so audit trails remain meaningful even if a key is
    later revoked. Replies form a tree via ``parent_id``.
    """

    __tablename__ = "comments"
    __table_args__ = (
        Index("ix_comments_team_entity", "team_id", "entity_type", "entity_id"),
        Index("ix_comments_parent", "parent_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    entity_type: Mapped[str] = mapped_column(String(32))     # "decision" | "task"
    entity_id: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("comments.id", ondelete="CASCADE"), nullable=True
    )
    actor: Mapped[str] = mapped_column(String(128), default="system")
    actor_key_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True
    )
    body: Mapped[str] = mapped_column(Text)
    edited_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class Reaction(Base):
    """An emoji reaction by an API key on any entity.

    ``(team_id, entity_type, entity_id, emoji, actor_key_id)`` is unique so
    each user can only react once per emoji per target. ``actor_key_id`` may
    be NULL in single-team mode; we then dedupe by ``actor`` label instead.
    """

    __tablename__ = "reactions"
    __table_args__ = (
        UniqueConstraint(
            "team_id", "entity_type", "entity_id", "emoji", "actor",
            name="uq_reactions_unique",
        ),
        Index("ix_reactions_entity", "team_id", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[int] = mapped_column(Integer)
    emoji: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(128), default="system")
    actor_key_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class SavedSearch(Base):
    """A named, persisted search query for a team.

    ``query`` is the literal text submitted to ``/api/search`` and
    ``alpha``/``filters`` are the optional ranking + filter overrides. Users
    can pin saved searches to the dashboard sidebar.
    """

    __tablename__ = "saved_searches"
    __table_args__ = (
        UniqueConstraint("team_id", "slug", name="uq_saved_search_team_slug"),
        Index("ix_saved_search_team", "team_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    slug: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    query: Mapped[str] = mapped_column(Text)
    alpha: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    filters: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class NotificationPref(Base):
    """Per-API-key notification preferences (digest cadence, channels, mute).

    A row is auto-created on first read with sensible defaults — operators
    don't need to backfill.
    """

    __tablename__ = "notification_prefs"
    __table_args__ = (
        UniqueConstraint("api_key_id", name="uq_notif_pref_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"))
    digest_cadence: Mapped[str] = mapped_column(String(16), default="weekly")  # off|daily|weekly
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    muted_events: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # JSON list
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


# ---------------------------------------------------------------------------
# v0.7 — distributed worker coordination
# ---------------------------------------------------------------------------
class WorkerLock(Base):
    """A coarse-grained distributed lock used to single-instance the worker.

    A row per ``name`` holds the current owner and an expiry. Acquire = INSERT
    with conflict-on-update only when expired. Heartbeats extend ``expires_at``.
    """

    __tablename__ = "worker_locks"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128))
    acquired_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


