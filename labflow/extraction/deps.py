"""Dependency linking between extracted tasks.

We support two dependency signals:

  * Explicit phrases: "depends on X", "after X is done", "blocked by X"
  * Implicit ordering: "first ..., then ...", "once X, do Y"

Resolution is title-similarity based (token overlap). We don't try to be
clever with pronoun resolution — for the MVP a simple, transparent
implementation is preferable to a brittle one.
"""
from __future__ import annotations

import re
from typing import List

from ..schemas import ExtractedTask

_DEP_PATTERNS = (
    re.compile(r"depends on ([^.;\n]+)", re.I),
    re.compile(r"after ([^,.;\n]+?)(?: is done| ships| lands| merges)", re.I),
    re.compile(r"blocked by ([^.;\n]+)", re.I),
    re.compile(r"once ([^,]+?),", re.I),
)

_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for",
    "is", "are", "be", "with", "by", "we", "i", "will", "should",
    "this", "that", "it",
}


def _tokens(text: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9]{3,}", text.lower())
        if w not in _STOPWORDS
    }


def _best_match(reference: str, candidates: List[ExtractedTask]) -> str | None:
    ref_tokens = _tokens(reference)
    if not ref_tokens:
        return None
    best: tuple[float, str | None] = (0.0, None)
    for cand in candidates:
        cand_tokens = _tokens(cand.title)
        if not cand_tokens:
            continue
        overlap = len(ref_tokens & cand_tokens)
        score = overlap / max(len(ref_tokens | cand_tokens), 1)
        if score > best[0]:
            best = (score, cand.title)
    # Require a meaningful overlap so we don't link unrelated tasks.
    return best[1] if best[0] >= 0.34 else None


def link_dependencies(tasks: List[ExtractedTask]) -> List[ExtractedTask]:
    """Mutate each task's ``depends_on_titles`` based on its source span.

    Returns the same list (modified in place) for convenience.
    """
    for t in tasks:
        text = (t.source_span or "") + " " + (t.description or "")
        if not text.strip():
            continue
        candidates = [other for other in tasks if other is not t]
        seen: set[str] = set()
        for pat in _DEP_PATTERNS:
            for m in pat.finditer(text):
                target = _best_match(m.group(1), candidates)
                if target and target not in seen and target != t.title:
                    seen.add(target)
                    t.depends_on_titles.append(target)
    return tasks


def topological_levels(tasks: List[ExtractedTask]) -> List[List[str]]:
    """Group task titles by dependency depth.

    Useful for the dashboard's "what's unblocked now" view. Cycles are
    broken by emitting remaining tasks in arbitrary order at the end.
    """
    by_title = {t.title: t for t in tasks}
    indeg: dict[str, int] = {t.title: 0 for t in tasks}
    for t in tasks:
        for dep in t.depends_on_titles:
            if dep in by_title:
                indeg[t.title] += 1

    levels: List[List[str]] = []
    remaining = set(by_title.keys())
    while remaining:
        ready = sorted(t for t in remaining if indeg[t] == 0)
        if not ready:
            # Cycle — emit the rest as a final level so we don't loop forever.
            levels.append(sorted(remaining))
            break
        levels.append(ready)
        for t in ready:
            remaining.discard(t)
            for other in tasks:
                if t in other.depends_on_titles and other.title in remaining:
                    indeg[other.title] -= 1
    return levels
