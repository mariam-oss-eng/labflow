"""Uncertainty classification.

We score how *uncertain* a sentence is on a 0..1 scale based on hedging
language commonly used in research conversations. The score is used both to:

  * mark tasks/decisions for explicit human review, and
  * surface assumptions the team is implicitly making.
"""
from __future__ import annotations

import re
from typing import Iterable

# (regex, weight). Weights are additive but capped at 1.0.
HEDGES: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(r"\bmaybe\b", re.I), 0.30),
    (re.compile(r"\bperhaps\b", re.I), 0.30),
    (re.compile(r"\bprobably\b", re.I), 0.25),
    (re.compile(r"\b(?:i|we)['’]?m? not sure\b", re.I), 0.45),
    (re.compile(r"\buncertain\b", re.I), 0.40),
    (re.compile(r"\bmight\b", re.I), 0.25),
    (re.compile(r"\bcould\b", re.I), 0.20),
    (re.compile(r"\bpossibly\b", re.I), 0.30),
    (re.compile(r"\bTBD\b"), 0.50),
    (re.compile(r"\bTODO\b"), 0.10),
    (re.compile(r"\bif (?:it|that|this) works\b", re.I), 0.30),
    (re.compile(r"\?\s*$"), 0.20),
    (re.compile(r"\bassuming\b", re.I), 0.25),
)

CERTAINTY_BOOSTS: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(r"\bdefinitely\b", re.I), -0.30),
    (re.compile(r"\bdecided\b", re.I), -0.20),
    (re.compile(r"\bwill\b", re.I), -0.05),
    (re.compile(r"\bmust\b", re.I), -0.15),
)


def score_uncertainty(sentence: str) -> float:
    """Return a value in [0.0, 1.0] reflecting hedging in ``sentence``."""
    if not sentence:
        return 0.0
    score = 0.0
    for pat, w in HEDGES:
        if pat.search(sentence):
            score += w
    for pat, w in CERTAINTY_BOOSTS:
        if pat.search(sentence):
            score += w
    return max(0.0, min(1.0, score))


def is_assumption(sentence: str) -> bool:
    """Heuristic check for assumption-flavored sentences."""
    if not sentence:
        return False
    s = sentence.strip().lower()
    triggers = (
        "assuming ",
        "we assume ",
        "assumption:",
        "if we assume",
        "presumably",
    )
    return any(t in s for t in triggers)


def assumption_risk(sentence: str) -> str:
    """Map an assumption sentence to a low/medium/high risk bucket."""
    score = score_uncertainty(sentence)
    if score >= 0.6:
        return "high"
    if score >= 0.3:
        return "medium"
    return "low"


def classify_many(sentences: Iterable[str]) -> list[float]:
    return [score_uncertainty(s) for s in sentences]
