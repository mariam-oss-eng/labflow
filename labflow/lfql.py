"""LabFlow Query Language (v0.16).

A tiny boolean filter DSL for tasks. Grammar (informal)::

    expr   := orexpr
    orexpr := andexpr ( "OR" andexpr )*
    andexpr:= unary  ( "AND" unary )*
    unary  := "NOT" unary | "(" expr ")" | atom
    atom   := key op value
    key    := identifier         ( e.g. status, owner, priority, title,
                                    state, sprint, due, created )
    op     := ":" | ":>" | ":<" | ":>=" | ":<=" | ":!="
    value  := bareword | quoted-string | iso-date | integer | float

Examples::

    status:open
    status:open AND owner:alice
    priority:>=high AND title:"login bug"
    NOT status:done AND created:>2026-01-01

The parser is hand-written (no third-party deps) and the evaluator
operates on already-loaded ORM rows or on a SQL :class:`select`. Both
modes are useful: in-memory eval is convenient for tests and for the
``lfql_match`` job hook; the SQL mode powers REST listing endpoints.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import and_ as sa_and, not_ as sa_not, or_ as sa_or, select
from sqlalchemy.orm import Session

from . import models
from .errors import ValidationError


# ---------------------------------------------------------------------------
# Field schema — what keys are allowed and how they coerce
# ---------------------------------------------------------------------------
_PRIORITY_RANK = {"low": 1, "medium": 2, "high": 3}

# key -> (kind, getter, sql_column_or_None)
# kind ∈ {"str", "int", "priority", "date"}
TASK_FIELDS: dict[str, tuple[str, Any, Any]] = {
    "status":   ("str",   lambda t: t.status,   models.Task.status),
    "state":    ("str",   lambda t: t.state,    models.Task.state),
    "owner":    ("str",   lambda t: (t.owner.handle.lower()
                                     if t.owner else None),
                 None),  # SQL eval requires a join → handled separately
    "title":    ("str",   lambda t: t.title,    models.Task.title),
    "kind":     ("str",   lambda t: t.kind,     models.Task.kind),
    "priority": ("priority", lambda t: t.priority, models.Task.priority),
    "due":      ("date",  lambda t: t.due_date, models.Task.due_date),
    "created":  ("date",  lambda t: t.created_at, models.Task.created_at),
    "effort":   ("int",   lambda t: t.effort_hours, models.Task.effort_hours),
}

VALID_OPS = frozenset({":", ":>", ":<", ":>=", ":<=", ":!="})


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------
@dataclass
class Token:
    kind: str   # 'WORD' | 'STRING' | 'AND' | 'OR' | 'NOT' | 'LP' | 'RP' | 'OP'
    value: str


_TOKEN_RE = re.compile(
    r"""
    \s+ |                                  # whitespace (skipped)
    (?P<lp>\() |
    (?P<rp>\)) |
    (?P<string>"([^"\\]|\\.)*") |          # double-quoted string
    (?P<op>:>=|:<=|:!=|:>|:<|:) |
    (?P<word>[A-Za-z0-9_][A-Za-z0-9_\-./@]*)  # words: no colon (operator)
    """,
    re.VERBOSE,
)
_KEYWORDS = {"AND", "OR", "NOT"}


def tokenize(text: str) -> list[Token]:
    out: list[Token] = []
    i = 0
    n = len(text)
    while i < n:
        m = _TOKEN_RE.match(text, i)
        if not m:
            raise ValidationError(f"unexpected character at {i!r}: {text[i]!r}")
        i = m.end()
        if m.group("lp"):
            out.append(Token("LP", "("))
        elif m.group("rp"):
            out.append(Token("RP", ")"))
        elif m.group("string"):
            raw = m.group("string")[1:-1]
            out.append(Token("STRING", raw.encode().decode("unicode_escape")))
        elif m.group("op"):
            out.append(Token("OP", m.group("op")))
        elif m.group("word"):
            w = m.group("word")
            if w in _KEYWORDS:
                out.append(Token(w, w))
            else:
                out.append(Token("WORD", w))
        # else: pure whitespace → skip
    return out


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------
@dataclass
class And: left: "Node"; right: "Node"  # noqa: E702
@dataclass
class Or:  left: "Node"; right: "Node"  # noqa: E702
@dataclass
class Not: child: "Node"                # noqa: E702
@dataclass
class Atom:
    key: str
    op: str
    value: Any        # already coerced for the field's kind


Node = Any  # And | Or | Not | Atom


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
class _Parser:
    def __init__(self, toks: list[Token]) -> None:
        self.toks = toks
        self.i = 0

    def peek(self) -> Token | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def eat(self, kind: str | None = None) -> Token:
        if self.i >= len(self.toks):
            raise ValidationError("unexpected end of expression")
        t = self.toks[self.i]
        if kind is not None and t.kind != kind:
            raise ValidationError(f"expected {kind}, got {t.kind} ({t.value!r})")
        self.i += 1
        return t

    def parse(self) -> Node:
        node = self.parse_or()
        if self.i != len(self.toks):
            tail = self.toks[self.i].value
            raise ValidationError(f"trailing tokens at {tail!r}")
        return node

    def parse_or(self) -> Node:
        node = self.parse_and()
        while self.peek() and self.peek().kind == "OR":
            self.eat("OR")
            node = Or(node, self.parse_and())
        return node

    def parse_and(self) -> Node:
        node = self.parse_unary()
        while self.peek() and self.peek().kind == "AND":
            self.eat("AND")
            node = And(node, self.parse_unary())
        return node

    def parse_unary(self) -> Node:
        t = self.peek()
        if t is None:
            raise ValidationError("unexpected end of expression")
        if t.kind == "NOT":
            self.eat("NOT")
            return Not(self.parse_unary())
        if t.kind == "LP":
            self.eat("LP")
            inner = self.parse_or()
            self.eat("RP")
            return inner
        return self.parse_atom()

    def parse_atom(self) -> Node:
        key_tok = self.eat("WORD")
        key = key_tok.value.lower()
        if key not in TASK_FIELDS:
            raise ValidationError(
                f"unknown field {key!r}; valid: {sorted(TASK_FIELDS)}"
            )
        op_tok = self.eat("OP")
        op = op_tok.value
        if op not in VALID_OPS:
            raise ValidationError(f"unknown operator {op!r}")
        v_tok = self.peek()
        if v_tok is None or v_tok.kind not in ("WORD", "STRING"):
            raise ValidationError(f"expected value after {key}{op}")
        self.eat()
        coerced = _coerce(key, v_tok.value)
        return Atom(key, op, coerced)


def _coerce(key: str, raw: str) -> Any:
    kind, _, _ = TASK_FIELDS[key]
    if kind == "int":
        try:
            return float(raw)
        except ValueError as exc:
            raise ValidationError(f"{key}: not a number {raw!r}") from exc
    if kind == "priority":
        if raw not in _PRIORITY_RANK:
            raise ValidationError(
                f"priority must be one of {sorted(_PRIORITY_RANK)}"
            )
        return raw
    if kind == "date":
        try:
            return datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValidationError(f"{key}: not an ISO date {raw!r}") from exc
    return raw


def parse(text: str) -> Node:
    """Parse an LFQL expression. Raises :class:`ValidationError` on bad input."""
    text = (text or "").strip()
    if not text:
        raise ValidationError("empty query")
    return _Parser(tokenize(text)).parse()


# ---------------------------------------------------------------------------
# In-memory evaluator
# ---------------------------------------------------------------------------
def _cmp(op: str, lhs: Any, rhs: Any) -> bool:
    if lhs is None:
        return op == ":!=" and rhs is not None
    if op == ":":
        if isinstance(rhs, str) and isinstance(lhs, str):
            return rhs.lower() in lhs.lower()
        return lhs == rhs
    if op == ":!=":
        return lhs != rhs
    try:
        if op == ":>":  return lhs > rhs
        if op == ":<":  return lhs < rhs
        if op == ":>=": return lhs >= rhs
        if op == ":<=": return lhs <= rhs
    except TypeError:
        return False
    return False


def evaluate(node: Node, task: models.Task) -> bool:
    if isinstance(node, And):
        return evaluate(node.left, task) and evaluate(node.right, task)
    if isinstance(node, Or):
        return evaluate(node.left, task) or evaluate(node.right, task)
    if isinstance(node, Not):
        return not evaluate(node.child, task)
    if isinstance(node, Atom):
        kind, getter, _ = TASK_FIELDS[node.key]
        actual = getter(task)
        rhs = node.value
        if kind == "priority":
            actual_rank = _PRIORITY_RANK.get(actual, 0)
            rhs_rank = _PRIORITY_RANK[rhs]
            if node.op == ":":   return actual == rhs
            if node.op == ":!=": return actual != rhs
            if node.op == ":>":  return actual_rank > rhs_rank
            if node.op == ":<":  return actual_rank < rhs_rank
            if node.op == ":>=": return actual_rank >= rhs_rank
            if node.op == ":<=": return actual_rank <= rhs_rank
        return _cmp(node.op, actual, rhs)
    raise AssertionError(f"unknown node {type(node)!r}")


# ---------------------------------------------------------------------------
# Convenience: run a query against a team's tasks (in-memory eval)
# ---------------------------------------------------------------------------
def run(sess: Session, *, team_id: int, query: str,
        limit: int = 1000) -> list[models.Task]:
    """Parse ``query`` and return matching tasks for ``team_id``.

    The evaluator runs in Python after fetching all team tasks. This
    keeps the parser simple and avoids hand-rolling SQL for every
    operator. For team sizes seen in practice (a few thousand open
    tasks) this is fast enough; the dedicated SQL pre-filter on
    ``team_id`` keeps the working set small.
    """
    ast = parse(query)
    rows = sess.execute(
        select(models.Task).where(models.Task.team_id == team_id)
    ).scalars().all()
    out: list[models.Task] = []
    for t in rows:
        if evaluate(ast, t):
            out.append(t)
            if len(out) >= limit:
                break
    return out


def explain(query: str) -> dict[str, Any]:
    """Parse the query and return a JSON-ish representation of the AST.

    Used by ``GET /api/lfql/explain`` so clients can verify how a query
    is being interpreted before saving it as a smart list / report.
    """
    def to_json(n: Node) -> Any:
        if isinstance(n, And):
            return {"and": [to_json(n.left), to_json(n.right)]}
        if isinstance(n, Or):
            return {"or": [to_json(n.left), to_json(n.right)]}
        if isinstance(n, Not):
            return {"not": to_json(n.child)}
        if isinstance(n, Atom):
            v = n.value.isoformat() if isinstance(n.value, datetime) else n.value
            return {"atom": {"key": n.key, "op": n.op, "value": v}}
        raise AssertionError
    return to_json(parse(query))


__all__ = [
    "Atom", "And", "Or", "Not", "Token", "TASK_FIELDS",
    "tokenize", "parse", "evaluate", "run", "explain",
]
