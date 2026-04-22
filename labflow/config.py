"""Application configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Runtime settings.

    Fields are populated by :func:`get_settings`, which re-reads the
    environment on each call. This matters for tests that monkeypatch
    ``LABFLOW_DATABASE_URL`` between cases.
    """

    database_url: str
    llm_provider: str


def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv("LABFLOW_DATABASE_URL", "sqlite:///./labflow.db"),
        # Optional LLM extraction backend. When unset, deterministic
        # rule-based extraction is used so the MVP runs offline and tests
        # are reproducible without API keys.
        llm_provider=os.getenv("LABFLOW_LLM_PROVIDER", "rules"),
    )
