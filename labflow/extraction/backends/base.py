"""Backend interface for transcript extraction."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from ...schemas import ExtractionResult


class ExtractorBackend(ABC):
    """Convert raw transcript/notes text into a validated ``ExtractionResult``.

    Implementations must be **side-effect free** and **deterministic when
    given the same input + reference time** (or document any non-determinism
    explicitly). They must validate their output through ``ExtractionResult``
    so downstream code can trust the schema.
    """

    name: str = "base"

    @abstractmethod
    def extract(self, text: str, *, reference: Optional[datetime] = None) -> ExtractionResult:
        ...
