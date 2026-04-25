"""Extraction pipeline orchestrator.

Public entrypoint: :func:`extract`. Delegates to the active extractor
backend (see :mod:`labflow.extraction.backends`). The default rules
backend keeps the package fully offline; an LLM backend can be enabled
by setting ``LABFLOW_EXTRACTION_BACKEND=llm`` and ``LABFLOW_LLM_CALLABLE``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..schemas import ExtractionResult
from .backends import get_backend


def extract(text: str, reference: Optional[datetime] = None) -> ExtractionResult:
    """Run the configured extractor backend on a transcript / notes blob."""
    return get_backend().extract(text, reference=reference)
