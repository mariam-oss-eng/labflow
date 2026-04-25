"""Deterministic rule-based backend (the default)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from ...schemas import ExtractionResult
from ..deps import link_dependencies
from ..owners import collect_unique_owners
from ..rules import (
    extract_assumptions,
    extract_blockers,
    extract_decisions,
    extract_experiments,
    extract_tasks,
    split_sentences,
)
from .base import ExtractorBackend


class RulesBackend(ExtractorBackend):
    """Regex / heuristic pipeline. Runs offline and is fully deterministic."""

    name = "rules"

    def extract(self, text: str, *, reference: Optional[datetime] = None) -> ExtractionResult:
        sentences = split_sentences(text)
        owners = collect_unique_owners(text)
        decisions = extract_decisions(sentences)
        tasks = extract_tasks(sentences, reference=reference)
        experiments = extract_experiments(sentences)
        assumptions = extract_assumptions(sentences)
        blockers = extract_blockers(sentences)
        link_dependencies(tasks)
        return ExtractionResult(
            owners=owners,
            decisions=decisions,
            tasks=tasks,
            experiments=experiments,
            assumptions=assumptions,
            blockers=blockers,
        )
