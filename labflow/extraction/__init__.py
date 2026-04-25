"""Extraction pipeline for LabFlow.

The default backend is deterministic and rule-based. An LLM backend can be
swapped in by setting ``LABFLOW_EXTRACTION_BACKEND=llm`` and
``LABFLOW_LLM_CALLABLE`` to a dotted path of a callable that returns JSON
matching :class:`labflow.schemas.ExtractionResult`.
"""
from .backends import ExtractorBackend, LLMBackend, RulesBackend, get_backend
from .pipeline import extract  # re-export

__all__ = ["extract", "ExtractorBackend", "RulesBackend", "LLMBackend", "get_backend"]
