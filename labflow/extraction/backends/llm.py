"""LLM-backed extractor with rules fallback.

The actual model call is delegated to a user-supplied callable referenced by
``LABFLOW_LLM_CALLABLE`` (e.g. ``"myproj.llm:complete"``). The callable
signature is::

    def complete(prompt: str, *, system: str | None = None) -> str

It must return a JSON string conforming to :class:`ExtractionResult`. This
keeps the framework model-agnostic — adapters for OpenAI, Anthropic, vLLM,
Ollama, etc. can live outside the project.

If the callable is missing, raises, or returns invalid JSON, we fall back
to :class:`RulesBackend` so a flaky model never breaks ingestion. The
fallback emits a structured warning log so operators can spot regressions.
"""
from __future__ import annotations

import importlib
import json
import logging
from datetime import datetime
from typing import Callable, Optional

from pydantic import ValidationError

from ...config import get_settings
from ...schemas import ExtractionResult
from .base import ExtractorBackend
from .rules import RulesBackend

log = logging.getLogger("labflow.extraction.llm")


SYSTEM_PROMPT = """You are LabFlow's extraction engine for technical-team meetings.
Output a single JSON object with this exact shape:
{
  "owners":      [{"handle": str, "display_name": str}],
  "decisions":   [{"statement": str, "rationale": str|null, "confidence": float}],
  "tasks":       [{"title": str, "description": str|null, "owner_handle": str|null,
                   "due_date": ISO8601|null, "kind": "task|code|experiment|review",
                   "uncertainty": float, "confidence": float,
                   "depends_on_titles": [str], "source_span": str|null}],
  "experiments": [{"name": str, "hypothesis": str|null, "method": str|null,
                   "metrics": [str], "dataset": str|null, "owner_handle": str|null}],
  "assumptions": [{"statement": str, "risk": "low|medium|high"}],
  "blockers":    [{"description": str, "blocked_task_title": str|null}]
}
Rules:
- Use null for missing values; do not hallucinate values.
- Owners are mentioned with @handle, [Name], or "Name will/should/needs to ...".
- Mark uncertain action items with uncertainty >= 0.5 and use language like "maybe", "we should look into".
- Never include extra fields. Output JSON only — no prose."""


def _import_callable(dotted: str) -> Callable[..., str]:
    module_path, _, attr = dotted.partition(":")
    if not module_path or not attr:
        raise ValueError(f"invalid LLM callable spec {dotted!r}; expected 'module:attr'")
    mod = importlib.import_module(module_path)
    fn = getattr(mod, attr)
    if not callable(fn):
        raise TypeError(f"{dotted!r} resolved to non-callable {fn!r}")
    return fn


class LLMBackend(ExtractorBackend):
    name = "llm"

    def __init__(self, *, fallback: ExtractorBackend | None = None,
                 callable_spec: str | None = None) -> None:
        self.fallback = fallback or RulesBackend()
        self._callable_spec = callable_spec
        self._callable: Callable[..., str] | None = None

    def _resolve_callable(self) -> Callable[..., str] | None:
        if self._callable is not None:
            return self._callable
        spec = self._callable_spec or get_settings().llm_callable
        if not spec:
            return None
        try:
            self._callable = _import_callable(spec)
        except Exception:  # noqa: BLE001
            log.exception("failed to import LLM callable %r", spec)
            return None
        return self._callable

    def _build_prompt(self, text: str, reference: Optional[datetime]) -> str:
        ref = reference.isoformat() if reference else "n/a"
        return f"REFERENCE_TIME: {ref}\n\nTRANSCRIPT:\n{text}"

    def extract(self, text: str, *, reference: Optional[datetime] = None) -> ExtractionResult:
        fn = self._resolve_callable()
        if fn is None:
            log.warning("LLM callable not configured; falling back to rules backend")
            return self.fallback.extract(text, reference=reference)

        try:
            raw = fn(self._build_prompt(text, reference), system=SYSTEM_PROMPT)
        except Exception:  # noqa: BLE001
            log.exception("LLM callable raised; falling back to rules backend")
            return self.fallback.extract(text, reference=reference)

        try:
            data = json.loads(raw)
            return ExtractionResult.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            log.warning("LLM output failed validation (%s); falling back to rules", exc)
            return self.fallback.extract(text, reference=reference)
