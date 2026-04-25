"""FastAPI application factory and routes."""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit as audit_mod
from . import digest as digest_mod
from . import (
    exports,
    graph as graph_mod,
    jobs as jobs_mod,
    metrics,
    models,
    retention as retention_mod,
    search as search_mod,
    services,
    sse as sse_mod,
    verification,
    webhooks as webhooks_mod,
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

WEB_DIR = Path(__file__).parent / "web"
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))

log = logging.getLogger("labflow.api")


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
        version="0.5.0",
        description=(
            "Meeting-to-execution OS for research and technical teams. "
            "Turns transcripts into a queryable graph of decisions, tasks, "
            "experiments, and evidence."
        ),
        contact={"name": "LabFlow", "url": "https://github.com/mariam-oss-eng/labflow"},
        license_info={"name": "MIT"},
        openapi_tags=[
            {"name": "meetings", "description": "Upload, extract, finalize, export."},
            {"name": "tasks", "description": "Action items with owners, deadlines, evidence."},
            {"name": "evidence", "description": "Artifacts that verify task completion."},
            {"name": "search", "description": "Hybrid keyword + semantic search."},
            {"name": "graph", "description": "Decision graph with supersession edges."},
            {"name": "jobs", "description": "Background job introspection."},
            {"name": "admin", "description": "Team-scoped administration: export, erase, retention."},
            {"name": "system", "description": "Health, readiness, metrics, live updates."},
        ],
    )

    # In test/dev we still call create_all so the app boots without a
    # separate `alembic upgrade`. In production, run `alembic upgrade head`
    # before starting and disable this by setting LABFLOW_AUTO_CREATE=false.
    init_db()
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
            },
        )

    @app.get("/meetings/{meeting_id}/review", response_class=HTMLResponse,
             include_in_schema=False)
    def review(meeting_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
        meeting = db.get(models.Meeting, meeting_id)
        if meeting is None:
            raise NotFoundError("meeting not found")
        return TEMPLATES.TemplateResponse(
            request,
            "review.html",
            {"title": meeting.title, "meeting": meeting},
        )

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
        return {"ok": True, "version": "0.5.0"}

    @app.get("/readyz", include_in_schema=False)
    def readyz(db: Session = Depends(get_db)) -> dict:
        # Verify the DB is reachable.
        from sqlalchemy import text
        db.execute(text("SELECT 1"))
        return {"ready": True}

    return app


app = create_app()
