"""Backend registry — picks the active backend from settings."""
from __future__ import annotations

from typing import Dict

from ...config import get_settings
from .base import ExtractorBackend
from .llm import LLMBackend
from .rules import RulesBackend

_REGISTRY: Dict[str, type[ExtractorBackend]] = {
    "rules": RulesBackend,
    "llm": LLMBackend,
}


def register_backend(name: str, cls: type[ExtractorBackend]) -> None:
    _REGISTRY[name] = cls


def get_backend(name: str | None = None) -> ExtractorBackend:
    """Return a backend instance by name (defaults to settings)."""
    chosen = name or get_settings().extraction_backend
    cls = _REGISTRY.get(chosen)
    if cls is None:
        raise ValueError(f"unknown extraction backend: {chosen!r}")
    return cls()
