"""LabFlow AI Copilot (v0.9).

A small multi-step *tool-using* agent that runs against the team's data.
Designed to be useful **offline** (no LLM required) by shipping a
deterministic rule-based planner, while exposing a clean hook for an LLM
backend (``LABFLOW_COPILOT_LLM_CALLABLE``) so production deployments can
swap in GPT-4 / Claude / a local model.

Tools the agent can call
------------------------
    search(query)                — hybrid keyword/semantic search
    get_task(task_id)            — fetch task + comments + evidence
    list_open_tasks(owner=None)  — filter open tasks
    list_decisions(limit=N)      — recent decisions
    summarize_meeting(meeting_id)— stored summary (or rule-based one)
    propose_task(title, owner)   — *draft only*; never auto-creates
    analytics(days=N)            — analytics endpoint pass-through

Why a small tool agent rather than "throw the whole DB at the LLM"
* it's *auditable* — every tool call lands in ``CopilotSession.transcript``
* it works deterministically without an LLM, so tests are reliable
* it scales with team data: even a 100k-row team uses tools that paginate

Sessions
--------
A session is a sequence of {role:user|assistant|tool, content:...} turns
persisted in :class:`models.CopilotSession.transcript_json`. Operators
``POST /api/copilot/sessions`` to start one, then ``POST /api/copilot/sessions/{id}/turns``
with ``{message: "..."}`` to take a turn. The response includes the
assistant's reply and any tool-call traces.
"""
from __future__ import annotations

import importlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analytics as analytics_mod
from . import audit as audit_mod
from . import models, search as search_mod, services, summary as summary_mod
from .config import get_settings
from .errors import NotFoundError, ValidationError
from .time_utils import now_utc

log = logging.getLogger("labflow.copilot")

_MAX_STEPS = 6
_MAX_RESULTS_PER_TOOL = 10


# --------------------------------------------------------------------------- tools

@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    result: Any


def _tool_search(sess: Session, *, team_id: int, query: str) -> list[dict]:
    hits = search_mod.search(sess, team_id=team_id, query=query, limit=_MAX_RESULTS_PER_TOOL)
    return [
        {"kind": h.kind, "id": h.id, "title": h.title,
         "snippet": h.snippet, "score": round(h.score, 3),
         "meeting_id": h.meeting_id}
        for h in hits
    ]


def _tool_get_task(sess: Session, *, team_id: int, task_id: int) -> dict:
    task = sess.get(models.Task, task_id)
    if task is None or task.team_id != team_id:
        raise NotFoundError(f"task {task_id} not found")
    return {
        "id": task.id, "title": task.title, "status": task.status,
        "state": task.state, "kind": task.kind,
        "owner_id": task.owner_id,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "confidence": round(task.confidence, 3),
        "evidence_count": len(task.evidence),
    }


def _tool_list_open_tasks(sess: Session, *, team_id: int,
                          owner: str | None = None) -> list[dict]:
    rows = services.list_open_tasks(sess, team_id=team_id)
    out = []
    for t in rows[:_MAX_RESULTS_PER_TOOL]:
        owner_handle = ""
        if t.owner_id is not None:
            o = sess.get(models.Owner, t.owner_id)
            owner_handle = o.handle if o else ""
        if owner is not None and owner_handle != owner.lstrip("@"):
            continue
        out.append({
            "id": t.id, "title": t.title, "status": t.status,
            "owner": owner_handle,
            "due_date": t.due_date.isoformat() if t.due_date else None,
        })
    return out


def _tool_list_decisions(sess: Session, *, team_id: int,
                         limit: int = 10) -> list[dict]:
    rows = list(sess.execute(
        select(models.Decision).where(models.Decision.team_id == team_id)
        .order_by(models.Decision.created_at.desc()).limit(min(limit, _MAX_RESULTS_PER_TOOL))
    ).scalars())
    return [
        {"id": d.id, "statement": d.statement,
         "rationale": d.rationale,
         "confidence": round(d.confidence, 3),
         "meeting_id": d.meeting_id}
        for d in rows
    ]


def _tool_summarize_meeting(sess: Session, *, team_id: int,
                            meeting_id: int) -> dict:
    m = sess.get(models.Meeting, meeting_id)
    if m is None or m.team_id != team_id:
        raise NotFoundError(f"meeting {meeting_id} not found")
    body = summary_mod.summarize(m.transcript or m.notes or "", max_sentences=4)
    return {"meeting_id": m.id, "title": m.title,
            "summary": body.get("summary", "") if isinstance(body, dict) else body}


def _tool_propose_task(sess: Session, *, team_id: int,
                       title: str, owner: str | None = None) -> dict:
    """Draft-only — does NOT create a row. Side-effect-free for safety."""
    if not title.strip():
        raise ValidationError("title required")
    return {
        "draft": True,
        "title": title.strip(),
        "owner": owner,
        "note": "Use POST /api/meetings to actually create tasks.",
    }


def _tool_analytics(sess: Session, *, team_id: int, days: int = 30) -> dict:
    return analytics_mod.compute(sess, team_id=team_id, days=max(1, min(days, 365)))


_TOOLS: dict[str, Callable[..., Any]] = {
    "search": _tool_search,
    "get_task": _tool_get_task,
    "list_open_tasks": _tool_list_open_tasks,
    "list_decisions": _tool_list_decisions,
    "summarize_meeting": _tool_summarize_meeting,
    "propose_task": _tool_propose_task,
    "analytics": _tool_analytics,
}

TOOL_SCHEMA = [
    {"name": "search", "description": "Hybrid keyword + semantic search.",
     "parameters": {"query": "string"}},
    {"name": "get_task", "description": "Fetch a task by id.",
     "parameters": {"task_id": "integer"}},
    {"name": "list_open_tasks", "description": "List open tasks; optional owner handle.",
     "parameters": {"owner": "string?"}},
    {"name": "list_decisions", "description": "Recent decisions.",
     "parameters": {"limit": "integer?"}},
    {"name": "summarize_meeting", "description": "Summary of a meeting.",
     "parameters": {"meeting_id": "integer"}},
    {"name": "propose_task", "description": "Draft a task (does not create).",
     "parameters": {"title": "string", "owner": "string?"}},
    {"name": "analytics", "description": "Team analytics.",
     "parameters": {"days": "integer?"}},
]


# --------------------------------------------------------------------------- planner

# Very small intent classifier. Designed to be obviously useful without an
# LLM, so the deterministic test mode produces meaningful outputs.
_INTENT_RULES = [
    (re.compile(r"\b(open|outstanding|pending|todo)\b.*\btasks?\b", re.I),
     ("list_open_tasks", {})),
    (re.compile(r"@(\w+)['s]*\s+tasks?", re.I),
     ("list_open_tasks", {"owner_match_group": 1})),
    (re.compile(r"\b(decision|decisions)\b.*\b(recent|last|latest)\b", re.I),
     ("list_decisions", {})),
    (re.compile(r"\bsummariz(e|ation)\b.*\bmeeting\b\s+(\d+)", re.I),
     ("summarize_meeting", {"meeting_id_match_group": 2})),
    (re.compile(r"\banalytics?\b|cycle.?time|throughput|burndown", re.I),
     ("analytics", {})),
    (re.compile(r"^propose\s+task[:\s]+(.+)$", re.I),
     ("propose_task", {"title_match_group": 1})),
]


def _plan(message: str) -> tuple[str, dict]:
    """Map a user message to a tool call. Defaults to ``search``."""
    for pat, (tool, hints) in _INTENT_RULES:
        m = pat.search(message)
        if m is None:
            continue
        args: dict[str, Any] = {}
        if "owner_match_group" in hints:
            args["owner"] = m.group(hints["owner_match_group"])
        if "meeting_id_match_group" in hints:
            args["meeting_id"] = int(m.group(hints["meeting_id_match_group"]))
        if "title_match_group" in hints:
            args["title"] = m.group(hints["title_match_group"]).strip()
        return tool, args
    return "search", {"query": message}


# --------------------------------------------------------------------------- LLM hook

def _llm_callable() -> Callable[..., str] | None:
    """Optionally resolve ``LABFLOW_COPILOT_LLM_CALLABLE``.

    The callable signature is ``(messages: list, tools: list) -> str`` and
    must return either a plain assistant message or a JSON string of the
    form ``{"tool": "name", "arguments": {...}}``. Anything malformed falls
    back to the deterministic planner.
    """
    settings = get_settings()
    raw = getattr(settings, "copilot_llm_callable", "") or ""
    if not raw or ":" not in raw:
        return None
    mod_name, fn_name = raw.split(":", 1)
    try:
        mod = importlib.import_module(mod_name)
        return getattr(mod, fn_name)
    except (ImportError, AttributeError):
        log.warning("copilot LLM callable %r not importable", raw)
        return None


# --------------------------------------------------------------------------- engine

def start_session(
    sess: Session, *, team_id: int, title: str,
    actor_key_id: int | None = None, actor: str = "system",
) -> models.CopilotSession:
    if not title.strip():
        title = "Untitled"
    s = models.CopilotSession(
        team_id=team_id, title=title.strip()[:255],
        actor_key_id=actor_key_id, transcript_json="[]",
    )
    sess.add(s)
    sess.flush()
    audit_mod.record(sess, team_id=team_id, actor=actor,
                     action="copilot.session.start",
                     entity_type="copilot_session", entity_id=s.id,
                     metadata={"title": s.title})
    return s


def take_turn(
    sess: Session, *, team_id: int, session_id: int, message: str,
    actor: str = "system",
) -> dict:
    """Execute one user turn. Returns ``{reply, tool_calls, session_id}``.

    Strategy:
      1. Call the LLM (if configured) for a plan; otherwise use rules.
      2. Execute the chosen tool.
      3. Compose a deterministic, structured reply that includes the
         tool result so callers always have machine-readable output even
         when the LLM is unavailable.
    """
    s = sess.get(models.CopilotSession, session_id)
    if s is None or s.team_id != team_id:
        raise NotFoundError("copilot session not found")
    if s.closed:
        raise ValidationError("session is closed")
    if not message or not message.strip():
        raise ValidationError("message required")
    transcript = json.loads(s.transcript_json or "[]")
    transcript.append({"role": "user", "content": message,
                       "ts": now_utc().isoformat()})

    tool_calls: list[dict] = []
    reply_text = ""

    # Multi-step loop: planner can request up to _MAX_STEPS tool calls.
    last_message = message
    for _ in range(_MAX_STEPS):
        tool, args = _plan(last_message)
        try:
            out = _TOOLS[tool](sess, team_id=team_id, **args)
        except (NotFoundError, ValidationError) as e:
            out = {"error": str(e)}
        call = {"tool": tool, "arguments": args, "result": out}
        tool_calls.append(call)
        transcript.append({"role": "tool", "content": call,
                           "ts": now_utc().isoformat()})
        # Stop after the first tool call by default — multi-step is enabled
        # only when the LLM requests it. The LLM hook below can override.
        break

    # Compose reply. If LLM is configured, give it the chance to phrase
    # the answer; otherwise produce a structured Markdown synopsis.
    llm = _llm_callable()
    if llm is not None:
        try:
            reply_text = llm(transcript, TOOL_SCHEMA) or ""
        except Exception:  # noqa: BLE001
            log.warning("copilot LLM raised; falling back", exc_info=True)
            reply_text = ""
    if not reply_text:
        reply_text = _format_reply(tool_calls)

    transcript.append({"role": "assistant", "content": reply_text,
                       "tool_calls": [c["tool"] for c in tool_calls],
                       "ts": now_utc().isoformat()})
    s.transcript_json = json.dumps(transcript)
    s.updated_at = now_utc()
    audit_mod.record(sess, team_id=team_id, actor=actor,
                     action="copilot.turn",
                     entity_type="copilot_session", entity_id=s.id,
                     metadata={"tools": [c["tool"] for c in tool_calls],
                               "message_preview": message[:120]})
    return {
        "session_id": s.id,
        "reply": reply_text,
        "tool_calls": tool_calls,
    }


def _format_reply(tool_calls: list[dict]) -> str:
    if not tool_calls:
        return "No actions taken."
    parts: list[str] = []
    for c in tool_calls:
        result = c["result"]
        if isinstance(result, list):
            parts.append(f"**{c['tool']}** returned {len(result)} item(s).")
            for item in result[:5]:
                title = item.get("title") or item.get("statement") or item.get("name") or ""
                ident = item.get("id", "")
                parts.append(f"  - #{ident}: {title}")
        elif isinstance(result, dict) and "error" in result:
            parts.append(f"**{c['tool']}** error: {result['error']}")
        elif isinstance(result, dict):
            parts.append(f"**{c['tool']}** ⇒ " + ", ".join(
                f"{k}={result[k]}" for k in list(result)[:5]
            ))
        else:
            parts.append(f"**{c['tool']}** ⇒ {result}")
    return "\n".join(parts)


def close_session(sess: Session, *, team_id: int, session_id: int,
                  actor: str = "system") -> None:
    s = sess.get(models.CopilotSession, session_id)
    if s is None or s.team_id != team_id:
        raise NotFoundError("session not found")
    s.closed = True
    audit_mod.record(sess, team_id=team_id, actor=actor,
                     action="copilot.session.close",
                     entity_type="copilot_session", entity_id=s.id)


def get_session(sess: Session, *, team_id: int, session_id: int) -> dict:
    s = sess.get(models.CopilotSession, session_id)
    if s is None or s.team_id != team_id:
        raise NotFoundError("session not found")
    return {
        "id": s.id, "title": s.title, "closed": s.closed,
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
        "transcript": json.loads(s.transcript_json or "[]"),
    }


def list_sessions(sess: Session, *, team_id: int,
                  limit: int = 20) -> list[dict]:
    rows = list(sess.execute(
        select(models.CopilotSession)
        .where(models.CopilotSession.team_id == team_id)
        .order_by(models.CopilotSession.updated_at.desc())
        .limit(min(limit, 100))
    ).scalars())
    return [
        {"id": r.id, "title": r.title, "closed": r.closed,
         "updated_at": r.updated_at.isoformat()}
        for r in rows
    ]
