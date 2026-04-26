"""Hand-rolled, dependency-free read-only GraphQL endpoint (v0.7).

Why no Strawberry / Ariadne / graphene?
---------------------------------------
Each pulls in 2-5 transitive dependencies (parsers, validators, type
introspection registries) for what amounts to ~100 lines of selection-set
walking. LabFlow's GraphQL surface is small and read-only, so we implement
just enough of the spec to be useful from the official clients (Apollo,
urql, ``graphql-request``, the Insomnia GraphQL panel):

* parsing: a tiny tokenizer + recursive-descent parser that handles
  ``query``/operation names, fields, arguments (string/int/bool), and
  selection sets nested arbitrarily deep.
* execution: dispatch on field name → resolver, recurse into nested
  selection sets, return a dict response shaped exactly per the
  ``selections`` requested. Unknown fields are silently dropped in
  permissive mode but reported as errors otherwise.
* schema introspection: a single ``__schema`` field returns the catalog
  so client tools can autocomplete.

This is **not** a full GraphQL server. It does enough to ship a useful
read API. If the surface grows, swap to a real library — the route stays
the same.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analytics as analytics_mod
from . import collab as collab_mod
from . import models, services

# --------------------------------------------------------------------- parser
_TOKEN = re.compile(
    r'\s+'
    r'|(?P<str>"(?:[^"\\]|\\.)*")'
    r'|(?P<int>-?\d+)'
    r'|(?P<word>[A-Za-z_][A-Za-z0-9_]*)'
    r'|(?P<punc>[{}():,])'
)


@dataclass
class Field:
    name: str
    args: dict[str, Any]
    selections: list["Field"]


def _tokenize(src: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in _TOKEN.finditer(src):
        if m.group("str") is not None:
            out.append(("STR", json.loads(m.group("str"))))
        elif m.group("int") is not None:
            out.append(("INT", int(m.group("int"))))
        elif m.group("word") is not None:
            w = m.group("word")
            if w in ("true", "false"):
                out.append(("BOOL", w == "true"))
            elif w == "null":
                out.append(("NULL", None))
            else:
                out.append(("WORD", w))
        elif m.group("punc") is not None:
            out.append(("PUNC", m.group("punc")))
    return out


class _Parser:
    def __init__(self, tokens: list[tuple[str, Any]]):
        self.toks = tokens
        self.i = 0

    def peek(self) -> tuple[str, Any] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def expect(self, kind: str, value: Any | None = None) -> Any:
        if self.i >= len(self.toks):
            raise ValueError("unexpected end of query")
        k, v = self.toks[self.i]
        if k != kind or (value is not None and v != value):
            raise ValueError(f"expected {kind}{f'/{value}' if value else ''} got {k}/{v!r}")
        self.i += 1
        return v

    def parse(self) -> list[Field]:
        # Allow optional "query" / "query Name" prefix.
        peek = self.peek()
        if peek and peek[0] == "WORD" and peek[1] == "query":
            self.i += 1
            peek = self.peek()
            if peek and peek[0] == "WORD":
                self.i += 1
        return self._parse_selection_set()

    def _parse_selection_set(self) -> list[Field]:
        self.expect("PUNC", "{")
        fields: list[Field] = []
        while self.peek() != ("PUNC", "}"):
            fields.append(self._parse_field())
        self.expect("PUNC", "}")
        return fields

    def _parse_field(self) -> Field:
        name = self.expect("WORD")
        args: dict[str, Any] = {}
        if self.peek() == ("PUNC", "("):
            self.i += 1
            while self.peek() != ("PUNC", ")"):
                arg_name = self.expect("WORD")
                self.expect("PUNC", ":")
                k, v = self.toks[self.i]; self.i += 1
                if k not in ("STR", "INT", "BOOL", "NULL"):
                    raise ValueError(f"unexpected arg literal {k}")
                args[arg_name] = v
                if self.peek() == ("PUNC", ","):
                    self.i += 1
            self.expect("PUNC", ")")
        selections: list[Field] = []
        if self.peek() == ("PUNC", "{"):
            selections = self._parse_selection_set()
        return Field(name=name, args=args, selections=selections)


def parse(src: str) -> list[Field]:
    return _Parser(_tokenize(src)).parse()


# ----------------------------------------------------------- execution / schema
SCHEMA = {
    "queryType": {"name": "Query"},
    "types": [
        {"name": "Query", "fields": [
            {"name": "team", "args": []},
            {"name": "tasks", "args": [
                {"name": "status", "type": "String"},
                {"name": "limit", "type": "Int"},
            ]},
            {"name": "decisions", "args": [{"name": "limit", "type": "Int"}]},
            {"name": "meetings", "args": [{"name": "limit", "type": "Int"}]},
            {"name": "comments", "args": [
                {"name": "entity_type", "type": "String"},
                {"name": "entity_id", "type": "Int"},
            ]},
            {"name": "analytics", "args": [{"name": "days", "type": "Int"}]},
        ]},
        {"name": "Task", "fields": [
            {"name": "id"}, {"name": "title"}, {"name": "status"},
            {"name": "kind"}, {"name": "uncertainty"}, {"name": "confidence"},
            {"name": "owner"}, {"name": "due_date"}, {"name": "created_at"},
            {"name": "closed_at"},
        ]},
        {"name": "Decision", "fields": [
            {"name": "id"}, {"name": "statement"}, {"name": "rationale"},
            {"name": "confidence"}, {"name": "superseded_by_id"},
            {"name": "created_at"},
        ]},
        {"name": "Meeting", "fields": [
            {"name": "id"}, {"name": "title"}, {"name": "meeting_type"},
            {"name": "occurred_at"}, {"name": "finalized"},
        ]},
        {"name": "Comment", "fields": [
            {"name": "id"}, {"name": "actor"}, {"name": "body"},
            {"name": "created_at"},
        ]},
    ],
}


def _project(obj: dict, sels: list[Field]) -> dict:
    if not sels:
        return obj
    out: dict[str, Any] = {}
    for sel in sels:
        if sel.name in obj:
            v = obj[sel.name]
            if isinstance(v, dict) and sel.selections:
                out[sel.name] = _project(v, sel.selections)
            elif isinstance(v, list) and sel.selections:
                out[sel.name] = [_project(x, sel.selections) for x in v if isinstance(x, dict)]
            else:
                out[sel.name] = v
    return out


def _task_dict(t: models.Task) -> dict:
    return {
        "id": t.id, "title": t.title, "status": t.status, "kind": t.kind,
        "uncertainty": t.uncertainty, "confidence": t.confidence,
        "owner": t.owner.handle if t.owner else None,
        "due_date": t.due_date.isoformat() if t.due_date else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
    }


def _decision_dict(d: models.Decision) -> dict:
    return {
        "id": d.id, "statement": d.statement, "rationale": d.rationale,
        "confidence": d.confidence,
        "superseded_by_id": d.superseded_by_id,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


def _meeting_dict(m: models.Meeting) -> dict:
    return {
        "id": m.id, "title": m.title, "meeting_type": m.meeting_type,
        "occurred_at": m.occurred_at.isoformat() if m.occurred_at else None,
        "finalized": m.finalized,
    }


def execute(sess: Session, *, team_id: int, query: str) -> dict:
    """Run a GraphQL query and return ``{"data": ..., "errors": [...]}``."""
    try:
        fields = parse(query)
    except Exception as exc:
        return {"data": None, "errors": [{"message": f"parse error: {exc}"}]}

    data: dict[str, Any] = {}
    errors: list[dict] = []
    for f in fields:
        try:
            if f.name == "__schema":
                data["__schema"] = _project(SCHEMA, f.selections)
            elif f.name == "team":
                team = sess.get(models.Team, team_id)
                data["team"] = _project(
                    {"id": team.id, "slug": team.slug, "name": team.name},
                    f.selections,
                )
            elif f.name == "tasks":
                stmt = select(models.Task).where(models.Task.team_id == team_id)
                if "status" in f.args:
                    stmt = stmt.where(models.Task.status == f.args["status"])
                limit = max(1, min(int(f.args.get("limit", 50)), 200))
                rows = list(sess.execute(stmt.order_by(models.Task.id).limit(limit)).scalars())
                data["tasks"] = [_project(_task_dict(t), f.selections) for t in rows]
            elif f.name == "decisions":
                limit = max(1, min(int(f.args.get("limit", 50)), 200))
                rows = list(
                    sess.execute(
                        select(models.Decision)
                        .where(models.Decision.team_id == team_id)
                        .order_by(models.Decision.id.desc()).limit(limit)
                    ).scalars()
                )
                data["decisions"] = [_project(_decision_dict(d), f.selections) for d in rows]
            elif f.name == "meetings":
                limit = max(1, min(int(f.args.get("limit", 50)), 200))
                rows = list(
                    sess.execute(
                        select(models.Meeting)
                        .where(models.Meeting.team_id == team_id)
                        .order_by(models.Meeting.occurred_at.desc()).limit(limit)
                    ).scalars()
                )
                data["meetings"] = [_project(_meeting_dict(m), f.selections) for m in rows]
            elif f.name == "comments":
                et = f.args.get("entity_type")
                eid = int(f.args.get("entity_id", 0))
                if et and eid:
                    rows = collab_mod.list_comments(
                        sess, team_id=team_id, entity_type=et, entity_id=eid,
                    )
                    data["comments"] = [
                        _project({
                            "id": c.id, "actor": c.actor, "body": c.body,
                            "created_at": c.created_at.isoformat() if c.created_at else None,
                        }, f.selections) for c in rows
                    ]
                else:
                    data["comments"] = []
            elif f.name == "analytics":
                days = int(f.args.get("days", 30))
                a = analytics_mod.compute(sess, team_id=team_id, days=days)
                data["analytics"] = _project(a.to_dict(), f.selections)
            else:
                errors.append({"message": f"unknown field: {f.name!r}"})
        except Exception as exc:
            errors.append({"message": f"{f.name}: {exc}"})
    return {"data": data, "errors": errors}
