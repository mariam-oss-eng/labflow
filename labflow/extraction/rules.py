"""Rule-based extractors.

Designed for technical-team transcripts: research standups, experiment
reviews, project kickoffs, postmortems. Each extractor returns objects
already validated against the Pydantic schema.

The rules are intentionally explicit and easily auditable. When a sentence
matches multiple categories, the most specific category wins
(experiment > task > decision > assumption > blocker).
"""
from __future__ import annotations

import re
from typing import Iterable, List

from ..schemas import (
    ExtractedAssumption,
    ExtractedBlocker,
    ExtractedDecision,
    ExtractedExperiment,
    ExtractedTask,
)
from .dates import parse_due_date
from .owners import assign_owner
from .uncertainty import assumption_risk, is_assumption, score_uncertainty

# ---- speaker-prefix normalization -----------------------------------------
# Lines like "@carol: I'll run the ablation" are rewritten to
# "@carol will run the ablation" so downstream rules can attribute the
# action to the correct owner without needing pronoun resolution.
_SPEAKER_PREFIX = re.compile(
    r"^\s*(?:@(?P<handle>[A-Za-z][\w\-]*)|"
    r"\[(?P<bracket>[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)\])\s*:\s*",
)
_FIRST_PERSON_VERB = re.compile(
    r"\bI(?:'|’)?(?:ll|m going to|d like to)\b|\bI will\b|\bI(?:'|’)?m going to\b|"
    r"\bwe(?:'|’)?ll\b|\bwe will\b",
    re.I,
)


def _normalize_speaker_line(line: str) -> str:
    m = _SPEAKER_PREFIX.match(line)
    if not m:
        return line
    handle = m.group("handle") or (m.group("bracket") or "").split()[0]
    if not handle:
        return line
    rest = line[m.end():]
    rest = _FIRST_PERSON_VERB.sub("will", rest, count=1)
    return f"@{handle} {rest}"


# ---- sentence splitter -----------------------------------------------------
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z@\[])|\n+")


def split_sentences(text: str) -> List[str]:
    if not text:
        return []
    # Apply per-line speaker normalization first so attribution is correct.
    lines = [_normalize_speaker_line(l) for l in text.splitlines()]
    normalized = "\n".join(lines)
    parts = [s.strip() for s in _SENT_SPLIT.split(normalized) if s and s.strip()]
    return parts


# ---- decision rules --------------------------------------------------------
_DECISION_PATTERNS = (
    re.compile(r"\bwe (?:have )?decided (?:to|that) (.+)", re.I),
    re.compile(r"\bdecision[:\-]\s*(.+)", re.I),
    re.compile(r"\bwe(?:'|’)?ll (?:go with|use|adopt) (.+)", re.I),
    re.compile(r"\blet(?:'|’)?s (?:go with|use|adopt) (.+)", re.I),
    re.compile(r"\bagreed (?:to|that) (.+)", re.I),
)


def extract_decisions(sentences: Iterable[str]) -> List[ExtractedDecision]:
    out: List[ExtractedDecision] = []
    seen: set[str] = set()
    for s in sentences:
        for pat in _DECISION_PATTERNS:
            m = pat.search(s)
            if not m:
                continue
            statement = m.group(1).strip().rstrip(".")
            if not statement or statement.lower() in seen:
                break
            seen.add(statement.lower())
            uncertainty = score_uncertainty(s)
            out.append(
                ExtractedDecision(
                    statement=statement,
                    rationale=s if s.lower() != statement.lower() else None,
                    confidence=max(0.0, 1.0 - uncertainty),
                )
            )
            break
    return out


# ---- task / action-item rules ---------------------------------------------
_TASK_PATTERNS = (
    # "@alice will retrain the model"
    re.compile(r"@[A-Za-z][\w\-]*\s+(?:will|to|should)\s+(.+)", re.I),
    # "Alice will retrain the model"
    re.compile(
        r"\b[A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{1,20})?\s+"
        r"(?:will|to|should|is going to)\s+(.+)",
    ),
    # "TODO: retrain the model"
    re.compile(r"\b(?:TODO|ACTION)[:\-]\s*(.+)", re.I),
    # "Action item: retrain ..."
    re.compile(r"\baction items?[:\-]\s*(.+)", re.I),
    # "[Alice] retrain the model"
    re.compile(r"\[[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?\]\s+(.+)"),
    # "Let's retrain the model" — only counts if there's an owner mention.
    re.compile(r"\b(?:let(?:'|’)?s|we need to|we should)\s+(.+)", re.I),
)

_CODE_HINTS = re.compile(r"\b(refactor|fix|implement|merge|rebase|deploy|ship|hotfix|"
                         r"PR|pull request|commit|repo|service|endpoint|API)\b", re.I)
_REVIEW_HINTS = re.compile(r"\b(review|approve|sign off|read|skim|read-through)\b", re.I)
_EXPERIMENT_HINTS = re.compile(r"\b(experiment|ablation|benchmark|sweep|eval|"
                               r"baseline|run training|train|fine-?tune)\b", re.I)


def _classify_kind(text: str) -> str:
    if _EXPERIMENT_HINTS.search(text):
        return "experiment"
    if _CODE_HINTS.search(text):
        return "code"
    if _REVIEW_HINTS.search(text):
        return "review"
    return "task"


_CONTINUATION = re.compile(
    r"^(?:depends on|blocked by|after\b|once\b|requires\b)",
    re.I,
)


def extract_tasks(sentences, reference=None) -> List[ExtractedTask]:
    out: List[ExtractedTask] = []
    seen_titles: set[str] = set()
    sentences = list(sentences)
    for idx, s in enumerate(sentences):
        for pat in _TASK_PATTERNS:
            m = pat.search(s)
            if not m:
                continue
            raw = m.group(1).strip()
            # Strip trailing deadline phrases from the title to keep it crisp.
            title = re.sub(
                r"\s+(?:by|on|in|next|tomorrow|today|eod|end of (?:the )?(?:week|month))\b.*$",
                "",
                raw,
                flags=re.I,
            ).strip().rstrip(".")
            if len(title) < 3:
                break
            key = title.lower()
            if key in seen_titles:
                break
            seen_titles.add(key)
            # Pull adjacent continuation sentences into the source span so
            # dependency phrases punctuated as separate sentences ("Depends
            # on X.") still resolve.
            span_parts = [s]
            j = idx + 1
            while j < len(sentences) and _CONTINUATION.match(sentences[j]):
                span_parts.append(sentences[j])
                j += 1
            span = " ".join(span_parts)
            out.append(
                ExtractedTask(
                    title=title,
                    description=span,
                    owner_handle=assign_owner(s),
                    due_date=parse_due_date(span, reference=reference),
                    kind=_classify_kind(span),
                    uncertainty=score_uncertainty(s),
                    confidence=max(0.0, 1.0 - score_uncertainty(s)),
                    source_span=span,
                )
            )
            break
    return out


# ---- experiment rules ------------------------------------------------------
_EXPERIMENT_PATTERNS = (
    re.compile(r"\bexperiment[:\-]\s*(.+)", re.I),
    re.compile(r"\b(?:run|propose|design)\s+(?:an? )?(?:experiment|ablation|sweep|benchmark)\s+(?:on|for|to)\s+(.+)", re.I),
    re.compile(r"\bhypothesis[:\-]\s*(.+)", re.I),
)

_METRIC_PATTERNS = re.compile(
    r"\b(?:accuracy|precision|recall|f1|auc|loss|perplexity|bleu|rouge|latency|throughput|MAE|RMSE)\b",
    re.I,
)


def extract_experiments(sentences: Iterable[str]) -> List[ExtractedExperiment]:
    out: List[ExtractedExperiment] = []
    seen: set[str] = set()
    for s in sentences:
        hypothesis = None
        name = None
        for pat in _EXPERIMENT_PATTERNS:
            m = pat.search(s)
            if not m:
                continue
            value = m.group(1).strip().rstrip(".")
            if "hypothesis" in pat.pattern:
                hypothesis = value
            else:
                name = value
            break
        if not name and not hypothesis:
            continue
        title = (name or hypothesis)[:255]
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        metrics = sorted({m.group(0).lower() for m in _METRIC_PATTERNS.finditer(s)})
        out.append(
            ExtractedExperiment(
                name=title,
                hypothesis=hypothesis,
                method=s if name else None,
                metrics=metrics,
                owner_handle=assign_owner(s),
            )
        )
    return out


# ---- assumption + blocker rules -------------------------------------------
def extract_assumptions(sentences: Iterable[str]) -> List[ExtractedAssumption]:
    out: List[ExtractedAssumption] = []
    seen: set[str] = set()
    for s in sentences:
        if not is_assumption(s):
            continue
        statement = re.sub(
            r"^(?:assumption[:\-]\s*|we assume\s+|assuming\s+)",
            "",
            s,
            flags=re.I,
        ).strip().rstrip(".")
        if len(statement) < 3 or statement.lower() in seen:
            continue
        seen.add(statement.lower())
        out.append(ExtractedAssumption(statement=statement, risk=assumption_risk(s)))
    return out


_BLOCKER_PATTERNS = (
    re.compile(r"\bblocked (?:on|by)\s+(.+)", re.I),
    re.compile(r"\bblocker[:\-]\s*(.+)", re.I),
    re.compile(r"\bwaiting (?:on|for)\s+(.+)", re.I),
)


def extract_blockers(sentences: Iterable[str]) -> List[ExtractedBlocker]:
    out: List[ExtractedBlocker] = []
    seen: set[str] = set()
    for s in sentences:
        for pat in _BLOCKER_PATTERNS:
            m = pat.search(s)
            if not m:
                continue
            desc = m.group(1).strip().rstrip(".")
            if len(desc) < 3 or desc.lower() in seen:
                break
            seen.add(desc.lower())
            out.append(ExtractedBlocker(description=desc))
            break
    return out
