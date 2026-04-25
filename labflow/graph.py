"""Decision graph + Mermaid renderer (v0.4).

A team's "decision graph" is the partial order over :class:`Decision`
rows induced by ``superseded_by_id``. We expose two views:

  * :func:`build_graph` — typed nodes/edges suitable for JSON/API output
  * :func:`render_mermaid` — a `Mermaid <https://mermaid.js.org>`_
    flowchart string ready to drop into Markdown, Notion, or the in-app
    review page (we render via the Mermaid CDN script in the dashboard
    template).

Why Mermaid over Graphviz / vis.js / d3? — Mermaid is a small JS bundle
(~250kB), text-based (so we generate the diagram server-side without any
build pipeline), and natively supported in GitHub Markdown so the same
graph can be pasted into a PR description or a wiki page.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models


@dataclass
class GraphNode:
    id: int
    statement: str
    confidence: float
    superseded: bool
    meeting_id: int


@dataclass
class GraphEdge:
    """An ``older → newer`` supersession edge."""
    src: int  # older decision (superseded)
    dst: int  # newer decision (replacement)


@dataclass
class DecisionGraph:
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "nodes": [n.__dict__ for n in self.nodes],
            "edges": [e.__dict__ for e in self.edges],
        }


def build_graph(sess: Session, *, team_id: int) -> DecisionGraph:
    """Return the decision graph for a team."""
    rows = list(
        sess.execute(
            select(models.Decision)
            .where(models.Decision.team_id == team_id)
            .order_by(models.Decision.id)
        ).scalars()
    )
    nodes = [
        GraphNode(
            id=d.id,
            statement=d.statement,
            confidence=float(d.confidence or 0.0),
            superseded=d.superseded_by_id is not None,
            meeting_id=d.meeting_id,
        )
        for d in rows
    ]
    edges = [
        GraphEdge(src=d.id, dst=d.superseded_by_id)
        for d in rows
        if d.superseded_by_id is not None
    ]
    return DecisionGraph(nodes=nodes, edges=edges)


def render_mermaid(graph: DecisionGraph, *, max_chars: int = 60) -> str:
    """Render a ``flowchart LR`` Mermaid diagram for ``graph``.

    ``max_chars`` truncates long statements so the diagram stays
    readable; full text is always available via the API.
    """
    lines: list[str] = ["flowchart LR"]
    if not graph.nodes:
        lines.append('  empty["(no decisions yet)"]')
        return "\n".join(lines)
    for n in graph.nodes:
        label = _escape(n.statement[:max_chars])
        if len(n.statement) > max_chars:
            label += "…"
        if n.superseded:
            lines.append(f'  d{n.id}["{label}"]:::superseded')
        else:
            lines.append(f'  d{n.id}["{label}"]')
    for e in graph.edges:
        lines.append(f"  d{e.src} --> d{e.dst}")
    lines.append("  classDef superseded fill:#f5f5f5,stroke:#999,color:#888,"
                 "stroke-dasharray: 5 5;")
    return "\n".join(lines)


def _escape(s: str) -> str:
    """Mermaid label escapes: drop characters that break the parser."""
    return (
        s.replace("\\", " ")
         .replace('"', "'")
         .replace("\n", " ")
         .replace("[", "(")
         .replace("]", ")")
    )
