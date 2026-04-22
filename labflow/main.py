"""FastAPI application factory and routes."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import List

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import digest as digest_mod
from . import exports, models, services, verification
from .db import get_session_factory, init_db
from .extraction import extract
from .schemas import EvidenceCreate, EvidenceOut, MeetingCreate, MeetingOut, TaskOut

WEB_DIR = Path(__file__).parent / "web"
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))

# Hard cap on user-supplied transcript size to bound regex work and memory.
# Real meeting transcripts are well under this; uploads exceeding the cap
# are rejected rather than silently truncated so users notice.
MAX_TRANSCRIPT_BYTES = 1_000_000  # 1 MB


def get_db() -> Session:
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


def create_app() -> FastAPI:
    app = FastAPI(
        title="LabFlow",
        version="0.1.0",
        description="Meeting-to-execution OS for research and technical teams.",
    )

    init_db()

    static_dir = WEB_DIR / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request, "index.html", {"title": "LabFlow"}
        )

    @app.get("/app", response_class=HTMLResponse)
    def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
        meetings = list(
            db.execute(
                select(models.Meeting).order_by(models.Meeting.occurred_at.desc())
            ).scalars()
        )
        open_tasks = services.list_open_tasks(db)
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "title": "LabFlow — Dashboard",
                "meetings": meetings,
                "open_tasks": open_tasks,
            },
        )

    @app.get("/meetings/{meeting_id}/review", response_class=HTMLResponse)
    def review(
        meeting_id: int, request: Request, db: Session = Depends(get_db)
    ) -> HTMLResponse:
        meeting = db.get(models.Meeting, meeting_id)
        if meeting is None:
            raise HTTPException(404, "meeting not found")
        return TEMPLATES.TemplateResponse(
            request,
            "review.html",
            {"title": meeting.title, "meeting": meeting},
        )

    # ----------------------------------------------------------- meeting API
    @app.post("/api/meetings", response_model=MeetingOut)
    def create_meeting(payload: MeetingCreate, db: Session = Depends(get_db)) -> MeetingOut:
        if len(payload.transcript) + len(payload.notes) > MAX_TRANSCRIPT_BYTES:
            raise HTTPException(413, "transcript + notes exceed size limit")
        meeting = models.Meeting(
            title=payload.title,
            meeting_type=payload.meeting_type,
            transcript=payload.transcript,
            notes=payload.notes,
            occurred_at=payload.occurred_at or datetime.utcnow(),
        )
        db.add(meeting)
        db.flush()
        if payload.transcript or payload.notes:
            result = extract(payload.transcript + "\n" + payload.notes,
                             reference=meeting.occurred_at)
            services.persist_extraction(db, meeting, result)
        return MeetingOut.model_validate(meeting)

    @app.post("/api/meetings/upload", response_model=MeetingOut)
    async def upload_meeting(
        title: str = Form(...),
        meeting_type: str = Form("standup"),
        transcript_file: UploadFile | None = None,
        notes: str = Form(""),
        db: Session = Depends(get_db),
    ) -> MeetingOut:
        transcript_bytes = await transcript_file.read() if transcript_file else b""
        if len(transcript_bytes) > MAX_TRANSCRIPT_BYTES:
            raise HTTPException(413, "transcript exceeds size limit")
        transcript = transcript_bytes.decode("utf-8", errors="replace")
        meeting = models.Meeting(
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

    @app.post("/api/meetings/{meeting_id}/extract", response_model=MeetingOut)
    def reextract(meeting_id: int, db: Session = Depends(get_db)) -> MeetingOut:
        meeting = db.get(models.Meeting, meeting_id)
        if meeting is None:
            raise HTTPException(404, "meeting not found")
        if meeting.finalized:
            raise HTTPException(400, "meeting already finalized")
        result = extract(meeting.transcript + "\n" + meeting.notes,
                         reference=meeting.occurred_at)
        services.persist_extraction(db, meeting, result)
        return MeetingOut.model_validate(meeting)

    @app.post("/api/meetings/{meeting_id}/finalize", response_model=MeetingOut)
    def finalize(meeting_id: int, db: Session = Depends(get_db)) -> MeetingOut:
        meeting = db.get(models.Meeting, meeting_id)
        if meeting is None:
            raise HTTPException(404, "meeting not found")
        meeting.finalized = True
        db.flush()
        return MeetingOut.model_validate(meeting)

    @app.get("/api/meetings/{meeting_id}/export.json")
    def export_json(meeting_id: int, db: Session = Depends(get_db)) -> JSONResponse:
        meeting = db.get(models.Meeting, meeting_id)
        if meeting is None:
            raise HTTPException(404, "meeting not found")
        return JSONResponse(exports.meeting_to_dict(meeting))

    @app.get("/api/meetings/{meeting_id}/export.md", response_class=PlainTextResponse)
    def export_md(meeting_id: int, db: Session = Depends(get_db)) -> str:
        meeting = db.get(models.Meeting, meeting_id)
        if meeting is None:
            raise HTTPException(404, "meeting not found")
        return exports.meeting_to_markdown(meeting)

    # ----------------------------------------------------------- task API
    @app.get("/api/tasks", response_model=List[TaskOut])
    def list_tasks(db: Session = Depends(get_db)) -> List[TaskOut]:
        tasks = list(db.execute(select(models.Task)).scalars())
        return [TaskOut.model_validate(t) for t in tasks]

    @app.patch("/api/tasks/{task_id}", response_model=TaskOut)
    def update_task(
        task_id: int,
        payload: dict,
        db: Session = Depends(get_db),
    ) -> TaskOut:
        task = db.get(models.Task, task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        allowed = {"title", "description", "status", "due_date", "uncertainty"}
        for k, v in payload.items():
            if k not in allowed:
                raise HTTPException(400, f"field {k!r} is not editable")
            if k == "due_date" and isinstance(v, str):
                v = datetime.fromisoformat(v) if v else None
            if k == "status" and v == "done" and task.status != "done":
                task.closed_at = datetime.utcnow()
            setattr(task, k, v)
        db.flush()
        return TaskOut.model_validate(task)

    # ------------------------------------------------------------ evidence
    @app.post("/api/evidence", response_model=EvidenceOut)
    def add_evidence(payload: EvidenceCreate, db: Session = Depends(get_db)) -> EvidenceOut:
        task = db.get(models.Task, payload.task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        ev = models.Evidence(
            task=task,
            kind=payload.kind,
            uri=payload.uri,
            summary=payload.summary,
        )
        db.add(ev)
        db.flush()
        verification.verify_evidence(db, ev)
        return EvidenceOut.model_validate(ev)

    # ------------------------------------------------------------ digest
    @app.get("/api/digest/weekly", response_class=PlainTextResponse)
    def weekly_digest(db: Session = Depends(get_db)) -> str:
        return digest_mod.build_weekly_digest(db).to_markdown()

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    return app


app = create_app()
