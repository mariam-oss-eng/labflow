"""FastAPI application factory and routes."""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit as audit_mod
from . import digest as digest_mod
from . import __version__
from . import (
    analytics as analytics_mod,
    acl as acl_mod,
    automation as automation_mod,
    backup as backup_mod,
    calendar_feed,
    collab as collab_mod,
    copilot as copilot_mod,
    csv_export,
    dag,
    dashboards as dashboards_mod,
    exports,
    forecasting as forecasting_mod,
    graph as graph_mod,
    graphql_api,
    i18n,
    jobs as jobs_mod,
    links as links_mod,
    metrics,
    models,
    notif_prefs,
    otel,
    plugin_marketplace,
    replica as replica_mod,
    retention as retention_mod,
    saved_searches,
    scopes as scopes_mod,
    search as search_mod,
    services,
    slack as slack_mod,
    sprints as sprints_mod,
    sse as sse_mod,
    summary as summary_mod,
    timetravel,
    vector_index_v2,
    verification,
    watchers as watchers_mod,
    webhooks as webhooks_mod,
    wiki as wiki_mod,
    workflows as workflows_mod,
    ws as ws_mod,
)
from .auth import build_team_dependency, ensure_bootstrap_team, require_role
from .config import get_settings
from .db import get_session_factory, init_db
from .errors import (
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationError as LFValidationError,
    install_exception_handlers,
)
from .extraction import extract
from .logging_setup import configure_logging, set_request_id
from .schemas import (
    EvidenceCreate,
    EvidenceOut,
    MeetingCreate,
    MeetingOut,
    PageMeta,
    PaginatedTasks,
    PaginatedMeetings,
    TaskOut,
    TaskUpdate,
)
from .time_utils import now_utc

# v0.6 / v0.7 request bodies — defined at module scope so FastAPI's
# dependency-injection machinery recognizes them as pydantic models
# (locally-scoped subclasses inside create_app() get treated as query
# params, which surfaces as "Field required" 422s).
from pydantic import BaseModel as _BM


class _CommentIn(_BM):
    entity_type: str
    entity_id: int
    body: str
    parent_id: int | None = None


class _ReactionIn(_BM):
    entity_type: str
    entity_id: int
    emoji: str


class _SavedSearchIn(_BM):
    name: str
    query: str
    slug: str | None = None
    alpha: float | None = None
    filters: dict | None = None
    pinned: bool = False


class _NotifPrefIn(_BM):
    digest_cadence: str | None = None
    email: str | None = None
    muted_events: list[str] | None = None


class _GraphQLIn(_BM):
    query: str


# v0.8 input bodies
class _WorkflowIn(_BM):
    name: str
    definition: dict
    make_default: bool = False


class _TaskTransitionIn(_BM):
    to_state: str


class _SprintIn(_BM):
    name: str
    starts_at: datetime  # type: ignore[name-defined]
    ends_at: datetime    # type: ignore[name-defined]
    goal: str | None = None
    slug: str | None = None


class _SprintAssignIn(_BM):
    task_id: int


class _AclGrantIn(_BM):
    entity_type: str
    entity_id: int
    api_key_id: int | None = None
    permission: str = "read"


class _ShareLinkIn(_BM):
    entity_type: str
    entity_id: int
    ttl_hours: int = 24 * 7
    passcode: str | None = None


class _DependencyIn(_BM):
    depends_on_id: int


# v0.9 input bodies
class _CopilotStartIn(_BM):
    title: str = "New session"


class _CopilotTurnIn(_BM):
    message: str


class _PluginInstallIn(_BM):
    manifest: dict
    expected_sha256: str | None = None


class _PluginEnableIn(_BM):
    enabled: bool


# v0.10 input bodies
class _RuleIn(_BM):
    name: str
    trigger_event: str
    actions: list[dict]
    condition: dict | None = None
    enabled: bool = True


class _BackupRestoreIn(_BM):
    envelope: dict
    secret: str
    new_slug: str | None = None


class _DashboardIn(_BM):
    slug: str
    name: str
    layout: list
    is_default: bool = False


# v0.11 input bodies
class _WikiUpsertIn(_BM):
    title: str
    body: str = ""
    slug: str | None = None
    summary: str | None = None


class _WatchIn(_BM):
    entity_type: str
    entity_id: int
    delivery: str = "feed"


WEB_DIR = Path(__file__).parent / "web"
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))

log = logging.getLogger("labflow.api")


def _html_escape(text: str) -> str:
    """Minimal HTML escape for HTMX fragment helpers — Jinja autoescape
    doesn't apply to f-strings built outside the template engine."""
    import html
    return html.escape(text or "", quote=True)


def get_db():
    """Yield a transactional session, committing on success and rolling back on error."""
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _clamp_page_size(size: int | None) -> int:
    settings = get_settings()
    if size is None or size <= 0:
        return settings.default_page_size
    return min(size, settings.max_page_size)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)

    app = FastAPI(
        title="LabFlow",
        version="0.15.0",
        description=(
            "Meeting-to-execution OS for research and technical teams. "
            "Turns transcripts into a queryable graph of decisions, tasks, "
            "experiments, and evidence. v0.10 adds a tamper-evident audit "
            "chain, signed backup/restore, an automation-rules engine, "
            "burndown forecasting, and customisable dashboards. v0.11 adds "
            "a knowledge-base wiki with backlinks, smart entity links, "
            "GraphQL mutations, watchers + activity feed, and a "
            "TypeScript SDK. v0.12 adds an interactive Kanban board, "
            "recurring tasks, per-key API quotas, bulk task operations, "
            "and per-key digest scheduling. v0.13 adds federated guest "
            "invites with scoped ACLs, declarative smart-list filters, "
            "Markdown bundle export, and an interactive CLI REPL. "
            "v0.14 adds task time tracking + effort estimates, per-team "
            "feature flags, smart-list change subscriptions, and an "
            "MCP-style JSON-RPC tool endpoint for external LLM agents. "
            "v0.15 adds an HTMX task list with inline transitions, "
            "public time-bound read-only share links, and an OpenAPI "
            "→ stdlib Python client generator (`labflow gen-sdk`). "
            "v0.16 adds **LFQL** (a small boolean query language), "
            "per-team **custom fields** for tasks/decisions, and "
            "**scheduled reports** that fire LFQL queries at "
            "hourly/daily/weekly cadence. v0.17 adds a webhook "
            "**dead-letter queue** with replay/discard, an ANSI "
            "**terminal dashboard** (`labflow tui`), an "
            "**activity heatmap** (JSON + standalone SVG), and "
            "**API key rotation** with a configurable grace window."
        ),
        contact={"name": "LabFlow", "url": "https://github.com/mariam-oss-eng/labflow"},
        license_info={"name": "MIT"},
        openapi_tags=[
            {"name": "meetings", "description": "Upload, extract, finalize, export, summarize."},
            {"name": "tasks", "description": "Action items with owners, deadlines, evidence."},
            {"name": "evidence", "description": "Artifacts that verify task completion."},
            {"name": "search", "description": "Hybrid keyword + semantic search."},
            {"name": "graph", "description": "Decision graph with supersession edges."},
            {"name": "jobs", "description": "Background job introspection."},
            {"name": "collab", "description": "Threaded comments and emoji reactions (v0.6)."},
            {"name": "analytics", "description": "Cycle time, throughput, completion rate (v0.6)."},
            {"name": "calendar", "description": "iCalendar feed of upcoming task due dates (v0.6)."},
            {"name": "saved-searches", "description": "Named, persisted search queries (v0.6)."},
            {"name": "graphql", "description": "Read-only GraphQL endpoint (v0.7)."},
            {"name": "workflows", "description": "Configurable task state machines (v0.8)."},
            {"name": "sprints", "description": "Time-boxed iterations with burndown (v0.8)."},
            {"name": "dag", "description": "Task dependency graph + critical path (v0.8)."},
            {"name": "acl", "description": "Resource-level ACLs and share links (v0.8)."},
            {"name": "exports", "description": "CSV / JSON exports for tasks and decisions (v0.8)."},
            {"name": "copilot", "description": "Multi-step AI agent over team data (v0.9)."},
            {"name": "plugins", "description": "Plugin marketplace install/enable lifecycle (v0.9)."},
            {"name": "vector", "description": "Persistent HNSW-style vector index (v0.9)."},
            {"name": "timetravel", "description": "?as_of= queries for historical state (v0.9)."},
            {"name": "i18n", "description": "Localised UI catalogues (v0.9)."},
            {"name": "automation", "description": "Declarative when/then rules engine (v0.10)."},
            {"name": "backup", "description": "Signed full-team backup & restore (v0.10)."},
            {"name": "forecast", "description": "Sprint completion ETAs and per-task forecasts (v0.10)."},
            {"name": "dashboards", "description": "Per-key customisable widget layouts (v0.10)."},
            {"name": "wiki", "description": "Markdown knowledge-base with backlinks and history (v0.11)."},
            {"name": "links", "description": "Smart entity links: #task-N, [[Page]], @handle (v0.11)."},
            {"name": "watchers", "description": "Per-key entity subscriptions and activity feed (v0.11)."},
            {"name": "board", "description": "Kanban-style task board over a workflow (v0.12)."},
            {"name": "recurring", "description": "Templates that materialise tasks on a cadence (v0.12)."},
            {"name": "quotas", "description": "Per-API-key daily request quotas (v0.12)."},
            {"name": "notifications", "description": "Per-key digest cadence and scheduling (v0.12)."},
            {"name": "invites", "description": "Federated guest invites with scoped ACLs (v0.13)."},
            {"name": "smart-lists", "description": "Saved declarative task filters (v0.13)."},
            {"name": "time", "description": "Task time tracking with timers and manual entries (v0.14)."},
            {"name": "feature-flags", "description": "Per-team feature flags (v0.14)."},
            {"name": "mcp", "description": "MCP-style JSON-RPC tool endpoint for LLM agents (v0.14)."},
            {"name": "shares", "description": "Time-bound public read-only share links (v0.15)."},
            {"name": "lfql", "description": "LabFlow Query Language — boolean filter DSL (v0.16)."},
            {"name": "custom-fields", "description": "Per-team custom fields on tasks / decisions (v0.16)."},
            {"name": "reports", "description": "Scheduled LFQL reports with webhook delivery (v0.16)."},
            {"name": "webhook-dlq", "description": "Webhook dead-letter queue: replay / discard (v0.17)."},
            {"name": "heatmap", "description": "Activity heatmap (JSON + standalone SVG) (v0.17)."},
            {"name": "key-rotation", "description": "API key rotation with grace window (v0.17)."},
            {"name": "admin", "description": "Team-scoped administration: export, erase, retention."},
            {"name": "system", "description": "Health, readiness, metrics, live updates (SSE/WS)."},
        ],
    )

    # In test/dev we still call create_all so the app boots without a
    # separate `alembic upgrade`. In production, run `alembic upgrade head`
    # before starting and disable this by setting LABFLOW_AUTO_CREATE=false.
    init_db()
    # Optional OpenTelemetry instrumentation (v0.7).
    if otel.setup():
        otel.instrument_fastapi(app)
        try:
            from .db import get_engine
            otel.instrument_sqlalchemy(get_engine())
        except Exception:  # noqa: BLE001
            log.debug("otel sqlalchemy hook skipped", exc_info=True)
    # Ensure bootstrap team exists in single-team mode.
    SessionLocal = get_session_factory()
    with SessionLocal() as bootstrap_session:
        ensure_bootstrap_team(bootstrap_session)

    install_exception_handlers(app)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # v0.4 — rate limit + idempotency. Order: rate limit OUTSIDE
    # idempotency so a flood of replays still gets 429'd.
    from .idempotency import IdempotencyMiddleware
    from .ratelimit import RateLimitMiddleware
    app.add_middleware(IdempotencyMiddleware)
    app.add_middleware(RateLimitMiddleware)

    static_dir = WEB_DIR / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ---- middleware: request-id + access log + simple latency header ------
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = rid
        token = set_request_id(rid)
        start = time.perf_counter()
        try:
            response: Response = await call_next(request)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            log.info(
                "http_request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round(elapsed_ms, 2),
                },
            )
            try:
                set_request_id(None)
            except Exception:
                pass
            # ``token`` is a contextvars.Token — we used set_request_id which
            # returned a token; reset to clear leakage between requests.
            try:
                from .logging_setup import _request_id_ctx
                _request_id_ctx.reset(token)
            except Exception:
                pass
        response.headers["x-request-id"] = rid
        response.headers["x-response-time-ms"] = f"{elapsed_ms:.2f}"
        return response

    # ---- auth dependency (team scoping) -----------------------------------
    require_team = build_team_dependency(get_db)

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def landing(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request, "index.html", {"title": "LabFlow"}
        )

    @app.get("/app", response_class=HTMLResponse, include_in_schema=False)
    def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
        # The dashboard always shows the bootstrap team in single-team mode,
        # or the team identified by header in auth-enabled mode (best effort
        # — pages don't enforce auth so users get a friendly empty view).
        team = None
        if not settings.auth_enabled:
            team = db.execute(
                select(models.Team).where(models.Team.slug == settings.bootstrap_team)
            ).scalar_one_or_none()
        meetings = []
        open_tasks = []
        if team is not None:
            meetings = list(
                db.execute(
                    select(models.Meeting)
                    .where(models.Meeting.team_id == team.id)
                    .order_by(models.Meeting.occurred_at.desc())
                ).scalars()
            )
            open_tasks = services.list_open_tasks(db, team_id=team.id)
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "title": "LabFlow — Dashboard",
                "meetings": meetings,
                "open_tasks": open_tasks,
                "version": __version__,
            },
        )

    @app.get("/meetings/{meeting_id}/review", response_class=HTMLResponse,
             include_in_schema=False)
    def review(meeting_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
        meeting = db.get(models.Meeting, meeting_id)
        if meeting is None:
            raise NotFoundError("meeting not found")
        # Render the team-wide decision graph as Mermaid for the review page.
        mermaid = graph_mod.render_mermaid(
            graph_mod.build_graph(db, team_id=meeting.team_id)
        )
        return TEMPLATES.TemplateResponse(
            request,
            "review.html",
            {"title": meeting.title, "meeting": meeting,
             "mermaid": mermaid, "version": __version__},
        )

    @app.get("/app/search", response_class=HTMLResponse, include_in_schema=False)
    def search_fragment(
        q: str = "",
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> HTMLResponse:
        """HTMX fragment used by the dashboard search box. Returns a small
        rendered list rather than a full template to keep the round-trip
        snappy."""
        if not (q or "").strip():
            return HTMLResponse("")
        hits = search_mod.search(db, team_id=team.id, query=q, limit=15)
        if not hits:
            return HTMLResponse(
                '<div class="text-slate-400">No matches yet.</div>'
            )
        items = []
        for h in hits:
            href = (f"/meetings/{h.meeting_id}/review"
                    if h.meeting_id else "#")
            items.append(
                f'<li class="py-2"><a href="{href}" class="block hover:bg-slate-50 rounded px-2 -mx-2">'
                f'<span class="inline-block rounded bg-slate-100 px-1.5 py-0.5 '
                f'text-[10px] font-medium uppercase text-slate-600 mr-2">{h.kind}</span>'
                f'<span class="font-medium">{_html_escape(h.title)}</span>'
                f'<span class="ml-2 text-xs text-slate-400">score {h.score:.2f}</span>'
                f'<div class="text-xs text-slate-500 mt-0.5">{_html_escape(h.snippet)}</div>'
                f'</a></li>'
            )
        body = ('<ul class="divide-y divide-slate-100">' + "".join(items) +
                "</ul>")
        return HTMLResponse(body)

    # ----------------------------------------------------------- meeting API
    @app.post("/api/meetings", response_model=MeetingOut, status_code=201)
    def create_meeting(
        payload: MeetingCreate,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> MeetingOut:
        if len(payload.transcript) + len(payload.notes) > settings.max_transcript_bytes:
            raise PayloadTooLargeError("transcript + notes exceed size limit")
        meeting = models.Meeting(
            team_id=team.id,
            title=payload.title,
            meeting_type=payload.meeting_type,
            transcript=payload.transcript,
            notes=payload.notes,
            occurred_at=payload.occurred_at or now_utc().replace(tzinfo=None),
        )
        db.add(meeting)
        db.flush()
        if payload.transcript or payload.notes:
            result = extract(
                payload.transcript + "\n" + payload.notes, reference=meeting.occurred_at
            )
            services.persist_extraction(db, meeting, result)
        return MeetingOut.model_validate(meeting)

    @app.post("/api/meetings/upload", response_model=MeetingOut, status_code=201)
    async def upload_meeting(
        title: str = Form(...),
        meeting_type: str = Form("standup"),
        transcript_file: UploadFile | None = None,
        notes: str = Form(""),
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> MeetingOut:
        transcript_bytes = await transcript_file.read() if transcript_file else b""
        if len(transcript_bytes) > settings.max_transcript_bytes:
            raise PayloadTooLargeError("transcript exceeds size limit")
        transcript = transcript_bytes.decode("utf-8", errors="replace")
        meeting = models.Meeting(
            team_id=team.id,
            title=title,
            meeting_type=meeting_type,
            transcript=transcript,
            notes=notes,
        )
        db.add(meeting)
        db.flush()
        result = extract(transcript + "\n" + notes, reference=meeting.occurred_at)
        services.persist_extraction(db, meeting, result)
        return MeetingOut.model_validate(meeting)

    def _get_team_meeting(db: Session, meeting_id: int, team: models.Team) -> models.Meeting:
        meeting = db.get(models.Meeting, meeting_id)
        if meeting is None or meeting.team_id != team.id:
            raise NotFoundError("meeting not found")
        return meeting

    @app.post("/api/meetings/{meeting_id}/extract", response_model=MeetingOut)
    def reextract(
        meeting_id: int,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> MeetingOut:
        meeting = _get_team_meeting(db, meeting_id, team)
        if meeting.finalized:
            raise ConflictError("meeting already finalized")
        result = extract(meeting.transcript + "\n" + meeting.notes,
                         reference=meeting.occurred_at)
        services.persist_extraction(db, meeting, result)
        return MeetingOut.model_validate(meeting)

    @app.post("/api/meetings/{meeting_id}/finalize", response_model=MeetingOut, tags=["meetings"])
    def finalize(
        meeting_id: int,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> MeetingOut:
        meeting = _get_team_meeting(db, meeting_id, team)
        if not meeting.finalized:
            meeting.finalized = True
            audit_mod.record(
                db, team_id=team.id, action="meeting.finalized",
                entity_type="meeting", entity_id=meeting.id,
            )
            webhooks_mod.emit(db, team_id=team.id, event="meeting.finalized",
                              payload={"meeting_id": meeting.id, "title": meeting.title})
            metrics.inc("labflow_meetings_finalized_total", team=team.slug)
            sse_mod.hub().publish(team.id, "meeting.finalized", {
                "meeting_id": meeting.id, "title": meeting.title,
            })
        db.flush()
        return MeetingOut.model_validate(meeting)

    @app.get("/api/meetings", response_model=PaginatedMeetings)
    def list_meetings(
        limit: int | None = None,
        offset: int = 0,
        meeting_type: str | None = None,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> PaginatedMeetings:
        size = _clamp_page_size(limit)
        offset = max(offset, 0)
        stmt = select(models.Meeting).where(models.Meeting.team_id == team.id)
        if meeting_type:
            stmt = stmt.where(models.Meeting.meeting_type == meeting_type)
        # cheap count via a separate query — adequate for MVP scale.
        from sqlalchemy import func as sa_func
        count_stmt = select(sa_func.count()).select_from(stmt.subquery())
        total = db.execute(count_stmt).scalar_one()
        rows = list(
            db.execute(
                stmt.order_by(models.Meeting.occurred_at.desc())
                .offset(offset)
                .limit(size)
            ).scalars()
        )
        return PaginatedMeetings(
            items=[MeetingOut.model_validate(m) for m in rows],
            page=PageMeta(total=total, limit=size, offset=offset),
        )

    @app.get("/api/meetings/{meeting_id}/export.json")
    def export_json(
        meeting_id: int,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> JSONResponse:
        meeting = _get_team_meeting(db, meeting_id, team)
        return JSONResponse(exports.meeting_to_dict(meeting))

    @app.get("/api/meetings/{meeting_id}/export.md", response_class=PlainTextResponse)
    def export_md(
        meeting_id: int,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> str:
        meeting = _get_team_meeting(db, meeting_id, team)
        return exports.meeting_to_markdown(meeting)

    # ----------------------------------------------------------- task API
    @app.get("/api/tasks", response_model=PaginatedTasks)
    def list_tasks(
        limit: int | None = None,
        offset: int = 0,
        status: str | None = None,
        owner: str | None = None,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> PaginatedTasks:
        size = _clamp_page_size(limit)
        offset = max(offset, 0)
        stmt = select(models.Task).where(models.Task.team_id == team.id)
        if status:
            stmt = stmt.where(models.Task.status == status)
        if owner:
            stmt = (
                stmt.join(models.Owner, models.Task.owner_id == models.Owner.id)
                    .where(models.Owner.handle == owner)
            )
        from sqlalchemy import func as sa_func
        total = db.execute(select(sa_func.count()).select_from(stmt.subquery())).scalar_one()
        rows = list(
            db.execute(
                stmt.order_by(models.Task.id).offset(offset).limit(size)
            ).scalars()
        )
        return PaginatedTasks(
            items=[TaskOut.model_validate(t) for t in rows],
            page=PageMeta(total=total, limit=size, offset=offset),
        )

    @app.patch("/api/tasks/{task_id}", response_model=TaskOut)
    def update_task(
        task_id: int,
        payload: TaskUpdate,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> TaskOut:
        task = db.get(models.Task, task_id)
        if task is None or task.team_id != team.id:
            raise NotFoundError("task not found")
        data: dict[str, Any] = payload.model_dump(exclude_unset=True)
        previous_status = task.status
        for key, value in data.items():
            if key == "status" and value == "done" and task.status != "done":
                task.closed_at = now_utc().replace(tzinfo=None)
            setattr(task, key, value)
        db.flush()
        if "status" in data and data["status"] != previous_status:
            audit_mod.record(
                db, team_id=team.id, action="task.status_changed",
                entity_type="task", entity_id=task.id,
                metadata={"from": previous_status, "to": task.status},
            )
            if task.status == "done":
                webhooks_mod.emit(db, team_id=team.id, event="task.closed",
                                  payload={"task_id": task.id, "title": task.title})
                metrics.inc("labflow_tasks_closed_total", team=team.slug)
                sse_mod.hub().publish(team.id, "task.closed", {
                    "task_id": task.id, "title": task.title,
                })
        return TaskOut.model_validate(task)

    # ------------------------------------------------------------ evidence
    @app.post("/api/evidence", response_model=EvidenceOut, status_code=201)
    def add_evidence(
        payload: EvidenceCreate,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> EvidenceOut:
        task = db.get(models.Task, payload.task_id)
        if task is None or task.team_id != team.id:
            raise NotFoundError("task not found")
        ev = models.Evidence(
            team_id=team.id,
            task=task,
            kind=payload.kind,
            uri=payload.uri,
            summary=payload.summary,
        )
        db.add(ev)
        db.flush()
        verification.verify_evidence(db, ev)
        if ev.verified:
            audit_mod.record(
                db, team_id=team.id, action="evidence.verified",
                entity_type="evidence", entity_id=ev.id,
                metadata={"task_id": task.id, "score": ev.score},
            )
            webhooks_mod.emit(db, team_id=team.id, event="evidence.verified",
                              payload={"task_id": task.id, "uri": ev.uri})
            metrics.inc("labflow_evidence_verified_total", team=team.slug)
            sse_mod.hub().publish(team.id, "evidence.verified", {
                "task_id": task.id, "uri": ev.uri, "score": ev.score,
            })
        return EvidenceOut.model_validate(ev)

    # ------------------------------------------------------------ search
    @app.get("/api/search", tags=["search"])
    def search(
        q: str,
        limit: int = 25,
        alpha: float | None = None,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        """Hybrid keyword + semantic search across decisions and tasks.

        ``alpha`` (0..1) blends the two signals — ``0`` is pure lexical,
        ``1`` is pure semantic, default uses ``LABFLOW_SEARCH_ALPHA``.
        Each result also includes ``score_components`` for explainability.
        """
        hits = search_mod.search(
            db, team_id=team.id, query=q,
            limit=min(max(limit, 1), 100),
            alpha=alpha,
        )
        return {
            "query": q,
            "alpha": alpha,
            "results": [
                {
                    "kind": h.kind, "id": h.id, "title": h.title,
                    "snippet": h.snippet, "score": h.score,
                    "meeting_id": h.meeting_id,
                    "score_components": h.score_components,
                }
                for h in hits
            ],
        }

    # ------------------------------------------------------------ async extract
    @app.post("/api/meetings/{meeting_id}/extract:async", status_code=202)
    def reextract_async(
        meeting_id: int,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        meeting = _get_team_meeting(db, meeting_id, team)
        if meeting.finalized:
            raise ConflictError("meeting already finalized")
        job = jobs_mod.enqueue(
            db, team_id=team.id, kind="extract_meeting",
            payload={"meeting_id": meeting.id},
            idempotency_key=f"extract:{meeting.id}",
        )
        return {"job_id": job.id, "status": job.status}

    @app.get("/api/jobs/{job_id}")
    def get_job(
        job_id: int,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        job = db.get(models.Job, job_id)
        if job is None or job.team_id != team.id:
            raise NotFoundError("job not found")
        import json as _json
        return {
            "id": job.id,
            "kind": job.kind,
            "status": job.status,
            "attempts": job.attempts,
            "error": job.error,
            "result": _json.loads(job.result) if job.result else None,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        }

    # ------------------------------------------------------------ inbound webhook
    @app.post("/api/webhooks/github", include_in_schema=False)
    async def github_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
        from .errors import AuthError
        body = await request.body()
        sig = request.headers.get("x-hub-signature-256")
        if not webhooks_mod.verify_github_signature(body, sig):
            raise AuthError("invalid signature")
        event_type = request.headers.get("x-github-event", "unknown")
        try:
            data = await request.json()
        except Exception:
            data = {}

        # Best-effort: turn pushes/PRs into evidence on tasks tagged with the
        # repository's team. For MVP we just append an audit row so operators
        # can confirm wiring; richer matching happens in a follow-up.
        team_slug = (request.headers.get("x-labflow-team")
                     or get_settings().bootstrap_team)
        team = db.execute(
            select(models.Team).where(models.Team.slug == team_slug)
        ).scalar_one_or_none()
        if team is None:
            return {"ok": True, "note": "no matching team"}

        audit_mod.record(
            db, team_id=team.id, action="github.received",
            entity_type="webhook", entity_id=None,
            actor=f"github:{event_type}",
            metadata={"event": event_type, "ref": data.get("ref")},
        )
        metrics.inc("labflow_github_events_total", team=team.slug, event=event_type)

        # If this is a push or PR with a useful URL/summary, attach as
        # evidence to any open task whose summary mentions a referenced
        # commit message word. Keep it small — don't iterate the whole DB.
        if event_type in ("push", "pull_request"):
            commits = data.get("commits") or []
            pr = (data.get("pull_request") or {})
            uri = pr.get("html_url") or (commits[0].get("url") if commits else None)
            summary = pr.get("title") or (commits[0].get("message") if commits else None)
            if uri and summary:
                # Cap candidates to recent open tasks for the team.
                cands = list(
                    db.execute(
                        select(models.Task)
                        .where(models.Task.team_id == team.id,
                               models.Task.status != "done")
                        .order_by(models.Task.id.desc()).limit(50)
                    ).scalars()
                )
                attached = 0
                for t in cands:
                    ev = models.Evidence(
                        team_id=team.id, task=t, kind="commit",
                        uri=uri, summary=summary,
                    )
                    db.add(ev)
                    db.flush()
                    if verification.verify_evidence(db, ev):
                        attached += 1
                        audit_mod.record(
                            db, team_id=team.id, action="evidence.verified",
                            entity_type="evidence", entity_id=ev.id,
                            actor=f"github:{event_type}",
                            metadata={"task_id": t.id, "score": ev.score},
                        )
                    else:
                        # Rollback the failed match — we don't want to
                        # spam every open task with unrelated evidence.
                        db.delete(ev)
                        db.flush()
                metrics.inc("labflow_github_evidence_attached_total",
                            float(attached), team=team.slug)
                return {"ok": True, "attached": attached}

        return {"ok": True}

    # ------------------------------------------------------------ metrics
    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics(db: Session = Depends(get_db)) -> Response:
        body = metrics.render(db)
        return Response(content=body, media_type="text/plain; version=0.0.4")

    # ------------------------------------------------------------ graph (v0.4)
    @app.get("/api/graph/decisions", tags=["graph"])
    def decision_graph_json(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        """Return the team's decision graph as ``{"nodes": [...], "edges": [...]}``."""
        return graph_mod.build_graph(db, team_id=team.id).to_dict()

    @app.get("/api/graph/decisions.mermaid", response_class=PlainTextResponse, tags=["graph"])
    def decision_graph_mermaid(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> str:
        """Return the decision graph rendered as a Mermaid ``flowchart``."""
        return graph_mod.render_mermaid(graph_mod.build_graph(db, team_id=team.id))

    # ------------------------------------------------------------ live updates (v0.5)
    @app.get("/api/stream", include_in_schema=True, tags=["system"])
    async def stream(
        team: models.Team = Depends(require_team),
    ) -> Response:
        """Server-Sent Events stream of team activity (heartbeat every 15s).

        Connect with ``EventSource`` from a browser or any standards-
        compliant SSE client. Events include ``meeting.finalized``,
        ``task.closed``, ``evidence.verified``.
        """
        from starlette.responses import StreamingResponse
        return StreamingResponse(
            sse_mod.stream(team.id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # disable nginx buffering
            },
        )

    # ------------------------------------------------------------ identity (v0.5)
    @app.get("/api/me", tags=["system"])
    def whoami(
        request: Request,
        team: models.Team = Depends(require_team),
    ) -> dict:
        """Return the caller's identity, role, and rate-limit posture."""
        return {
            "team": {"id": team.id, "slug": team.slug, "name": team.name},
            "role": getattr(request.state, "role", "admin"),
            "auth_enabled": settings.auth_enabled,
        }

    # ------------------------------------------------------------ admin (v0.5)
    @app.get("/api/admin/export", tags=["admin"])
    def admin_export(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
        _: None = Depends(require_role("admin")),
    ) -> dict:
        """GDPR Article 15 — full export of every row scoped to this team."""
        return retention_mod.export_team(db, team_id=team.id)

    @app.delete("/api/admin/erase", tags=["admin"])
    def admin_erase(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
        _: None = Depends(require_role("admin")),
    ) -> dict:
        """GDPR Article 17 — hard-delete the team and every cascaded row."""
        # Audit *before* delete; the audit row itself goes away with the cascade.
        audit_mod.record(db, team_id=team.id, action="team.erased",
                         entity_type="team", entity_id=team.id,
                         actor="admin")
        retention_mod.erase_team(db, team_id=team.id)
        return {"erased": True}

    @app.post("/api/admin/retention/sweep", tags=["admin"])
    def admin_retention_sweep(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
        _: None = Depends(require_role("admin")),
    ) -> dict:
        """Run the retention sweep immediately. Useful for ops dry-runs."""
        return retention_mod.sweep(db)


    # ------------------------------------------------------------ digest
    @app.get("/api/digest/weekly", response_class=PlainTextResponse)
    def weekly_digest(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> str:
        return digest_mod.build_weekly_digest(db, team_id=team.id).to_markdown()

    # ------------------------------------------------------------ health
    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"ok": True, "version": __version__}

    @app.get("/readyz", include_in_schema=False)
    def readyz(db: Session = Depends(get_db)) -> dict:
        # Verify the DB is reachable.
        from sqlalchemy import text
        db.execute(text("SELECT 1"))
        return {"ready": True}

    # ====================================================================
    # v0.6 — Collaboration & Insights
    # ====================================================================
    def _actor_label(request: Request) -> str:
        key_id = getattr(request.state, "api_key_id", None)
        return f"key:{key_id}" if key_id is not None else "system"

    @app.get("/api/comments", tags=["collab"])
    def list_comments_route(
        entity_type: str, entity_id: int,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        rows = collab_mod.list_comments(
            db, team_id=team.id,
            entity_type=entity_type, entity_id=entity_id,
        )
        return {"comments": collab_mod.thread_comments(rows)}

    @app.post("/api/comments", status_code=201, tags=["collab"])
    def create_comment(
        payload: _CommentIn,
        request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        c = collab_mod.add_comment(
            db, team_id=team.id, entity_type=payload.entity_type,
            entity_id=payload.entity_id, body=payload.body,
            parent_id=payload.parent_id,
            actor=_actor_label(request),
            actor_key_id=getattr(request.state, "api_key_id", None),
        )
        return {"id": c.id, "actor": c.actor, "body": c.body,
                "parent_id": c.parent_id,
                "created_at": c.created_at.isoformat() if c.created_at else None}

    @app.delete("/api/comments/{comment_id}", tags=["collab"])
    def delete_comment(
        comment_id: int, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        collab_mod.soft_delete_comment(
            db, team_id=team.id, comment_id=comment_id,
            actor=_actor_label(request),
        )
        return {"deleted": True}

    @app.post("/api/reactions", tags=["collab"])
    def toggle_reaction(
        payload: _ReactionIn,
        request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        _, added = collab_mod.react(
            db, team_id=team.id, entity_type=payload.entity_type,
            entity_id=payload.entity_id, emoji=payload.emoji,
            actor=_actor_label(request),
            actor_key_id=getattr(request.state, "api_key_id", None),
        )
        counts = collab_mod.reaction_counts(
            db, team_id=team.id, entity_type=payload.entity_type,
            entity_id=payload.entity_id,
        )
        return {"added": added, "counts": counts}

    @app.get("/api/reactions", tags=["collab"])
    def get_reactions(
        entity_type: str, entity_id: int,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return {
            "counts": collab_mod.reaction_counts(
                db, team_id=team.id, entity_type=entity_type, entity_id=entity_id,
            ),
        }

    # ----------------------------------------------------- saved searches
    @app.get("/api/saved-searches", tags=["saved-searches"])
    def list_saved_searches(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return {"items": [saved_searches.to_dict(s)
                          for s in saved_searches.list_for(db, team_id=team.id)]}

    @app.post("/api/saved-searches", status_code=201, tags=["saved-searches"])
    def create_saved_search(
        payload: _SavedSearchIn,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        row = saved_searches.create(
            db, team_id=team.id, name=payload.name, query=payload.query,
            slug=payload.slug, alpha=payload.alpha, filters=payload.filters,
            pinned=payload.pinned,
        )
        return saved_searches.to_dict(row)

    @app.delete("/api/saved-searches/{slug}", tags=["saved-searches"])
    def delete_saved_search(
        slug: str,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        saved_searches.delete(db, team_id=team.id, slug=slug)
        return {"deleted": True}

    @app.get("/api/saved-searches/{slug}/run", tags=["saved-searches"])
    def run_saved_search(
        slug: str,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        row = saved_searches.get(db, team_id=team.id, slug=slug)
        hits = search_mod.search(
            db, team_id=team.id, query=row.query,
            limit=50, alpha=row.alpha,
        )
        return {
            "saved_search": saved_searches.to_dict(row),
            "results": [{"kind": h.kind, "id": h.id, "title": h.title,
                         "snippet": h.snippet, "score": h.score} for h in hits],
        }

    # ----------------------------------------------------- analytics
    @app.get("/api/analytics", tags=["analytics"])
    def analytics_endpoint(
        days: int = 30,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return analytics_mod.compute(db, team_id=team.id, days=days).to_dict()

    # ----------------------------------------------------- iCalendar feed
    @app.get("/api/calendar.ics", tags=["calendar"])
    def calendar_feed_route(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> Response:
        body = calendar_feed.render_team_calendar(db, team_id=team.id)
        return Response(content=body, media_type="text/calendar; charset=utf-8")

    # ----------------------------------------------------- AI summary
    @app.get("/api/meetings/{meeting_id}/summary", tags=["meetings"])
    def meeting_summary(
        meeting_id: int,
        max_sentences: int = 5,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        meeting = _get_team_meeting(db, meeting_id, team)
        text = (meeting.transcript or "") + "\n" + (meeting.notes or "")
        with otel.span("labflow.summarize", meeting_id=meeting.id):
            return summary_mod.summarize(text, max_sentences=max(1, min(max_sentences, 20)))

    # ----------------------------------------------------- HTML digest
    @app.get("/api/digest/weekly.html", tags=["system"])
    def weekly_digest_html(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> HTMLResponse:
        return HTMLResponse(
            digest_mod.build_weekly_digest(db, team_id=team.id).to_html()
        )

    # ----------------------------------------------------- notification prefs
    @app.get("/api/me/notifications", tags=["system"])
    def get_notif_prefs(
        request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        key_id = getattr(request.state, "api_key_id", None)
        if key_id is None:
            return {"digest_cadence": "weekly", "email": None,
                    "muted_events": [], "single_team_mode": True}
        return notif_prefs.to_dict(
            notif_prefs.get_or_create(db, team_id=team.id, api_key_id=key_id)
        )

    @app.put("/api/me/notifications", tags=["system"])
    def update_notif_prefs(
        payload: _NotifPrefIn,
        request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        key_id = getattr(request.state, "api_key_id", None)
        if key_id is None:
            raise LFValidationError(
                "notification preferences are per-key; enable LABFLOW_AUTH_ENABLED"
            )
        row = notif_prefs.update(
            db, team_id=team.id, api_key_id=key_id,
            digest_cadence=payload.digest_cadence,
            email=payload.email,
            muted_events=payload.muted_events,
        )
        return notif_prefs.to_dict(row)

    # ====================================================================
    # v0.7 — Realtime, GraphQL, Observability
    # ====================================================================
    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """Bidirectional realtime channel.

        In single-team mode connects without auth. When auth is enabled,
        clients pass the API key as the ``token`` query param (browsers
        can't easily set headers on WS handshakes).
        """
        await websocket.accept()
        team_id = None
        try:
            if not settings.auth_enabled:
                # bootstrap team
                SessionLocal = get_session_factory()
                with SessionLocal() as sess:
                    team = sess.execute(
                        select(models.Team).where(models.Team.slug == settings.bootstrap_team)
                    ).scalar_one_or_none()
                    team_id = team.id if team else None
            else:
                from .auth import hash_api_key
                token = websocket.query_params.get("token", "")
                if not token:
                    await websocket.close(code=4401)
                    return
                SessionLocal = get_session_factory()
                with SessionLocal() as sess:
                    key = sess.execute(
                        select(models.ApiKey).where(
                            models.ApiKey.key_hash == hash_api_key(token),
                            models.ApiKey.revoked_at.is_(None),
                        )
                    ).scalar_one_or_none()
                    team_id = key.team_id if key else None
            if team_id is None:
                await websocket.close(code=4401)
                return
            await ws_mod.serve(websocket, team_id=team_id)
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            try:
                await websocket.close(code=1011)
            except Exception:
                pass

    @app.post("/graphql", tags=["graphql"])
    def graphql_endpoint(
        payload: _GraphQLIn,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        """Read-only GraphQL endpoint. POST a ``{"query": "..."}`` body."""
        with otel.span("labflow.graphql"):
            return graphql_api.execute(db, team_id=team.id, query=payload.query)

    @app.get("/graphql/schema", response_class=JSONResponse, tags=["graphql"])
    def graphql_schema() -> dict:
        return graphql_api.SCHEMA

    # ====================================================================
    # v0.8 — Workflows / Sprints / DAG / ACL / Share links / CSV / Slack
    # ====================================================================

    # ------------------------------------------------------------ workflows
    @app.get("/api/workflows", tags=["workflows"])
    def list_workflows_route(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        wfs = workflows_mod.list_workflows(db, team_id=team.id)
        # Ensure default exists (even if the team has no tasks yet) so the UI
        # has something to render.
        if not wfs:
            workflows_mod.get_default_workflow(db, team_id=team.id)
            wfs = workflows_mod.list_workflows(db, team_id=team.id)
        import json as _json
        return {"workflows": [
            {"id": w.id, "name": w.name, "is_default": w.is_default,
             "definition": _json.loads(w.definition_json)}
            for w in wfs
        ]}

    @app.post("/api/workflows", status_code=201, tags=["workflows"],
              dependencies=[Depends(require_role("admin"))])
    def create_workflow_route(
        payload: _WorkflowIn,
        request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        wf = workflows_mod.create_workflow(
            db, team_id=team.id, name=payload.name,
            definition=payload.definition,
            make_default=payload.make_default,
            actor=_actor_label(request),
        )
        import json as _json
        return {
            "id": wf.id, "name": wf.name, "is_default": wf.is_default,
            "definition": _json.loads(wf.definition_json),
        }

    @app.post("/api/tasks/{task_id}/transition", tags=["workflows"])
    def transition_task_route(
        task_id: int,
        payload: _TaskTransitionIn,
        request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        task = db.get(models.Task, task_id)
        if task is None or task.team_id != team.id:
            raise NotFoundError("task not found")
        # ACL gate (only enforced when ACL rows exist for this task).
        acl_mod.assert_allowed(
            db, team_id=team.id, entity_type="task", entity_id=task.id,
            api_key_id=getattr(request.state, "api_key_id", None),
            permission="write",
        )
        result = workflows_mod.transition_task(
            db, task=task, to_state=payload.to_state,
            actor=_actor_label(request),
            actor_role=getattr(request.state, "role", "admin"),
        )
        return {
            "task_id": task.id,
            "from_state": result.from_state,
            "to_state": result.to_state,
            "is_terminal": result.is_terminal,
            "sla_breach_at": result.sla_breach_at.isoformat()
            if result.sla_breach_at else None,
        }

    @app.post("/api/admin/sla/sweep", tags=["admin"],
              dependencies=[Depends(require_role("admin"))])
    def sla_sweep_route(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        breached = workflows_mod.sweep_sla_breaches(db, team_id=team.id)
        return {"breached": breached, "count": len(breached)}

    # ------------------------------------------------------------ sprints
    @app.get("/api/sprints", tags=["sprints"])
    def list_sprints_route(
        active: bool | None = None,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        sprints = sprints_mod.list_sprints(db, team_id=team.id, active=active)
        return {"sprints": [
            {"id": s.id, "slug": s.slug, "name": s.name,
             "starts_at": s.starts_at.isoformat(),
             "ends_at": s.ends_at.isoformat(),
             "active": s.active, "goal": s.goal}
            for s in sprints
        ]}

    @app.post("/api/sprints", status_code=201, tags=["sprints"])
    def create_sprint_route(
        payload: _SprintIn,
        request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        sp = sprints_mod.create_sprint(
            db, team_id=team.id, name=payload.name,
            starts_at=payload.starts_at, ends_at=payload.ends_at,
            goal=payload.goal, slug=payload.slug,
            actor=_actor_label(request),
        )
        return {"id": sp.id, "slug": sp.slug, "name": sp.name,
                "starts_at": sp.starts_at.isoformat(),
                "ends_at": sp.ends_at.isoformat(),
                "active": sp.active, "goal": sp.goal}

    @app.post("/api/sprints/{slug}/close", tags=["sprints"],
              dependencies=[Depends(require_role("member"))])
    def close_sprint_route(
        slug: str, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        sp = sprints_mod.close_sprint(db, team_id=team.id, slug=slug,
                                      actor=_actor_label(request))
        return {"slug": sp.slug, "active": sp.active}

    @app.post("/api/sprints/{slug}/assign", tags=["sprints"])
    def assign_sprint_route(
        slug: str, payload: _SprintAssignIn, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        task = sprints_mod.assign_task(db, team_id=team.id,
                                       sprint_slug=slug, task_id=payload.task_id,
                                       actor=_actor_label(request))
        return {"task_id": task.id, "sprint_slug": slug}

    @app.get("/api/sprints/{slug}/burndown", tags=["sprints"])
    def burndown_route(
        slug: str,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return sprints_mod.burndown(db, team_id=team.id, slug=slug)

    # ------------------------------------------------------------ DAG / critical path
    @app.post("/api/tasks/{task_id}/depends_on", status_code=201, tags=["dag"])
    def add_dependency_route(
        task_id: int, payload: _DependencyIn,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        dag.add_dependency(db, team_id=team.id, task_id=task_id,
                           depends_on_id=payload.depends_on_id)
        return {"task_id": task_id, "depends_on_id": payload.depends_on_id}

    @app.get("/api/tasks/critical-path", tags=["dag"])
    def critical_path_route(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return dag.critical_path(db, team_id=team.id)

    # ------------------------------------------------------------ ACL
    @app.get("/api/acl", tags=["acl"])
    def list_acls_route(
        entity_type: str | None = None, entity_id: int | None = None,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        rows = acl_mod.list_acls(db, team_id=team.id,
                                 entity_type=entity_type, entity_id=entity_id)
        return {"acls": [
            {"id": r.id, "entity_type": r.entity_type, "entity_id": r.entity_id,
             "api_key_id": r.api_key_id, "permission": r.permission}
            for r in rows
        ]}

    @app.post("/api/acl", status_code=201, tags=["acl"],
              dependencies=[Depends(require_role("admin"))])
    def grant_acl_route(
        payload: _AclGrantIn, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        row = acl_mod.grant(db, team_id=team.id,
                            entity_type=payload.entity_type,
                            entity_id=payload.entity_id,
                            api_key_id=payload.api_key_id,
                            permission=payload.permission,
                            actor=_actor_label(request))
        return {"id": row.id, "permission": row.permission}

    @app.delete("/api/acl/{acl_id}", tags=["acl"],
                dependencies=[Depends(require_role("admin"))])
    def revoke_acl_route(
        acl_id: int, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        acl_mod.revoke(db, team_id=team.id, acl_id=acl_id,
                       actor=_actor_label(request))
        return {"ok": True}

    @app.post("/api/share-links", status_code=201, tags=["acl"],
              dependencies=[Depends(require_role("member"))])
    def create_share_link_route(
        payload: _ShareLinkIn, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        row, plaintext = acl_mod.create_share_link(
            db, team_id=team.id, entity_type=payload.entity_type,
            entity_id=payload.entity_id, ttl_hours=payload.ttl_hours,
            passcode=payload.passcode,
            created_by_key_id=getattr(request.state, "api_key_id", None),
            actor=_actor_label(request),
        )
        return {
            "id": row.id, "token": plaintext,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "entity_type": row.entity_type, "entity_id": row.entity_id,
        }

    @app.get("/api/share/{token}", tags=["acl"])
    def resolve_share_route(
        token: str, passcode: str | None = None,
        db: Session = Depends(get_db),
    ) -> dict:
        row = acl_mod.resolve(db, token=token, passcode=passcode)
        # Return a minimal read-only payload for the entity.
        if row.entity_type == "meeting":
            m = db.get(models.Meeting, row.entity_id)
            if m is None:
                raise NotFoundError("entity gone")
            return {
                "type": "meeting", "id": m.id, "title": m.title,
                "occurred_at": m.occurred_at.isoformat(),
                "decisions": [
                    {"id": d.id, "statement": d.statement} for d in m.decisions
                ],
                "tasks": [
                    {"id": t.id, "title": t.title, "status": t.status}
                    for t in m.tasks
                ],
            }
        if row.entity_type == "decision":
            d = db.get(models.Decision, row.entity_id)
            return {"type": "decision", "id": d.id, "statement": d.statement,
                    "rationale": d.rationale, "confidence": d.confidence}
        if row.entity_type == "task":
            t = db.get(models.Task, row.entity_id)
            return {"type": "task", "id": t.id, "title": t.title,
                    "status": t.status, "state": t.state}
        raise NotFoundError("unsupported entity")

    # ------------------------------------------------------------ CSV exports
    @app.get("/api/exports/tasks.csv", response_class=PlainTextResponse,
             tags=["exports"])
    def tasks_csv_route(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> PlainTextResponse:
        body = csv_export.tasks_csv(db, team_id=team.id)
        return PlainTextResponse(content=body, media_type="text/csv; charset=utf-8")

    @app.get("/api/exports/decisions.csv", response_class=PlainTextResponse,
             tags=["exports"])
    def decisions_csv_route(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> PlainTextResponse:
        body = csv_export.decisions_csv(db, team_id=team.id)
        return PlainTextResponse(content=body, media_type="text/csv; charset=utf-8")

    # ------------------------------------------------------------ Slack
    @app.post("/api/notify/slack/digest", tags=["system"],
              dependencies=[Depends(require_role("admin"))])
    def slack_digest_route(
        webhook_url: str,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        weekly = digest_mod.render_weekly(db, team_id=team.id)
        msg = slack_mod.render_digest(weekly)
        ok = slack_mod.post_message(webhook_url, msg,
                                    signing_secret=settings.webhook_signing_secret or None)
        return {"posted": ok}

    # ====================================================================
    # v0.9 — AI Copilot, Plugin Marketplace, Vector v2, Time-travel, i18n, PWA
    # ====================================================================

    # ------------------------------------------------------------ Copilot
    @app.post("/api/copilot/sessions", status_code=201, tags=["copilot"])
    def copilot_start_route(
        payload: _CopilotStartIn, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        s = copilot_mod.start_session(
            db, team_id=team.id, title=payload.title,
            actor_key_id=getattr(request.state, "api_key_id", None),
            actor=_actor_label(request),
        )
        return {"id": s.id, "title": s.title, "closed": s.closed,
                "created_at": s.created_at.isoformat()}

    @app.get("/api/copilot/sessions", tags=["copilot"])
    def copilot_list_route(
        limit: int = 20,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return {"sessions": copilot_mod.list_sessions(db, team_id=team.id, limit=limit)}

    @app.get("/api/copilot/sessions/{session_id}", tags=["copilot"])
    def copilot_get_route(
        session_id: int,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return copilot_mod.get_session(db, team_id=team.id, session_id=session_id)

    @app.post("/api/copilot/sessions/{session_id}/turns", tags=["copilot"])
    def copilot_turn_route(
        session_id: int, payload: _CopilotTurnIn, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return copilot_mod.take_turn(
            db, team_id=team.id, session_id=session_id,
            message=payload.message, actor=_actor_label(request),
        )

    @app.post("/api/copilot/sessions/{session_id}/close", tags=["copilot"])
    def copilot_close_route(
        session_id: int, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        copilot_mod.close_session(db, team_id=team.id, session_id=session_id,
                                  actor=_actor_label(request))
        return {"ok": True}

    @app.get("/api/copilot/tools", tags=["copilot"])
    def copilot_tools_route() -> dict:
        return {"tools": copilot_mod.TOOL_SCHEMA}

    # ------------------------------------------------------------ Plugins
    @app.get("/api/plugins", tags=["plugins"])
    def plugins_list_route(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        ps = plugin_marketplace.list_plugins(db, team_id=team.id)
        import json as _json
        return {"plugins": [
            {"id": p.id, "name": p.name, "version": p.version,
             "author": p.author, "description": p.description,
             "enabled": p.enabled, "manifest_sha256": p.manifest_sha256,
             "manifest": _json.loads(p.manifest_json),
             "installed_at": p.installed_at.isoformat()}
            for p in ps
        ]}

    @app.post("/api/plugins", status_code=201, tags=["plugins"],
              dependencies=[Depends(require_role("admin"))])
    def plugins_install_route(
        payload: _PluginInstallIn, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        p = plugin_marketplace.install(
            db, team_id=team.id, manifest=payload.manifest,
            expected_sha256=payload.expected_sha256,
            actor=_actor_label(request),
        )
        return {"id": p.id, "name": p.name, "version": p.version,
                "manifest_sha256": p.manifest_sha256, "enabled": p.enabled}

    @app.post("/api/plugins/{name}/enable", tags=["plugins"],
              dependencies=[Depends(require_role("admin"))])
    def plugins_enable_route(
        name: str, payload: _PluginEnableIn, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        p = plugin_marketplace.set_enabled(
            db, team_id=team.id, name=name, enabled=payload.enabled,
            actor=_actor_label(request),
        )
        return {"name": p.name, "enabled": p.enabled}

    @app.delete("/api/plugins/{name}", tags=["plugins"],
                dependencies=[Depends(require_role("admin"))])
    def plugins_uninstall_route(
        name: str, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        plugin_marketplace.uninstall(db, team_id=team.id, name=name,
                                     actor=_actor_label(request))
        return {"ok": True}

    # ------------------------------------------------------------ Vector index v2
    @app.post("/api/vector/build", tags=["vector"],
              dependencies=[Depends(require_role("admin"))])
    def vector_build_route(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        meta = vector_index_v2.build_team_index(db, team_id=team.id)
        if meta is None:
            return {"built": False, "reason":
                    "no vectors or LABFLOW_VECTOR_INDEX_DIR unset"}
        return {"built": True, "shard_id": meta.id, "vectors": meta.vectors,
                "model": meta.model, "path": meta.path,
                "sha256": meta.sha256}

    @app.get("/api/vector/query", tags=["vector"])
    def vector_query_route(
        q: str, k: int = 10, rerank: bool = False,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        results = vector_index_v2.query(
            db, team_id=team.id, text=q, k=max(1, min(k, 50)),
            rerank_query=q if rerank else None,
        )
        return {"results": [
            {"score": round(s, 4),
             "entity_type": e.entity_type, "entity_id": e.entity_id}
            for s, e in results
        ]}

    # ------------------------------------------------------------ Time-travel
    @app.get("/api/timetravel/tasks/{task_id}", tags=["timetravel"])
    def timetravel_task_route(
        task_id: int, as_of: datetime,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return timetravel.task_as_of(db, team_id=team.id, task_id=task_id,
                                     as_of=as_of)

    @app.get("/api/timetravel/meetings/{meeting_id}", tags=["timetravel"])
    def timetravel_meeting_route(
        meeting_id: int, as_of: datetime,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return timetravel.meeting_as_of(db, team_id=team.id,
                                        meeting_id=meeting_id, as_of=as_of)

    # ------------------------------------------------------------ i18n
    @app.get("/api/i18n/locales", tags=["i18n"])
    def i18n_locales_route() -> dict:
        return {"locales": i18n.supported_locales()}

    @app.get("/api/i18n/messages", tags=["i18n"])
    def i18n_messages_route(request: Request, locale: str | None = None) -> dict:
        loc = locale or i18n.negotiate(request.headers.get("accept-language"))
        return {"locale": loc, "messages": i18n.all_messages(loc)}

    # ------------------------------------------------------------ Replica health
    @app.get("/readyz/replicas", tags=["system"])
    def replica_health_route() -> dict:
        return replica_mod.health()

    # ====================================================================
    # v0.10 — Audit chain, backup/restore, automation, forecasting, dashboards
    # ====================================================================

    # ------------------------------------------------------------ Audit chain
    @app.get("/api/audit/verify", tags=["admin"],
             dependencies=[Depends(require_role("admin"))])
    def audit_verify_route(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return audit_mod.verify_chain(db, team_id=team.id)

    @app.get("/api/audit/events", tags=["admin"])
    def audit_events_route(
        limit: int = 50,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        rows = watchers_mod.feed(db, team_id=team.id, api_key_id=None,
                                 limit=limit)
        return {"events": rows}

    # ------------------------------------------------------------ Backup
    @app.post("/api/admin/backup", tags=["backup"],
              dependencies=[Depends(require_role("admin"))])
    def backup_route(
        secret: str,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return backup_mod.make_backup(db, team_id=team.id, secret=secret)

    @app.post("/api/admin/restore/preview", tags=["backup"],
              dependencies=[Depends(require_role("admin"))])
    def restore_preview_route(
        payload: _BackupRestoreIn,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return backup_mod.preview_restore(payload.envelope, secret=payload.secret)

    @app.post("/api/admin/restore/apply", status_code=201, tags=["backup"],
              dependencies=[Depends(require_role("admin"))])
    def restore_apply_route(
        payload: _BackupRestoreIn, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        if not payload.new_slug:
            raise LFValidationError("new_slug is required")
        result = backup_mod.restore_into_new_team(
            db, envelope=payload.envelope, secret=payload.secret,
            new_slug=payload.new_slug,
        )
        audit_mod.record(
            db, team_id=team.id, actor=_actor_label(request),
            action="admin.restore", entity_type="team",
            entity_id=result["team_id"],
            metadata={"new_slug": payload.new_slug,
                      "row_total": sum(result["inserted"].values())},
        )
        return result

    # ------------------------------------------------------------ Automation rules
    @app.get("/api/automation/rules", tags=["automation"])
    def rules_list_route(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        rules = automation_mod.list_rules(db, team_id=team.id)
        import json as _j
        return {"rules": [{
            "id": r.id, "name": r.name, "trigger_event": r.trigger_event,
            "condition": _j.loads(r.condition_json) if r.condition_json else None,
            "actions": _j.loads(r.actions_json),
            "enabled": r.enabled, "fires": r.fires,
            "last_fired_at": r.last_fired_at.isoformat() if r.last_fired_at else None,
        } for r in rules]}

    @app.post("/api/automation/rules", status_code=201, tags=["automation"],
              dependencies=[Depends(require_role("admin"))])
    def rules_create_route(
        payload: _RuleIn, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        rule = automation_mod.create_rule(
            db, team_id=team.id, name=payload.name,
            trigger_event=payload.trigger_event,
            actions=payload.actions, condition=payload.condition,
            enabled=payload.enabled,
        )
        audit_mod.record(
            db, team_id=team.id, actor=_actor_label(request),
            action="automation.rule.upsert", entity_type="rule",
            entity_id=rule.id, metadata={"name": rule.name},
        )
        return {"id": rule.id, "name": rule.name, "enabled": rule.enabled}

    @app.delete("/api/automation/rules/{name}", tags=["automation"],
                dependencies=[Depends(require_role("admin"))])
    def rules_delete_route(
        name: str, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        automation_mod.delete_rule(db, team_id=team.id, name=name)
        audit_mod.record(
            db, team_id=team.id, actor=_actor_label(request),
            action="automation.rule.deleted", entity_type="rule",
            entity_id=None, metadata={"name": name},
        )
        return {"ok": True}

    # ------------------------------------------------------------ Forecasting
    @app.get("/api/forecast/sprint/{slug}", tags=["forecast"])
    def forecast_sprint_route(
        slug: str,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return forecasting_mod.sprint_forecast(db, team_id=team.id, slug=slug)

    @app.get("/api/forecast/task/{task_id}", tags=["forecast"])
    def forecast_task_route(
        task_id: int,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return forecasting_mod.task_eta(db, team_id=team.id, task_id=task_id)

    # ------------------------------------------------------------ Dashboards
    @app.get("/api/dashboards/widgets", tags=["dashboards"])
    def dashboards_widgets_route() -> dict:
        return {"widgets": dashboards_mod.widget_catalogue()}

    @app.get("/api/dashboards", tags=["dashboards"])
    def dashboards_list_route(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        rows = dashboards_mod.list_dashboards(db, team_id=team.id)
        import json as _j
        return {"dashboards": [{
            "id": d.id, "slug": d.slug, "name": d.name,
            "is_default": d.is_default,
            "layout": _j.loads(d.layout_json),
            "owner_key_id": d.owner_key_id,
            "updated_at": d.updated_at.isoformat(),
        } for d in rows]}

    @app.post("/api/dashboards", status_code=201, tags=["dashboards"])
    def dashboards_upsert_route(
        payload: _DashboardIn, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        d = dashboards_mod.upsert_dashboard(
            db, team_id=team.id,
            owner_key_id=getattr(request.state, "api_key_id", None),
            slug=payload.slug, name=payload.name, layout=payload.layout,
            is_default=payload.is_default,
        )
        return {"id": d.id, "slug": d.slug, "name": d.name,
                "is_default": d.is_default}

    @app.get("/api/dashboards/{slug}/data", tags=["dashboards"])
    def dashboards_render_route(
        slug: str,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return dashboards_mod.render(db, team_id=team.id, slug=slug)

    # ====================================================================
    # v0.11 — Wiki, smart links, watchers/feed, GraphQL mutations
    # ====================================================================

    # ------------------------------------------------------------ Wiki
    @app.get("/api/wiki/pages", tags=["wiki"])
    def wiki_list_route(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        rows = wiki_mod.list_pages(db, team_id=team.id)
        return {"pages": [{
            "id": p.id, "slug": p.slug, "title": p.title,
            "summary": p.summary,
            "updated_at": p.updated_at.isoformat(),
        } for p in rows]}

    @app.post("/api/wiki/pages", status_code=201, tags=["wiki"])
    def wiki_upsert_route(
        payload: _WikiUpsertIn, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        p = wiki_mod.upsert_page(
            db, team_id=team.id, title=payload.title, body=payload.body,
            slug=payload.slug, summary=payload.summary,
            actor=_actor_label(request),
        )
        return {"id": p.id, "slug": p.slug, "title": p.title,
                "current_revision_id": p.current_revision_id}

    @app.get("/api/wiki/pages/{slug}", tags=["wiki"])
    def wiki_get_route(
        slug: str,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        p = wiki_mod.get_page(db, team_id=team.id, slug=slug)
        backlinks = links_mod.backlinks_for(
            db, team_id=team.id, target_type="wiki", target_id=p.id,
        )
        return {
            "id": p.id, "slug": p.slug, "title": p.title,
            "summary": p.summary, "body": p.body,
            "current_revision_id": p.current_revision_id,
            "updated_at": p.updated_at.isoformat(),
            "backlinks": backlinks,
        }

    @app.delete("/api/wiki/pages/{slug}", tags=["wiki"],
                dependencies=[Depends(require_role("member"))])
    def wiki_delete_route(
        slug: str, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        wiki_mod.soft_delete(db, team_id=team.id, slug=slug,
                             actor=_actor_label(request))
        return {"ok": True}

    @app.get("/api/wiki/pages/{slug}/revisions", tags=["wiki"])
    def wiki_revisions_route(
        slug: str,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        revs = wiki_mod.revisions_for(db, team_id=team.id, slug=slug)
        return {"revisions": [{
            "id": r.id, "title": r.title, "author": r.author,
            "created_at": r.created_at.isoformat(),
        } for r in revs]}

    @app.get("/api/wiki/search", tags=["wiki"])
    def wiki_search_route(
        q: str, limit: int = 20,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        hits = wiki_mod.search(db, team_id=team.id, q=q, limit=limit)
        return {"hits": [{
            "slug": p.slug, "title": p.title, "summary": p.summary,
        } for p in hits]}

    # ------------------------------------------------------------ Backlinks
    @app.get("/api/links/backlinks", tags=["links"])
    def backlinks_route(
        target_type: str, target_id: int,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return {"backlinks": links_mod.backlinks_for(
            db, team_id=team.id,
            target_type=target_type, target_id=target_id,
        )}

    # ------------------------------------------------------------ Watchers
    @app.get("/api/watchers", tags=["watchers"])
    def watchers_list_route(
        request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        kid = getattr(request.state, "api_key_id", None) or 0
        rows = watchers_mod.list_watches(db, team_id=team.id, api_key_id=kid)
        return {"watches": [{
            "id": w.id, "entity_type": w.entity_type,
            "entity_id": w.entity_id, "delivery": w.delivery,
        } for w in rows]}

    @app.post("/api/watchers", status_code=201, tags=["watchers"])
    def watchers_add_route(
        payload: _WatchIn, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        kid = getattr(request.state, "api_key_id", None) or 0
        w = watchers_mod.add_watch(
            db, team_id=team.id, api_key_id=kid,
            entity_type=payload.entity_type, entity_id=payload.entity_id,
            delivery=payload.delivery,
        )
        return {"id": w.id, "delivery": w.delivery}

    @app.delete("/api/watchers", tags=["watchers"])
    def watchers_remove_route(
        entity_type: str, entity_id: int, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        kid = getattr(request.state, "api_key_id", None) or 0
        ok = watchers_mod.remove_watch(
            db, team_id=team.id, api_key_id=kid,
            entity_type=entity_type, entity_id=entity_id,
        )
        return {"ok": ok}

    @app.get("/api/feed", tags=["watchers"])
    def feed_route(
        request: Request, limit: int = 50,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        kid = getattr(request.state, "api_key_id", None)
        events = watchers_mod.feed(
            db, team_id=team.id, api_key_id=kid, limit=limit,
        )
        return {"events": events}

    # ====================================================================
    # v0.12 — boards, recurring tasks, quotas, bulk ops, digest hour
    # ====================================================================
    from . import (  # noqa: PLC0415
        boards as boards_mod,
        bulk_tasks as bulk_mod,
        invites as invites_mod,
        notif_prefs as notif_prefs_mod,
        quotas as quotas_mod,
        recurring as recurring_mod,
        smart_lists as smart_lists_mod,
        bundle as bundle_mod,
    )
    from .auth import generate_api_key, hash_api_key  # noqa: PLC0415

    def _resolve_or_create_default_key(sess, *, team_id: int) -> int:
        """Return an API key id for ``team_id`` — used by routes that need
        a key context (notification prefs, watchers) when running in
        single-team / no-auth mode where no key was sent on the request.
        Picks the most recently created live key, or mints one named
        ``default-noauth`` if the team has none."""
        key = sess.execute(
            select(models.ApiKey)
            .where(models.ApiKey.team_id == team_id,
                   models.ApiKey.revoked_at.is_(None))
            .order_by(models.ApiKey.id.asc())
        ).scalars().first()
        if key is not None:
            return key.id
        plaintext = generate_api_key()
        key = models.ApiKey(
            team_id=team_id, name="default-noauth",
            key_hash=hash_api_key(plaintext),
        )
        sess.add(key)
        sess.flush()
        return key.id

    # ---- kanban board -------------------------------------------------
    @app.get("/api/board/{workflow_slug}", tags=["board"])
    def board_route(
        workflow_slug: str,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        slug = workflow_slug if workflow_slug != "_default" else None
        return boards_mod.board_for_workflow(db, team_id=team.id, workflow_slug=slug)

    @app.get("/app/board/{workflow_slug}", response_class=HTMLResponse,
             include_in_schema=False)
    def board_html(
        workflow_slug: str,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> HTMLResponse:
        slug = workflow_slug if workflow_slug != "_default" else None
        board = boards_mod.board_for_workflow(db, team_id=team.id, workflow_slug=slug)
        return HTMLResponse(boards_mod.render_html(board, team_slug=team.slug))

    # ---- recurring tasks ---------------------------------------------
    @app.get("/api/recurring-tasks", tags=["recurring"])
    def recurring_list(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        rows = recurring_mod.list_templates(db, team_id=team.id)
        return {"items": [{
            "id": r.id, "slug": r.slug, "cadence": r.cadence,
            "interval": r.interval, "day_of_week": r.day_of_week,
            "day_of_month": r.day_of_month,
            "template_title": r.template_title,
            "template_owner_id": r.template_owner_id,
            "template_priority": r.template_priority,
            "next_run_at": r.next_run_at.isoformat(),
            "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
            "active": r.active,
        } for r in rows]}

    @app.post("/api/recurring-tasks", status_code=201, tags=["recurring"],
              dependencies=[Depends(require_role("admin"))])
    def recurring_create(
        payload: dict, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        if not isinstance(payload, dict):
            raise LFValidationError("body must be an object")
        slug = payload.pop("slug", None) or ""
        actor = getattr(request.state, "actor", "system")
        rt = recurring_mod.create_template(
            db, team_id=team.id, slug=str(slug), payload=payload, actor=actor,
        )
        return {"id": rt.id, "slug": rt.slug,
                "next_run_at": rt.next_run_at.isoformat()}

    @app.delete("/api/recurring-tasks/{slug}", tags=["recurring"],
                dependencies=[Depends(require_role("admin"))])
    def recurring_delete(
        slug: str, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        actor = getattr(request.state, "actor", "system")
        recurring_mod.delete_template(db, team_id=team.id, slug=slug, actor=actor)
        return {"ok": True}

    @app.post("/api/recurring-tasks/run", tags=["recurring"],
              dependencies=[Depends(require_role("admin"))])
    def recurring_run(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        created = recurring_mod.materialize_due(db, team_id=team.id)
        return {"created": [{"id": t.id, "title": t.title} for t in created]}

    # ---- API key quotas ----------------------------------------------
    @app.get("/api/admin/quotas", tags=["quotas"],
             dependencies=[Depends(require_role("admin"))])
    def quotas_list(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return {"items": [{
            "api_key_id": q.api_key_id, "daily_limit": q.daily_limit,
            "updated_at": q.updated_at.isoformat() if q.updated_at else None,
        } for q in quotas_mod.list_quotas(db, team_id=team.id)]}

    @app.put("/api/admin/quotas/{api_key_id}", tags=["quotas"],
             dependencies=[Depends(require_role("admin"))])
    def quotas_set(
        api_key_id: int, payload: dict, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        if not isinstance(payload, dict) or "daily_limit" not in payload:
            raise LFValidationError("body must contain daily_limit")
        actor = getattr(request.state, "actor", "system")
        # Confirm the key belongs to this team.
        key = db.get(models.ApiKey, api_key_id)
        if key is None or key.team_id != team.id:
            raise NotFoundError("api key not found")
        q = quotas_mod.set_quota(
            db, team_id=team.id, api_key_id=api_key_id,
            daily_limit=int(payload["daily_limit"]), actor=actor,
        )
        return {"api_key_id": q.api_key_id, "daily_limit": q.daily_limit}

    @app.delete("/api/admin/quotas/{api_key_id}", tags=["quotas"],
                dependencies=[Depends(require_role("admin"))])
    def quotas_delete_route(
        api_key_id: int, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        actor = getattr(request.state, "actor", "system")
        quotas_mod.delete_quota(
            db, team_id=team.id, api_key_id=api_key_id, actor=actor,
        )
        return {"ok": True}

    @app.get("/api/admin/quotas/usage", tags=["quotas"],
             dependencies=[Depends(require_role("admin"))])
    def quotas_usage(
        day: str | None = None,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return {"day": day or quotas_mod._today(),
                "items": quotas_mod.usage_summary(db, team_id=team.id, day=day)}

    # ---- bulk task operations ----------------------------------------
    @app.post("/api/tasks/bulk", tags=["tasks"])
    def tasks_bulk(
        payload: dict, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        if not isinstance(payload, dict):
            raise LFValidationError("body must be an object")
        actor = getattr(request.state, "actor", "system")
        return bulk_mod.apply_bulk(
            db, team_id=team.id,
            op=str(payload.get("op", "")),
            task_ids=payload.get("task_ids") or [],
            args=payload.get("args"),
            actor=actor,
        )

    # ---- digest hour scheduler ---------------------------------------
    @app.put("/api/notifications/me", tags=["notifications"])
    def notifications_update_me(
        payload: dict, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        kid = getattr(request.state, "api_key_id", None)
        if kid is None:
            # Single-team / test mode: fall back to the bootstrap key,
            # creating a placeholder if needed so prefs always have a home.
            kid = _resolve_or_create_default_key(db, team_id=team.id)
        if not isinstance(payload, dict):
            raise LFValidationError("body must be an object")
        row = notif_prefs_mod.update(
            db, team_id=team.id, api_key_id=kid,
            digest_cadence=payload.get("digest_cadence"),
            email=payload.get("email"),
            muted_events=payload.get("muted_events"),
            digest_hour_utc=payload.get("digest_hour_utc"),
        )
        return notif_prefs_mod.to_dict(row)

    @app.get("/api/notifications/digest-due", tags=["notifications"],
             dependencies=[Depends(require_role("admin"))])
    def notifications_digest_due(
        hour: int,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        rows = notif_prefs_mod.keys_due_for_digest(
            db, team_id=team.id, hour_utc=hour,
        )
        return {"hour_utc": hour,
                "api_key_ids": [r.api_key_id for r in rows]}

    # ====================================================================
    # v0.13 — invites, smart lists, bundle export
    # ====================================================================

    def _mint_guest_key(sess, *, team_id: int, name: str, scopes: str | None):
        plaintext = generate_api_key()
        row = models.ApiKey(
            team_id=team_id, name=name,
            key_hash=hash_api_key(plaintext), scopes=scopes,
        )
        sess.add(row)
        sess.flush()
        return row, plaintext

    @app.post("/api/invites", status_code=201, tags=["invites"],
              dependencies=[Depends(require_role("admin"))])
    def invites_create(
        payload: dict, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        if not isinstance(payload, dict):
            raise LFValidationError("body must be an object")
        kid = getattr(request.state, "api_key_id", None)
        actor = getattr(request.state, "actor", "system")
        inv, token = invites_mod.create_invite(
            db, team_id=team.id,
            email=str(payload.get("email", "")),
            role=str(payload.get("role", "viewer")),
            scopes=payload.get("scopes"),
            acl_entries=payload.get("acl_entries"),
            ttl_hours=int(payload.get("ttl_hours", 168)),
            created_by_key_id=kid, actor=actor,
        )
        return {"id": inv.id, "email": inv.email, "role": inv.role,
                "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
                "token": token}  # only returned at creation time

    @app.get("/api/invites", tags=["invites"],
             dependencies=[Depends(require_role("admin"))])
    def invites_list(
        status: str | None = None,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return {"items": [{
            "id": i.id, "email": i.email, "status": i.status, "role": i.role,
            "created_at": i.created_at.isoformat() if i.created_at else None,
            "expires_at": i.expires_at.isoformat() if i.expires_at else None,
            "accepted_at": i.accepted_at.isoformat() if i.accepted_at else None,
        } for i in invites_mod.list_invites(db, team_id=team.id, status=status)]}

    @app.delete("/api/invites/{invite_id}", tags=["invites"],
                dependencies=[Depends(require_role("admin"))])
    def invites_revoke(
        invite_id: int, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        actor = getattr(request.state, "actor", "system")
        invites_mod.revoke_invite(
            db, team_id=team.id, invite_id=invite_id, actor=actor,
        )
        return {"ok": True}

    @app.post("/api/invites/accept", tags=["invites"], include_in_schema=True)
    def invites_accept(payload: dict, db: Session = Depends(get_db)) -> dict:
        # Public route — guest hasn't been minted yet.
        if not isinstance(payload, dict) or "token" not in payload:
            raise LFValidationError("body must contain token")
        inv, key, plaintext = invites_mod.accept_invite(
            db, token=str(payload["token"]), mint_key=_mint_guest_key,
        )
        return {"invite_id": inv.id, "api_key": plaintext,
                "api_key_id": key.id, "team_id": inv.team_id}

    # ---- smart lists -------------------------------------------------
    @app.get("/api/smart-lists", tags=["smart-lists"])
    def smart_lists_list(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        import json as _json
        return {"items": [{
            "slug": r.slug, "name": r.name,
            "filter": _json.loads(r.filter_json),
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        } for r in smart_lists_mod.list_all(db, team_id=team.id)]}

    @app.put("/api/smart-lists/{slug}", tags=["smart-lists"])
    def smart_lists_upsert(
        slug: str, payload: dict, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        if not isinstance(payload, dict):
            raise LFValidationError("body must be an object")
        actor = getattr(request.state, "actor", "system")
        row = smart_lists_mod.upsert(
            db, team_id=team.id, slug=slug,
            name=str(payload.get("name", slug)),
            filter=payload.get("filter") or {},
            actor=actor,
        )
        return {"slug": row.slug, "name": row.name}

    @app.delete("/api/smart-lists/{slug}", tags=["smart-lists"])
    def smart_lists_delete(
        slug: str, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        actor = getattr(request.state, "actor", "system")
        smart_lists_mod.delete(db, team_id=team.id, slug=slug, actor=actor)
        return {"ok": True}

    @app.get("/api/smart-lists/{slug}/run", tags=["smart-lists"])
    def smart_lists_run(
        slug: str, limit: int = 200,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        tasks = smart_lists_mod.run(db, team_id=team.id, slug=slug, limit=limit)
        return {"items": [{
            "id": t.id, "title": t.title, "status": t.status,
            "state": t.state, "priority": t.priority, "owner_id": t.owner_id,
            "due_date": t.due_date.isoformat() if t.due_date else None,
        } for t in tasks]}

    # ---- markdown bundle export --------------------------------------
    @app.get("/api/admin/export/bundle.zip", tags=["admin"],
             dependencies=[Depends(require_role("admin"))])
    def export_bundle(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> Response:
        data = bundle_mod.build_bundle(db, team_id=team.id)
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition":
                     f'attachment; filename="labflow-{team.slug}.zip"'},
        )

    # ====================================================================
    # v0.14 — time tracking, feature flags, smart-list subs, MCP tool API
    # ====================================================================
    from . import (  # noqa: PLC0415
        feature_flags as ff_mod,
        mcp as mcp_mod,
        smart_list_subscriptions as sls_mod,
        time_tracking as time_mod,
    )

    # ---- task effort estimate ----------------------------------------
    @app.put("/api/tasks/{task_id}/effort", tags=["tasks"])
    def task_set_effort(
        task_id: int, payload: dict,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        if not isinstance(payload, dict) or "effort_hours" not in payload:
            raise LFValidationError("body must contain effort_hours")
        v = payload["effort_hours"]
        if v is not None and (not isinstance(v, (int, float)) or v < 0
                              or v > 10000):
            raise LFValidationError("effort_hours must be in [0, 10000]")
        task = db.get(models.Task, task_id)
        if task is None or task.team_id != team.id:
            raise NotFoundError(f"task {task_id} not found")
        task.effort_hours = float(v) if v is not None else None
        db.flush()
        return {"id": task.id, "effort_hours": task.effort_hours}

    # ---- time tracking ----------------------------------------------
    @app.post("/api/tasks/{task_id}/time/start", tags=["time"])
    def time_start(
        task_id: int, payload: dict | None = None,
        request: Request = None,  # type: ignore[assignment]
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        body = payload or {}
        actor = getattr(request.state, "actor", "system") if request else "system"
        owner_id = body.get("owner_id")
        entry = time_mod.start_timer(
            db, team_id=team.id, task_id=task_id,
            owner_id=int(owner_id) if owner_id is not None else None,
            note=body.get("note"), actor=actor,
        )
        return {"id": entry.id, "started_at": entry.started_at.isoformat(),
                "task_id": entry.task_id, "owner_id": entry.owner_id}

    @app.post("/api/tasks/time/stop", tags=["time"])
    def time_stop(
        payload: dict | None = None, request: Request = None,  # type: ignore[assignment]
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        body = payload or {}
        actor = getattr(request.state, "actor", "system") if request else "system"
        owner_id = body.get("owner_id")
        entry = time_mod.stop_timer(
            db, team_id=team.id,
            owner_id=int(owner_id) if owner_id is not None else None,
            actor=actor,
        )
        return {
            "id": entry.id,
            "task_id": entry.task_id,
            "started_at": entry.started_at.isoformat(),
            "ended_at": entry.ended_at.isoformat() if entry.ended_at else None,
        }

    @app.post("/api/tasks/{task_id}/time", tags=["time"])
    def time_log_manual(
        task_id: int, payload: dict, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        if not isinstance(payload, dict):
            raise LFValidationError("body must be an object")
        try:
            started = datetime.fromisoformat(payload["started_at"])
            ended = datetime.fromisoformat(payload["ended_at"])
        except (KeyError, TypeError, ValueError) as e:
            raise LFValidationError(
                f"started_at and ended_at must be ISO datetimes: {e}"
            ) from None
        actor = getattr(request.state, "actor", "system")
        owner_id = payload.get("owner_id")
        entry = time_mod.log_manual(
            db, team_id=team.id, task_id=task_id,
            started_at=started, ended_at=ended,
            owner_id=int(owner_id) if owner_id is not None else None,
            note=payload.get("note"), actor=actor,
        )
        return {"id": entry.id,
                "started_at": entry.started_at.isoformat(),
                "ended_at": entry.ended_at.isoformat()}

    @app.get("/api/tasks/{task_id}/time", tags=["time"])
    def time_summary(
        task_id: int,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        # Confirm task ownership before exposing aggregates.
        task = db.get(models.Task, task_id)
        if task is None or task.team_id != team.id:
            raise NotFoundError(f"task {task_id} not found")
        return time_mod.task_summary(db, team_id=team.id, task_id=task_id)

    @app.get("/api/time/report", tags=["time"])
    def time_team_report(
        days: int = 14,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        from datetime import timedelta
        days = max(1, min(int(days), 365))
        since = (datetime.utcnow() - timedelta(days=days))
        return time_mod.team_report(db, team_id=team.id, since=since)

    # ---- feature flags ----------------------------------------------
    @app.get("/api/feature-flags", tags=["feature-flags"])
    def ff_list(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        rows = ff_mod.list_all(db, team_id=team.id)
        return {"items": [{"key": r.key, "enabled": r.enabled,
                            "payload_json": r.payload_json,
                            "updated_at": r.updated_at.isoformat()
                            if r.updated_at else None}
                           for r in rows]}

    @app.put("/api/feature-flags/{key}", tags=["feature-flags"],
             dependencies=[Depends(require_role("admin"))])
    def ff_upsert(
        key: str, payload: dict, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        if not isinstance(payload, dict):
            raise LFValidationError("body must be an object")
        actor = getattr(request.state, "actor", "system")
        row = ff_mod.upsert(
            db, team_id=team.id, key=key,
            enabled=bool(payload.get("enabled", False)),
            payload=payload.get("payload"),
            actor=actor,
        )
        return {"key": row.key, "enabled": row.enabled}

    @app.delete("/api/feature-flags/{key}", tags=["feature-flags"],
                dependencies=[Depends(require_role("admin"))])
    def ff_delete(
        key: str, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        actor = getattr(request.state, "actor", "system")
        ff_mod.delete(db, team_id=team.id, key=key, actor=actor)
        return {"ok": True}

    # ---- smart-list subscriptions -----------------------------------
    @app.post("/api/smart-lists/{slug}/subscriptions", tags=["smart-lists"],
              dependencies=[Depends(require_role("admin"))])
    def sls_subscribe(
        slug: str, payload: dict, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        if not isinstance(payload, dict):
            raise LFValidationError("body must be an object")
        actor = getattr(request.state, "actor", "system")
        sub = sls_mod.subscribe(
            db, team_id=team.id, smart_list_slug=slug,
            webhook_url=str(payload.get("webhook_url") or ""),
            secret=payload.get("secret"), actor=actor,
        )
        return {"id": sub.id, "webhook_url": sub.webhook_url}

    @app.get("/api/smart-lists/{slug}/subscriptions", tags=["smart-lists"])
    def sls_list(
        slug: str,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        rows = sls_mod.list_for(db, team_id=team.id, smart_list_slug=slug)
        return {"items": [{
            "id": r.id, "webhook_url": r.webhook_url,
            "last_digest": r.last_digest,
            "last_fired_at": r.last_fired_at.isoformat()
            if r.last_fired_at else None,
        } for r in rows]}

    @app.delete("/api/smart-lists/subscriptions/{sub_id}",
                tags=["smart-lists"],
                dependencies=[Depends(require_role("admin"))])
    def sls_unsub(
        sub_id: int, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        actor = getattr(request.state, "actor", "system")
        sls_mod.unsubscribe(db, team_id=team.id, sub_id=sub_id, actor=actor)
        return {"ok": True}

    @app.post("/api/admin/smart-lists/sweep", tags=["smart-lists"],
              dependencies=[Depends(require_role("admin"))])
    def sls_sweep(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return {"fired": sls_mod.sweep(db, team_id=team.id)}

    # ---- MCP-style JSON-RPC tool endpoint ---------------------------
    @app.get("/api/mcp/tools", tags=["mcp"])
    def mcp_tools_list(
        db: Session = Depends(get_db),  # noqa: ARG001 — kept for symmetry
        team: models.Team = Depends(require_team),  # noqa: ARG001
    ) -> dict:
        return mcp_mod.list_tools()

    @app.post("/api/mcp", tags=["mcp"])
    def mcp_jsonrpc(
        payload: dict,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return mcp_mod.jsonrpc(db, team_id=team.id, payload=payload)

    # ====================================================================
    # v0.15 — public shares, HTMX task list, SDK-gen advertisement
    # ====================================================================
    from . import (  # noqa: PLC0415
        htmx_tasks as htmx_mod,
        public_shares as shares_mod,
    )

    # ---- public share lifecycle (auth required) ---------------------
    @app.post("/api/shares", status_code=201, tags=["shares"],
              dependencies=[Depends(require_role("admin"))])
    def shares_create(
        payload: dict, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        if not isinstance(payload, dict):
            raise LFValidationError("body must be an object")
        try:
            entity_type = str(payload["entity_type"])
            entity_id = int(payload["entity_id"])
        except (KeyError, TypeError, ValueError) as e:
            raise LFValidationError(
                f"entity_type and entity_id are required: {e}"
            ) from None
        ttl = int(payload.get("ttl_hours", 168))
        actor = getattr(request.state, "actor", "system")
        cs = shares_mod.create(
            db, team_id=team.id, entity_type=entity_type,
            entity_id=entity_id, ttl_hours=ttl, actor=actor,
        )
        return {"id": cs.id, "token": cs.token,  # plaintext returned ONCE
                "entity_type": cs.entity_type, "entity_id": cs.entity_id,
                "expires_at": cs.expires_at.isoformat()}

    @app.get("/api/shares", tags=["shares"])
    def shares_list_route(
        entity_type: str | None = None, entity_id: int | None = None,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        rows = shares_mod.list_for(
            db, team_id=team.id, entity_type=entity_type, entity_id=entity_id,
        )
        return {"items": [{
            "id": r.id, "entity_type": r.entity_type,
            "entity_id": r.entity_id,
            "expires_at": r.expires_at.isoformat(),
            "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None,
            "view_count": r.view_count,
        } for r in rows]}

    @app.delete("/api/shares/{share_id}", tags=["shares"],
                dependencies=[Depends(require_role("admin"))])
    def shares_revoke(
        share_id: int, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        actor = getattr(request.state, "actor", "system")
        shares_mod.revoke(db, team_id=team.id, share_id=share_id, actor=actor)
        return {"ok": True}

    # ---- public read-only share view (NO auth) ----------------------
    @app.get("/share/{token}", include_in_schema=False)
    def shares_resolve_route(
        token: str, db: Session = Depends(get_db),
    ) -> JSONResponse:
        row, rendered = shares_mod.resolve(db, token=token)
        return JSONResponse({"share": {
            "entity_type": row.entity_type,
            "expires_at": row.expires_at.isoformat(),
        }, "data": rendered})

    # ---- HTMX task list page ----------------------------------------
    @app.get("/app/tasks", response_class=HTMLResponse,
             include_in_schema=False)
    def htmx_tasks_page(
        request: Request, q: str | None = None, status: str | None = None,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> HTMLResponse:
        return HTMLResponse(htmx_mod.render_page(
            db, team_id=team.id, team_slug=team.slug, q=q, status=status,
        ))

    @app.get("/api/tasks/_table", response_class=HTMLResponse,
             include_in_schema=False)
    def htmx_tasks_fragment(
        q: str | None = None, status: str | None = None,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> HTMLResponse:
        return HTMLResponse(htmx_mod.render_table_fragment(
            db, team_id=team.id, q=q, status=status,
        ))

    @app.post("/api/tasks/_status/{task_id}", response_class=HTMLResponse,
              include_in_schema=False)
    def htmx_tasks_transition(
        task_id: int, to: str,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> HTMLResponse:
        return HTMLResponse(htmx_mod.transition_status(
            db, team_id=team.id, task_id=task_id, to=to,
        ))

    # ====================================================================
    # v0.16 — LFQL, custom fields, scheduled reports
    # ====================================================================
    from . import (  # noqa: PLC0415
        custom_fields as cf_mod,
        lfql as lfql_mod,
        scheduled_reports as reports_mod,
    )

    # ---- LFQL --------------------------------------------------------
    @app.get("/api/lfql/explain", tags=["lfql"])
    def lfql_explain(
        q: str,
        team: models.Team = Depends(require_team),
    ) -> dict:
        return {"query": q, "ast": lfql_mod.explain(q)}

    @app.get("/api/lfql/run", tags=["lfql"])
    def lfql_run(
        q: str, limit: int = 100,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        if limit < 1 or limit > 500:
            raise LFValidationError("limit must be in [1, 500]")
        rows = lfql_mod.run(db, team_id=team.id, query=q, limit=limit)
        return {"matched": len(rows),
                "items": [{"id": t.id, "title": t.title, "status": t.status,
                           "priority": t.priority,
                           "owner_handle": t.owner.handle if t.owner else None}
                          for t in rows]}

    # ---- custom fields ----------------------------------------------
    @app.post("/api/custom-fields", tags=["custom-fields"], status_code=201)
    def cf_define(
        payload: dict, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        actor = getattr(request.state, "actor", "system")
        row = cf_mod.define_field(
            db, team_id=team.id, actor=actor,
            entity_type=payload.get("entity_type"),
            key=payload.get("key"), label=payload.get("label", ""),
            kind=payload.get("kind"),
            options=payload.get("options"),
            required=bool(payload.get("required", False)),
        )
        return {"id": row.id, "entity_type": row.entity_type,
                "key": row.key, "kind": row.kind}

    @app.get("/api/custom-fields", tags=["custom-fields"])
    def cf_list(
        entity_type: str | None = None,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        rows = cf_mod.list_fields(db, team_id=team.id, entity_type=entity_type)
        return {"items": [{
            "id": r.id, "entity_type": r.entity_type, "key": r.key,
            "label": r.label, "kind": r.kind, "required": r.required,
            "options": (None if r.options_json is None
                        else __import__("json").loads(r.options_json)),
        } for r in rows]}

    @app.delete("/api/custom-fields/{def_id}", tags=["custom-fields"],
                dependencies=[Depends(require_role("admin"))])
    def cf_delete(
        def_id: int, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        actor = getattr(request.state, "actor", "system")
        cf_mod.delete_field(db, team_id=team.id, def_id=def_id, actor=actor)
        return {"ok": True}

    @app.put("/api/{entity_type}/{entity_id}/fields/{key}",
             tags=["custom-fields"])
    def cf_set(
        entity_type: str, entity_id: int, key: str, payload: dict,
        request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        actor = getattr(request.state, "actor", "system")
        if "value" not in payload:
            raise LFValidationError("body must contain value")
        cf_mod.set_value(
            db, team_id=team.id, entity_type=entity_type,
            entity_id=entity_id, key=key, value=payload["value"], actor=actor,
        )
        return {"ok": True, "values": cf_mod.list_values(
            db, team_id=team.id, entity_type=entity_type,
            entity_id=entity_id,
        )}

    @app.get("/api/{entity_type}/{entity_id}/fields", tags=["custom-fields"])
    def cf_get(
        entity_type: str, entity_id: int,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        if entity_type not in cf_mod.VALID_ENTITIES:
            raise LFValidationError(
                f"entity_type must be one of {sorted(cf_mod.VALID_ENTITIES)}"
            )
        return {"values": cf_mod.list_values(
            db, team_id=team.id, entity_type=entity_type,
            entity_id=entity_id,
        )}

    @app.delete("/api/{entity_type}/{entity_id}/fields/{key}",
                tags=["custom-fields"])
    def cf_unset(
        entity_type: str, entity_id: int, key: str, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        actor = getattr(request.state, "actor", "system")
        cf_mod.delete_value(
            db, team_id=team.id, entity_type=entity_type,
            entity_id=entity_id, key=key, actor=actor,
        )
        return {"ok": True}

    # ---- scheduled reports -------------------------------------------
    @app.post("/api/reports", tags=["reports"], status_code=201)
    def reports_create(
        payload: dict, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        actor = getattr(request.state, "actor", "system")
        row = reports_mod.create(
            db, team_id=team.id, actor=actor,
            name=payload.get("name", ""),
            query=payload.get("query", ""),
            cadence=payload.get("cadence", ""),
            webhook_url=payload.get("webhook_url", ""),
            secret=payload.get("secret"),
        )
        return {"id": row.id, "name": row.name, "cadence": row.cadence,
                "next_run_at": row.next_run_at.isoformat()
                if row.next_run_at else None}

    @app.get("/api/reports", tags=["reports"])
    def reports_list(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        rows = reports_mod.list_reports(db, team_id=team.id)
        return {"items": [{
            "id": r.id, "name": r.name, "query": r.query,
            "cadence": r.cadence, "enabled": r.enabled,
            "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
            "next_run_at": r.next_run_at.isoformat() if r.next_run_at else None,
        } for r in rows]}

    @app.delete("/api/reports/{report_id}", tags=["reports"],
                dependencies=[Depends(require_role("admin"))])
    def reports_delete(
        report_id: int, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        actor = getattr(request.state, "actor", "system")
        reports_mod.delete(db, team_id=team.id, report_id=report_id,
                           actor=actor)
        return {"ok": True}

    @app.get("/api/reports/{report_id}/runs", tags=["reports"])
    def reports_runs(
        report_id: int, limit: int = 50,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        rows = reports_mod.runs_for(
            db, team_id=team.id, report_id=report_id, limit=limit,
        )
        return {"items": [{
            "id": r.id, "matched": r.matched, "delivered": r.delivered,
            "status_code": r.status_code, "error": r.error,
            "created_at": r.created_at.isoformat(),
        } for r in rows]}

    @app.post("/api/reports/_run-due", tags=["reports"],
              dependencies=[Depends(require_role("admin"))])
    def reports_run_due(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        n = reports_mod.run_due(db)
        return {"executed": n}

    # ====================================================================
    # v0.17 — webhook DLQ, activity heatmap, API key rotation
    # ====================================================================
    from . import (  # noqa: PLC0415
        heatmap as heatmap_mod,
        key_rotation as rotation_mod,
        webhook_dlq as dlq_mod,
    )

    # ---- webhook DLQ -------------------------------------------------
    @app.get("/api/webhook-dlq", tags=["webhook-dlq"])
    def dlq_list(
        limit: int = 100, offset: int = 0,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        rows = dlq_mod.list_dead(
            db, team_id=team.id, limit=limit, offset=offset,
        )
        return {"items": [{
            "id": r.id, "event": r.event, "attempts": r.attempts,
            "status_code": r.status_code,
            "dead_lettered_at": r.dead_lettered_at.isoformat()
            if r.dead_lettered_at else None,
            "created_at": r.created_at.isoformat(),
        } for r in rows]}

    @app.get("/api/webhook-dlq/stats", tags=["webhook-dlq"])
    def dlq_stats(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return dlq_mod.stats(db, team_id=team.id)

    @app.post("/api/webhook-dlq/{delivery_id}/replay", tags=["webhook-dlq"],
              dependencies=[Depends(require_role("admin"))])
    def dlq_replay(
        delivery_id: int, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        actor = getattr(request.state, "actor", "system")
        d = dlq_mod.replay(
            db, team_id=team.id, delivery_id=delivery_id, actor=actor,
        )
        return {"id": d.id, "attempts": d.attempts}

    @app.post("/api/webhook-dlq/{delivery_id}/discard", tags=["webhook-dlq"],
              dependencies=[Depends(require_role("admin"))])
    def dlq_discard(
        delivery_id: int, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        actor = getattr(request.state, "actor", "system")
        dlq_mod.discard(
            db, team_id=team.id, delivery_id=delivery_id, actor=actor,
        )
        return {"ok": True}

    # ---- activity heatmap --------------------------------------------
    @app.get("/api/heatmap", tags=["heatmap"])
    def heatmap_json(
        days: int = heatmap_mod.DAYS_DEFAULT,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        return {"days": days, "counts": heatmap_mod.daily_counts(
            db, team_id=team.id, days=days,
        )}

    @app.get("/api/heatmap.svg", tags=["heatmap"],
             response_class=Response)
    def heatmap_svg(
        days: int = heatmap_mod.DAYS_DEFAULT,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> Response:
        counts = heatmap_mod.daily_counts(db, team_id=team.id, days=days)
        svg = heatmap_mod.render_svg(counts)
        return Response(content=svg, media_type="image/svg+xml")

    # ---- API key rotation --------------------------------------------
    @app.post("/api/keys/{key_id}/rotate", tags=["key-rotation"],
              status_code=201,
              dependencies=[Depends(require_role("admin"))])
    def keys_rotate(
        key_id: int, payload: dict | None = None, request: Request = None,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        body = payload or {}
        actor = getattr(request.state, "actor", "system") if request else "system"
        result = rotation_mod.rotate(
            db, team_id=team.id, old_key_id=key_id,
            grace_hours=int(body.get("grace_hours",
                                     rotation_mod.GRACE_DEFAULT_HOURS)),
            actor=actor,
        )
        # Plaintext is shown ONCE here; never persisted, never returned again.
        return {
            "new_key_id": result.new_id, "old_key_id": result.old_id,
            "token": result.new_token,
            "grace_until": result.grace_until.isoformat(),
        }

    @app.post("/api/keys/{key_id}/rotate/cancel", tags=["key-rotation"],
              dependencies=[Depends(require_role("admin"))])
    def keys_rotate_cancel(
        key_id: int, request: Request,
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        actor = getattr(request.state, "actor", "system")
        rotation_mod.cancel_rotation(
            db, team_id=team.id, old_key_id=key_id, actor=actor,
        )
        return {"ok": True}

    @app.post("/api/keys/_sweep-expired", tags=["key-rotation"],
              dependencies=[Depends(require_role("admin"))])
    def keys_sweep(
        db: Session = Depends(get_db),
        team: models.Team = Depends(require_team),
    ) -> dict:
        n = rotation_mod.sweep_expired(db, team_id=team.id)
        return {"revoked": n}

    return app



app = create_app()
