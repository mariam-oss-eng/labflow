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
    # v0.8 — scope tokens, comma-separated. NULL means "all scopes" (back-compat).
    # Recognised scopes: read, write, admin, webhook:emit, plugin:install
    scopes: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # v0.17 — key rotation: when set, this row is the *successor* of
    # ``rotated_from_id`` and the predecessor remains valid until
    # ``rotation_grace_until``. A nightly sweeper revokes expired predecessors.
    rotated_from_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True
    )
    rotation_grace_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

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
    # v0.8 — workflow / sprint / state-machine
    workflow_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True
    )
    sprint_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("sprints.id", ondelete="SET NULL"), nullable=True
    )
    state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    sla_breach_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # v0.12 — task priority (low|medium|high). Optional / nullable.
    priority: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # v0.14 — explicit effort estimate in hours (replaces dag.py heuristic
    # for tasks where it's been set; the heuristic is still used as fallback).
    effort_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

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
    # v0.10 — tamper-evident hash chain. ``prev_hash`` is the entry_hash of
    # the previous audit row for the same team (or 64 zeros for genesis);
    # ``entry_hash`` = sha256(canonical(prev_hash | row payload)). Both
    # columns are nullable for back-compat with rows written before v0.10;
    # the verifier treats NULL as "unchecked" rather than "broken".
    prev_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


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
    # v0.17 — when set, the delivery exhausted its retry cap and now lives
    # in the dead-letter queue. Set to NULL again on successful replay.
    dead_lettered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )


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
    # v0.12: hour-of-day (0..23, UTC) at which this key expects its digest.
    # NULL = "any time" — the legacy weekly behaviour.
    digest_hour_utc: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
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


# ---------------------------------------------------------------------------
# v0.8 — workflows, sprints, ACLs, scopes, share links
# ---------------------------------------------------------------------------
class Workflow(Base):
    """A configurable task state-machine for a team.

    ``definition_json`` is a JSON document of the form::

        {
          "states": ["open", "in_progress", "in_review", "closed"],
          "initial": "open",
          "terminal": ["closed"],
          "transitions": [
            {"from": "open", "to": "in_progress",
             "guard_role": "member"},
            {"from": "in_progress", "to": "in_review",
             "sla_hours": 48},
            ...
          ]
        }

    Exactly one workflow per team is marked ``is_default``; new tasks adopt
    the default workflow's ``initial`` state.
    """

    __tablename__ = "workflows"
    __table_args__ = (
        UniqueConstraint("team_id", "name", name="uq_workflow_team_name"),
        Index("ix_workflow_team", "team_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    definition_json: Mapped[str] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class Sprint(Base):
    """A time-boxed iteration. Tasks are assigned via ``Task.sprint_id``."""

    __tablename__ = "sprints"
    __table_args__ = (
        UniqueConstraint("team_id", "slug", name="uq_sprint_team_slug"),
        Index("ix_sprint_team_active", "team_id", "active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    slug: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    goal: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class ResourceAcl(Base):
    """Per-resource access control (in addition to RBAC role).

    A row grants a single API key (``api_key_id``) a permission level
    (``read`` / ``write``) on a single resource. The absence of any ACL row
    for a resource means "fall back to the role-based check" — ACLs only
    *restrict* access, they don't widen it. A row with ``api_key_id NULL``
    is a wildcard "everyone in the team" grant used to mark public-within-
    team resources without listing every key.
    """

    __tablename__ = "resource_acls"
    __table_args__ = (
        UniqueConstraint(
            "team_id", "entity_type", "entity_id", "api_key_id",
            name="uq_acl_unique",
        ),
        Index("ix_acl_entity", "team_id", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    entity_type: Mapped[str] = mapped_column(String(32))     # meeting | decision | task
    entity_id: Mapped[int] = mapped_column(Integer)
    api_key_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=True
    )
    permission: Mapped[str] = mapped_column(String(8), default="read")  # read | write
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class ShareLink(Base):
    """A signed, optionally passcode-gated link granting time-limited read.

    Tokens are stored *hashed* (SHA-256) — the plaintext token is shown to
    the operator exactly once at creation and embedded in the link.
    """

    __tablename__ = "share_links"
    __table_args__ = (
        Index("ix_share_token_hash", "token_hash", unique=True),
        Index("ix_share_team_entity", "team_id", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[int] = mapped_column(Integer)
    token_hash: Mapped[str] = mapped_column(String(64))
    passcode_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_by_key_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


# Note: ApiKey gains a ``scopes`` column (CSV of scope strings) via the v0.8
# migration; the column is declared on the ApiKey class above. Task gains
# workflow_id / sprint_id / state / sla_breach_at, declared on Task above.


# ---------------------------------------------------------------------------
# v0.9 — plugins, AI copilot sessions
# ---------------------------------------------------------------------------
class Plugin(Base):
    """A team-installed plugin from the LabFlow plugin marketplace.

    Plugins are *signed manifests* — operators install a plugin by
    submitting a manifest JSON and a SHA-256 hash. The system records
    the manifest, hash, author, and version; *enabling* a plugin makes
    it available to feature flags and the runtime registry.

    The execution boundary (loading code, sandboxing) is intentionally
    out of scope for v0.9 — this gives the team a discoverable catalogue
    without the security headache of arbitrary code execution. See
    ADR-0010 for the sandbox plan in v1.0.
    """

    __tablename__ = "plugins"
    __table_args__ = (
        UniqueConstraint("team_id", "name", name="uq_plugin_team_name"),
        Index("ix_plugin_team", "team_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(32))
    author: Mapped[str] = mapped_column(String(128))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    manifest_json: Mapped[str] = mapped_column(Text)
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    installed_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class CopilotSession(Base):
    """Persistent multi-turn AI copilot session with full audit trail.

    Each turn (user message + tool calls + assistant message) is appended
    to ``transcript_json`` (a JSON array) so operators can replay any
    interaction. Sessions are scoped to a team and (optionally) an actor
    API key.
    """

    __tablename__ = "copilot_sessions"
    __table_args__ = (Index("ix_copilot_team_created", "team_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    actor_key_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(255))
    transcript_json: Mapped[str] = mapped_column(Text, default="[]")
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class VectorIndexShard(Base):
    """Persistence row for a vector-index v2 shard (HNSW snapshot).

    The actual vectors live on disk under
    ``LABFLOW_VECTOR_INDEX_DIR/<team_id>/<shard_id>.idx``. This row is
    metadata: which model produced the snapshot, dimensionality, vector
    count, and the build timestamp. The runtime loader picks the most
    recent shard per ``(team_id, model)`` and falls back to brute-force
    when no shard exists.
    """

    __tablename__ = "vector_index_shards"
    __table_args__ = (
        Index("ix_vshard_team_model_built", "team_id", "model", "built_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    model: Mapped[str] = mapped_column(String(64))
    dim: Mapped[int] = mapped_column(Integer)
    vectors: Mapped[int] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64))
    built_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)




# ---------------------------------------------------------------------------
# v0.10 — automation rules, dashboards
# ---------------------------------------------------------------------------
class AutomationRule(Base):
    """Declarative when/then rule (v0.10).

    A rule has a trigger event name, an optional JSON condition, and a
    list of actions. Actions are dispatched by ``automation.dispatch``
    when an event with a matching name is published. Rules are JSON-only
    so operators can add/remove them without a code deploy.
    """

    __tablename__ = "automation_rules"
    __table_args__ = (
        UniqueConstraint("team_id", "name", name="uq_rule_team_name"),
        Index("ix_rule_team_event", "team_id", "trigger_event"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    trigger_event: Mapped[str] = mapped_column(String(64))
    condition_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    actions_json: Mapped[str] = mapped_column(Text)  # list of {kind, params}
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    fires: Mapped[int] = mapped_column(Integer, default=0)
    last_fired_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class Dashboard(Base):
    """Per-key customisable dashboard layout (v0.10)."""

    __tablename__ = "dashboards"
    __table_args__ = (
        UniqueConstraint("team_id", "owner_key_id", "slug",
                         name="uq_dashboard_team_owner_slug"),
        Index("ix_dashboard_team", "team_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    owner_key_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True
    )
    slug: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    layout_json: Mapped[str] = mapped_column(Text)  # list of widget specs
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


# ---------------------------------------------------------------------------
# v0.11 — knowledge base, watchers, entity links
# ---------------------------------------------------------------------------
class WikiPage(Base):
    """A markdown wiki page (v0.11). Slug-addressed, soft-deletable.

    The ``current_revision_id`` points at the active body; ``WikiRevision``
    is the immutable history. Backlinks are materialised in
    ``EntityLink`` so they survive renames.
    """

    __tablename__ = "wiki_pages"
    __table_args__ = (
        UniqueConstraint("team_id", "slug", name="uq_wiki_team_slug"),
        Index("ix_wiki_team", "team_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    slug: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(255))
    summary: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    body: Mapped[str] = mapped_column(Text, default="")
    current_revision_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class WikiRevision(Base):
    """Immutable revision row for :class:`WikiPage` (v0.11)."""

    __tablename__ = "wiki_revisions"
    __table_args__ = (Index("ix_wiki_rev_page", "page_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("wiki_pages.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String(128), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class EntityLink(Base):
    """Materialised cross-reference parsed from text (v0.11).

    Triggered by the smart-link parser on transcript / comment / wiki
    saves. ``source_type/id`` -> ``target_type/id`` with a ``kind`` of
    ``mention`` (@handle), ``ref`` (#task-123), or ``wikilink`` ([[Page]]).
    """

    __tablename__ = "entity_links"
    __table_args__ = (
        Index("ix_link_source", "team_id", "source_type", "source_id"),
        Index("ix_link_target", "team_id", "target_type", "target_id"),
        UniqueConstraint("team_id", "source_type", "source_id",
                         "target_type", "target_id", "kind",
                         name="uq_entity_link"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    source_type: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[int] = mapped_column(Integer)
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))  # mention|ref|wikilink
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class Watcher(Base):
    """Per-key subscription to entity-change events (v0.11)."""

    __tablename__ = "watchers"
    __table_args__ = (
        UniqueConstraint("team_id", "api_key_id", "entity_type", "entity_id",
                         name="uq_watcher"),
        Index("ix_watcher_entity", "team_id", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    api_key_id: Mapped[int] = mapped_column(
        ForeignKey("api_keys.id", ondelete="CASCADE")
    )
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[int] = mapped_column(Integer)
    delivery: Mapped[str] = mapped_column(String(16), default="feed")  # feed|email|slack
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


# ---------------------------------------------------------------------------
# v0.12 — Recurring tasks, API key quotas
# ---------------------------------------------------------------------------
class RecurringTask(Base):
    """Template that materialises new :class:`Task` rows on a cadence (v0.12).

    Cadences:
      * ``daily`` — every N days (``interval`` defaults to 1)
      * ``weekly`` — on ``day_of_week`` (0=Monday … 6=Sunday)
      * ``monthly`` — on ``day_of_month`` (1..28; >28 is clamped per month)

    The materialiser is idempotent: it advances ``next_run_at`` *after*
    creating the task, so re-running the sweeper before the next due
    boundary is a no-op.
    """

    __tablename__ = "recurring_tasks"
    __table_args__ = (
        Index("ix_recurring_team_active_due", "team_id", "active", "next_run_at"),
        UniqueConstraint("team_id", "slug", name="uq_recurring_slug"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    slug: Mapped[str] = mapped_column(String(80))
    cadence: Mapped[str] = mapped_column(String(16))  # daily|weekly|monthly
    interval: Mapped[int] = mapped_column(Integer, default=1)
    day_of_week: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    day_of_month: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    template_title: Mapped[str] = mapped_column(String(255))
    template_owner_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("owners.id", ondelete="SET NULL"), nullable=True
    )
    template_priority: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class ApiKeyQuota(Base):
    """Daily request quota for a single API key (v0.12)."""

    __tablename__ = "api_key_quotas"
    __table_args__ = (
        UniqueConstraint("api_key_id", name="uq_quota_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    api_key_id: Mapped[int] = mapped_column(
        ForeignKey("api_keys.id", ondelete="CASCADE")
    )
    daily_limit: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class ApiKeyUsage(Base):
    """Per-key, per-UTC-day request counter (v0.12).

    Composite unique key ``(api_key_id, day)`` lets the rate-limiter
    perform a single UPSERT per request.
    """

    __tablename__ = "api_key_usage"
    __table_args__ = (
        UniqueConstraint("api_key_id", "day", name="uq_usage_key_day"),
        Index("ix_usage_team_day", "team_id", "day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    api_key_id: Mapped[int] = mapped_column(
        ForeignKey("api_keys.id", ondelete="CASCADE")
    )
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD (UTC)
    count: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------------------
# v0.13 — Guest invites, smart lists
# ---------------------------------------------------------------------------
class Invite(Base):
    """A pending external-collaborator invitation (v0.13).

    On accept, a new guest :class:`ApiKey` is minted (with the configured
    ``role`` and optional ``scopes``) and one :class:`ResourceAcl` row is
    written for each ``(entity_type, entity_id)`` in ``acl_entries_json``
    so the guest only sees what was shared.
    """

    __tablename__ = "invites"
    __table_args__ = (
        Index("ix_invite_token_hash", "token_hash", unique=True),
        Index("ix_invite_team_status", "team_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(255))
    token_hash: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16), default="viewer")
    scopes: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    acl_entries_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|accepted|revoked
    created_by_key_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True
    )
    accepted_key_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class SmartList(Base):
    """Saved declarative task filter (v0.13).

    Filter is JSON: any of ``state``, ``assignee_handle``, ``label``,
    ``due_before`` (ISO date), ``sprint_slug``, ``priority``. Empty/
    missing keys are treated as wildcards.
    """

    __tablename__ = "smart_lists"
    __table_args__ = (
        UniqueConstraint("team_id", "slug", name="uq_smart_list_slug"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    slug: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(128))
    filter_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


# ---------------------------------------------------------------------------
# v0.14 — Time tracking, feature flags, smart-list subscriptions, MCP audit
# ---------------------------------------------------------------------------
class TimeEntry(Base):
    """Tracked time on a task (v0.14).

    Either an open entry (``ended_at IS NULL``) recording an in-progress
    timer, or a closed entry with both ``started_at`` and ``ended_at``
    populated. Manual entries set both at creation; timer entries get
    ``ended_at`` filled by a stop call.
    """

    __tablename__ = "time_entries"
    __table_args__ = (
        Index("ix_time_entries_task", "task_id"),
        Index("ix_time_entries_team_owner", "team_id", "owner_id"),
        Index("ix_time_entries_open", "team_id", "ended_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    owner_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("owners.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual|timer
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class FeatureFlag(Base):
    """Per-team feature flag (v0.14).

    Boolean (and optional JSON payload) toggle scoped to a team. Used by
    server-side gates and surfaced to the client through ``/api/feature-flags``.
    """

    __tablename__ = "feature_flags"
    __table_args__ = (
        UniqueConstraint("team_id", "key", name="uq_feature_flag_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(80))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    payload_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class SmartListSubscription(Base):
    """Webhook subscription for a smart list's run output (v0.14).

    A sweeper hashes the current run's task IDs and only fires the
    webhook when the digest differs from ``last_digest``.
    """

    __tablename__ = "smart_list_subscriptions"
    __table_args__ = (
        UniqueConstraint("smart_list_id", "webhook_url",
                         name="uq_smart_list_sub_url"),
        Index("ix_smart_list_sub_team", "team_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    smart_list_id: Mapped[int] = mapped_column(
        ForeignKey("smart_lists.id", ondelete="CASCADE")
    )
    webhook_url: Mapped[str] = mapped_column(String(500))
    secret: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    last_digest: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_fired_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


# ---------------------------------------------------------------------------
# v0.15 — Public share links (read-only, signed, TTL)
# ---------------------------------------------------------------------------
class PublicShare(Base):
    """A read-only public link to a single entity (v0.15).

    Token is the SHA-256 hash of a 32-byte random string returned to the
    creator exactly once. ``expires_at`` is enforced server-side.
    """

    __tablename__ = "public_shares"
    __table_args__ = (
        Index("ix_public_share_token", "token_hash", unique=True),
        Index("ix_public_share_team_entity", "team_id",
              "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    entity_type: Mapped[str] = mapped_column(String(32))   # decision|task|wiki
    entity_id: Mapped[int] = mapped_column(Integer)
    token_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(80), default="system")
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


# ---------------------------------------------------------------------------
# v0.16 — Custom fields, scheduled reports
# ---------------------------------------------------------------------------
class CustomFieldDef(Base):
    """A user-defined field attached to one entity kind, per team (v0.16).

    ``kind`` controls how the value is validated / coerced:

    * ``text`` — arbitrary string (max 4 KB)
    * ``number`` — coerced to float
    * ``date`` — ISO-8601 string, parsed to datetime on read
    * ``select`` — one of ``options`` (a JSON-encoded list of strings)
    """

    __tablename__ = "custom_field_defs"
    __table_args__ = (
        UniqueConstraint("team_id", "entity_type", "key",
                         name="uq_cfdef_team_entity_key"),
        Index("ix_cfdef_team_entity", "team_id", "entity_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    entity_type: Mapped[str] = mapped_column(String(32))   # task|decision
    key: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))          # text|number|date|select
    options_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class CustomFieldValue(Base):
    """A value for one (entity, field def) pair (v0.16)."""

    __tablename__ = "custom_field_values"
    __table_args__ = (
        UniqueConstraint("def_id", "entity_id", name="uq_cfval_def_entity"),
        Index("ix_cfval_team_entity", "team_id", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    def_id: Mapped[int] = mapped_column(
        ForeignKey("custom_field_defs.id", ondelete="CASCADE")
    )
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[int] = mapped_column(Integer)
    value: Mapped[str] = mapped_column(Text)               # always stored as text
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class ScheduledReport(Base):
    """A saved LFQL query that runs on a cadence (v0.16).

    ``cadence`` is one of ``hourly|daily|weekly``. The sweeper compares
    ``last_run_at`` against ``now()`` and fires due reports. When fired,
    the matching task IDs are POSTed to ``webhook_url`` (HMAC-signed if
    ``secret`` is set) and a :class:`ScheduledReportRun` row is appended.
    """

    __tablename__ = "scheduled_reports"
    __table_args__ = (
        UniqueConstraint("team_id", "name", name="uq_schedrep_team_name"),
        Index("ix_schedrep_team_due", "team_id", "next_run_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    query: Mapped[str] = mapped_column(Text)               # an LFQL expression
    cadence: Mapped[str] = mapped_column(String(16))       # hourly|daily|weekly
    webhook_url: Mapped[str] = mapped_column(String(500))
    secret: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class ScheduledReportRun(Base):
    """A single execution of a :class:`ScheduledReport` (v0.16)."""

    __tablename__ = "scheduled_report_runs"
    __table_args__ = (
        Index("ix_schedrep_run_report", "report_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("scheduled_reports.id", ondelete="CASCADE")
    )
    matched: Mapped[int] = mapped_column(Integer, default=0)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
