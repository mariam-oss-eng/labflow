"""Extraction pipeline orchestrator.

Public entrypoint: :func:`extract`. Composes:

  1. sentence segmentation
  2. owner roll-up
  3. decision / task / experiment / assumption / blocker rules
  4. dependency linking across tasks
  5. final Pydantic validation (via ``ExtractionResult``)

The pipeline is deterministic and side-effect free, which makes it trivial
to test and reproduce.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..schemas import ExtractionResult
from .deps import link_dependencies
from .owners import collect_unique_owners
from .rules import (
    extract_assumptions,
    extract_blockers,
    extract_decisions,
    extract_experiments,
    extract_tasks,
    split_sentences,
)


def extract(text: str, reference: Optional[datetime] = None) -> ExtractionResult:
    """Run the extraction pipeline on a transcript / notes blob."""
    sentences = split_sentences(text)

    owners = collect_unique_owners(text)
    decisions = extract_decisions(sentences)
    tasks = extract_tasks(sentences, reference=reference)
    experiments = extract_experiments(sentences)
    assumptions = extract_assumptions(sentences)
    blockers = extract_blockers(sentences)

    link_dependencies(tasks)

    # Validate by re-constructing the top-level model — guarantees the
    # output conforms to the published schema before we persist it.
    return ExtractionResult(
        owners=owners,
        decisions=decisions,
        tasks=tasks,
        experiments=experiments,
        assumptions=assumptions,
        blockers=blockers,
    )
