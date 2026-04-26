"""AI summary backend (v0.6).

Same shape as :mod:`labflow.embeddings` — a deterministic offline default
plus a callable hook for swapping in a real LLM via env. The default
implementation is **TextRank-lite**: build a sentence-similarity graph
over the transcript and pick the top-N sentences by eigenvector-weight.
It runs offline in pure Python in well under 100 ms for typical meeting
transcripts (single digits of KB).

Operators who want a real LLM set::

    LABFLOW_SUMMARY_CALLABLE=myproj.llm:summarize

The callable signature is ``(text: str, *, max_sentences: int) -> str``
and returns the summary text. Failures fall back to TextRank so the
endpoint never breaks.
"""
from __future__ import annotations

import importlib
import logging
import math
import re
from collections import Counter
from typing import Callable

from .config import get_settings

log = logging.getLogger("labflow.summary")

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_TOKEN = re.compile(r"[a-z0-9']+")
_STOP = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on",
    "at", "for", "by", "with", "as", "is", "was", "were", "be", "been",
    "are", "this", "that", "it", "we", "you", "they", "i", "our",
    "from", "have", "has", "will", "would", "could", "should",
})


def _split_sentences(text: str) -> list[str]:
    txt = (text or "").replace("\n", " ").strip()
    if not txt:
        return []
    parts = _SENT_SPLIT.split(txt)
    return [p.strip() for p in parts if len(p.strip()) >= 8]


def _tokenize(s: str) -> list[str]:
    return [t for t in _TOKEN.findall(s.lower()) if t not in _STOP and len(t) > 2]


def _similarity(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    ca, cb = Counter(a), Counter(b)
    common = set(ca) & set(cb)
    if not common:
        return 0.0
    overlap = sum(min(ca[w], cb[w]) for w in common)
    return overlap / (math.log(len(a) + 1) + math.log(len(b) + 1))


def textrank(text: str, *, max_sentences: int = 5) -> str:
    """Deterministic offline summarizer.

    Builds a sentence-similarity graph and approximates PageRank over it
    (10 power iterations is more than enough for the small graphs we
    handle). Returns the top sentences in **document order** so the
    summary still reads chronologically.
    """
    sents = _split_sentences(text)
    if len(sents) <= max_sentences:
        return " ".join(sents)
    toks = [_tokenize(s) for s in sents]
    n = len(sents)
    sim = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            s = _similarity(toks[i], toks[j])
            sim[i][j] = sim[j][i] = s
    # Normalize rows.
    for i in range(n):
        row_sum = sum(sim[i]) or 1.0
        sim[i] = [v / row_sum for v in sim[i]]
    # Power iteration.
    rank = [1.0 / n] * n
    damping = 0.85
    for _ in range(20):
        new = [(1.0 - damping) / n] * n
        for i in range(n):
            for j in range(n):
                new[j] += damping * rank[i] * sim[i][j]
        rank = new
    ranked = sorted(range(n), key=lambda i: rank[i], reverse=True)[:max_sentences]
    chosen = sorted(ranked)
    return " ".join(sents[i] for i in chosen)


def _resolve_callable(path: str) -> Callable[..., str] | None:
    if not path or ":" not in path:
        return None
    mod, _, fn = path.partition(":")
    try:
        return getattr(importlib.import_module(mod), fn)
    except Exception:  # noqa: BLE001
        log.warning("summary callable %r not importable", path, exc_info=True)
        return None


def summarize(text: str, *, max_sentences: int = 5) -> dict:
    """Public entry point. Returns ``{"summary": ..., "backend": ...}``."""
    settings = get_settings()
    path = getattr(settings, "summary_callable", "") or ""
    if path:
        fn = _resolve_callable(path)
        if fn is not None:
            try:
                out = fn(text, max_sentences=max_sentences)
                if isinstance(out, str) and out.strip():
                    return {"summary": out.strip(), "backend": path}
            except Exception:  # noqa: BLE001
                log.exception("summary callable %r raised; falling back", path)
    return {"summary": textrank(text, max_sentences=max_sentences),
            "backend": "textrank"}
