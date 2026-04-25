"""Pydantic schemas for API I/O and extraction validation.

These schemas are the canonical contract for the LLM/rule-based extractor.
They are strict by default — extra fields raise — so we catch hallucinations
or schema drift early.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Extraction-time schemas (no DB ids)
# ---------------------------------------------------------------------------
RiskLevel = Literal["low", "medium", "high"]
TaskKind = Literal["task", "code", "experiment", "review"]
TaskStatus = Literal["open", "in_progress", "done", "cancelled"]
EvidenceKind = Literal["commit", "doc", "eval", "checklist", "link"]


class ExtractedOwner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handle: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)


class ExtractedDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(min_length=3)
    rationale: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.8)


class ExtractedTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3, max_length=255)
    description: Optional[str] = None
    owner_handle: Optional[str] = None
    due_date: Optional[datetime] = None
    kind: TaskKind = "task"
    uncertainty: float = Field(ge=0.0, le=1.0, default=0.0)
    confidence: float = Field(ge=0.0, le=1.0, default=0.8)
    depends_on_titles: List[str] = Field(default_factory=list)
    source_span: Optional[str] = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        v = v.strip().rstrip(".")
        if not v:
            raise ValueError("title may not be empty after stripping")
        return v


class ExtractedExperiment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=255)
    hypothesis: Optional[str] = None
    method: Optional[str] = None
    metrics: List[str] = Field(default_factory=list)
    dataset: Optional[str] = None
    owner_handle: Optional[str] = None


class ExtractedAssumption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(min_length=3)
    risk: RiskLevel = "medium"


class ExtractedBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(min_length=3)
    blocked_task_title: Optional[str] = None


class ExtractionResult(BaseModel):
    """Top-level structured output of the extraction pipeline."""

    model_config = ConfigDict(extra="forbid")

    owners: List[ExtractedOwner] = Field(default_factory=list)
    decisions: List[ExtractedDecision] = Field(default_factory=list)
    tasks: List[ExtractedTask] = Field(default_factory=list)
    experiments: List[ExtractedExperiment] = Field(default_factory=list)
    assumptions: List[ExtractedAssumption] = Field(default_factory=list)
    blockers: List[ExtractedBlocker] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# API request/response schemas
# ---------------------------------------------------------------------------
class MeetingCreate(BaseModel):
    title: str
    meeting_type: str = "standup"
    transcript: str = ""
    notes: str = ""
    occurred_at: Optional[datetime] = None


class MeetingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    meeting_type: str
    occurred_at: datetime
    finalized: bool


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    description: Optional[str]
    status: TaskStatus
    kind: TaskKind
    uncertainty: float
    due_date: Optional[datetime]
    owner_id: Optional[int]


class EvidenceCreate(BaseModel):
    task_id: int
    kind: EvidenceKind
    uri: str = Field(min_length=1, max_length=512)
    summary: Optional[str] = None


class EvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    task_id: int
    kind: EvidenceKind
    uri: str
    summary: Optional[str]
    score: float
    verified: bool


class TaskUpdate(BaseModel):
    """Strict update payload for ``PATCH /api/tasks/{id}``.

    Only listed fields are mutable; ``extra="forbid"`` ensures stray fields
    raise 422 instead of being silently ignored.
    """

    model_config = ConfigDict(extra="forbid")
    title: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    status: Optional[TaskStatus] = None
    due_date: Optional[datetime] = None
    uncertainty: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class PageMeta(BaseModel):
    """Pagination metadata returned alongside list endpoints."""

    total: int
    limit: int
    offset: int


class PaginatedTasks(BaseModel):
    items: list[TaskOut]
    page: PageMeta


class PaginatedMeetings(BaseModel):
    items: list[MeetingOut]
    page: PageMeta
