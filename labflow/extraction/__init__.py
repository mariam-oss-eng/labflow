"""Extraction pipeline for LabFlow.

The default backend is deterministic and rule-based. An LLM backend can be
swapped in by setting ``LABFLOW_LLM_PROVIDER`` and providing a function that
returns an :class:`labflow.schemas.ExtractionResult`.
"""
from .pipeline import extract  # re-export

__all__ = ["extract"]
