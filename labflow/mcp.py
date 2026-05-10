"""MCP-style JSON-RPC 2.0 tool endpoint (v0.14).

LabFlow exposes a *minimal* subset of the
[Model Context Protocol](https://modelcontextprotocol.io/) so external
LLM agents can list and call read-only tools over plain HTTP without
needing the whole copilot session machinery.

Wire format is JSON-RPC 2.0 — one `POST /api/mcp` returns one
response. The two methods we implement are:

* ``tools/list``  → ``{"tools": [{"name": ..., "description": ...,
                                  "input_schema": {...}}]}``
* ``tools/call``  → ``{"content": [{"type": "text|json", ...}],
                       "isError": false}``

All tools are **read-only** by design. Mutations stay behind the typed
REST API where every change goes through audit + ACL.
"""
from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analytics as analytics_mod, models, search as search_mod
from .errors import ValidationError


def _tool_search(sess: Session, *, team_id: int, args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValidationError("query is required")
    limit = max(1, min(int(args.get("limit", 10)), 50))
    hits = search_mod.search(sess, team_id=team_id, query=query, limit=limit)
    return {"results": [
        {"kind": h.kind, "id": h.id, "title": h.title,
         "snippet": h.snippet, "score": h.score,
         "meeting_id": h.meeting_id}
        for h in hits
    ]}


def _tool_list_open_tasks(sess: Session, *, team_id: int, args: dict) -> dict:
    limit = max(1, min(int(args.get("limit", 25)), 100))
    rows = list(sess.execute(
        select(models.Task)
        .where(models.Task.team_id == team_id, models.Task.status == "open")
        .order_by(models.Task.due_date.is_(None), models.Task.due_date.asc(),
                  models.Task.id.asc())
        .limit(limit)
    ).scalars().all())
    return {"tasks": [{
        "id": t.id, "title": t.title, "owner_id": t.owner_id,
        "due_date": t.due_date.isoformat() if t.due_date else None,
        "priority": t.priority, "state": t.state,
    } for t in rows]}


def _tool_get_task(sess: Session, *, team_id: int, args: dict) -> dict:
    tid = int(args.get("task_id") or 0)
    t = sess.get(models.Task, tid)
    if t is None or t.team_id != team_id:
        return {"error": "not found"}
    return {
        "id": t.id, "title": t.title, "description": t.description,
        "status": t.status, "state": t.state, "priority": t.priority,
        "owner_id": t.owner_id, "due_date": t.due_date.isoformat() if t.due_date else None,
    }


def _tool_list_decisions(sess: Session, *, team_id: int, args: dict) -> dict:
    limit = max(1, min(int(args.get("limit", 25)), 100))
    rows = list(sess.execute(
        select(models.Decision)
        .where(models.Decision.team_id == team_id)
        .order_by(models.Decision.id.desc())
        .limit(limit)
    ).scalars().all())
    return {"decisions": [{
        "id": d.id, "statement": d.statement,
        "rationale": d.rationale,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    } for d in rows]}


def _tool_analytics(sess: Session, *, team_id: int, args: dict) -> dict:
    days = max(1, min(int(args.get("days", 14)), 90))
    a = analytics_mod.compute(sess, team_id=team_id, days=days)
    # Analytics is a dataclass; render as a plain dict for transport.
    from dataclasses import asdict
    return asdict(a)


# Registry: name -> (handler, description, input_schema)
_TOOLS: dict[str, tuple[Callable[..., dict], str, dict]] = {
    "search": (
        _tool_search, "Hybrid keyword/semantic search across the team's data.",
        {"type": "object", "required": ["query"],
         "properties": {"query": {"type": "string"},
                        "limit": {"type": "integer", "default": 10}}},
    ),
    "list_open_tasks": (
        _tool_list_open_tasks, "List open tasks ordered by due date.",
        {"type": "object", "properties": {
            "limit": {"type": "integer", "default": 25}}},
    ),
    "get_task": (
        _tool_get_task, "Fetch a single task by id.",
        {"type": "object", "required": ["task_id"],
         "properties": {"task_id": {"type": "integer"}}},
    ),
    "list_decisions": (
        _tool_list_decisions, "List recent decisions.",
        {"type": "object", "properties": {
            "limit": {"type": "integer", "default": 25}}},
    ),
    "analytics": (
        _tool_analytics, "Team analytics roll-up over the last N days.",
        {"type": "object", "properties": {
            "days": {"type": "integer", "default": 14}}},
    ),
}


def list_tools() -> dict:
    return {"tools": [
        {"name": name, "description": desc, "input_schema": schema}
        for name, (_h, desc, schema) in sorted(_TOOLS.items())
    ]}


def call_tool(
    sess: Session, *, team_id: int, name: str,
    arguments: dict | None = None,
) -> dict:
    arguments = arguments or {}
    if not isinstance(arguments, dict):
        raise ValidationError("arguments must be an object")
    if name not in _TOOLS:
        return {"isError": True,
                "content": [{"type": "text", "text": f"unknown tool: {name}"}]}
    handler, _desc, _schema = _TOOLS[name]
    try:
        result = handler(sess, team_id=team_id, args=arguments)
    except ValidationError as e:
        return {"isError": True,
                "content": [{"type": "text", "text": str(e)}]}
    return {"isError": False,
            "content": [{"type": "json", "json": result}]}


def jsonrpc(
    sess: Session, *, team_id: int, payload: dict,
) -> dict:
    """Dispatch one JSON-RPC 2.0 request and return the response object."""
    if not isinstance(payload, dict):
        raise ValidationError("payload must be an object")
    rpc_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise ValidationError("params must be an object")

    def _ok(result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}

    def _err(code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": code, "message": message}}

    if method == "tools/list":
        return _ok(list_tools())
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return _err(-32602, "params.name (string) is required")
        return _ok(call_tool(sess, team_id=team_id, name=name,
                             arguments=params.get("arguments") or {}))
    return _err(-32601, f"method not found: {method!r}")
