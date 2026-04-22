"""Owner detection and assignment for action items.

We support three forms commonly seen in technical-team transcripts:

  * ``@handle`` mentions
  * ``[Name]`` brackets
  * ``Name will <verb>`` capitalized name immediately preceding a verb

Owner handles are normalized lowercase. Display names are derived from the
raw mention if possible.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from ..schemas import ExtractedOwner

_AT_MENTION = re.compile(r"@([A-Za-z][A-Za-z0-9_\-]{0,63})")
_BRACKET = re.compile(r"\[([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)\]")
_NAME_VERB = re.compile(
    r"\b([A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{1,20})?)\s+"
    r"(?:will|to|should|is going to|owns?|takes?)\b"
)


@dataclass(frozen=True)
class OwnerMention:
    handle: str
    display_name: str
    span: tuple[int, int]


def _normalize_handle(raw: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "", raw.lower().strip())[:64] or "unassigned"


def _to_display(raw: str) -> str:
    cleaned = raw.replace("_", " ").replace("-", " ").strip()
    return " ".join(part.capitalize() for part in cleaned.split())


def find_owner_mentions(text: str) -> List[OwnerMention]:
    """Return all owner mentions in ``text`` in textual order."""
    found: List[OwnerMention] = []
    seen: set[tuple[str, int]] = set()
    for m in _AT_MENTION.finditer(text):
        h = _normalize_handle(m.group(1))
        key = (h, m.start())
        if key in seen:
            continue
        seen.add(key)
        found.append(OwnerMention(h, _to_display(m.group(1)), m.span()))
    for m in _BRACKET.finditer(text):
        h = _normalize_handle(m.group(1))
        key = (h, m.start())
        if key in seen:
            continue
        seen.add(key)
        found.append(OwnerMention(h, _to_display(m.group(1)), m.span()))
    for m in _NAME_VERB.finditer(text):
        h = _normalize_handle(m.group(1))
        key = (h, m.start())
        if key in seen:
            continue
        seen.add(key)
        found.append(OwnerMention(h, _to_display(m.group(1)), m.span()))
    found.sort(key=lambda om: om.span[0])
    return found


def assign_owner(sentence: str) -> Optional[str]:
    """Pick the most likely owner handle for an action item.

    Heuristic: prefer the *first* mention in the sentence; fall back to the
    last mention seen in earlier context if needed (handled by the pipeline).
    """
    mentions = find_owner_mentions(sentence)
    return mentions[0].handle if mentions else None


def collect_unique_owners(text: str) -> List[ExtractedOwner]:
    """Roll up all unique owners across a transcript."""
    by_handle: dict[str, ExtractedOwner] = {}
    for m in find_owner_mentions(text):
        if m.handle not in by_handle:
            by_handle[m.handle] = ExtractedOwner(
                handle=m.handle, display_name=m.display_name or m.handle
            )
    return list(by_handle.values())
