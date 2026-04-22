"""SQLAlchemy ORM models for LabFlow's decision/task graph.

The schema is intentionally normalized so that:
  * meetings own decisions, tasks, experiments, assumptions, and blockers
  * tasks reference an owner and may depend on other tasks (DAG)
  * evidence items attach to tasks and drive automatic completion
  * decisions persist across meetings, forming a long-term decision graph
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    String,
    Table,
    Column,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


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
# Core entities
# ---------------------------------------------------------------------------
class Owner(Base):
    __tablename__ = "owners"

    id: Mapped[int] = mapped_column(primary_key=True)
    handle: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    tasks: Mapped[list["Task"]] = relationship(back_populates="owner")


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    meeting_type: Mapped[str] = mapped_column(String(64), default="standup")
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    transcript: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    finalized: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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

    id: Mapped[int] = mapped_column(primary_key=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"))
    statement: Mapped[str] = mapped_column(Text)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.8)
    superseded_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("decisions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    meeting: Mapped["Meeting"] = relationship(back_populates="decisions")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("owners.id"), nullable=True)
    due_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="open")  # open|in_progress|done|cancelled
    kind: Mapped[str] = mapped_column(String(32), default="task")  # task|code|experiment|review
    uncertainty: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1
    confidence: Mapped[float] = mapped_column(Float, default=0.8)
    source_span: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
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

    id: Mapped[int] = mapped_column(primary_key=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    hypothesis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    method: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metrics: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # CSV of metric names
    dataset: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="proposed")
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("owners.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    meeting: Mapped["Meeting"] = relationship(back_populates="experiments")
    owner: Mapped[Optional["Owner"]] = relationship()


class Assumption(Base):
    __tablename__ = "assumptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"))
    statement: Mapped[str] = mapped_column(Text)
    risk: Mapped[str] = mapped_column(String(16), default="medium")  # low|medium|high
    validated: Mapped[bool] = mapped_column(Boolean, default=False)

    meeting: Mapped["Meeting"] = relationship(back_populates="assumptions")


class Blocker(Base):
    __tablename__ = "blockers"

    id: Mapped[int] = mapped_column(primary_key=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"))
    description: Mapped[str] = mapped_column(Text)
    blocked_task_id: Mapped[Optional[int]] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)

    meeting: Mapped["Meeting"] = relationship(back_populates="blockers")


class Evidence(Base):
    """An artifact that may verify completion of a task.

    `kind` is one of: commit, doc, eval, checklist, link.
    `verified` is set when the verification engine matches the evidence to the
    task's keywords or owner.
    """

    __tablename__ = "evidence"
    __table_args__ = (UniqueConstraint("task_id", "uri", name="uq_evidence_task_uri"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    uri: Mapped[str] = mapped_column(String(512))
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    task: Mapped["Task"] = relationship(back_populates="evidence")
