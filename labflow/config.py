"""Application configuration via pydantic-settings.

Settings are loaded from environment variables and an optional ``.env``
file (see ``.env.example``). They are validated at startup so misconfigured
deployments fail fast rather than at first request.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed runtime settings.

    Environment variables are prefixed with ``LABFLOW_`` (e.g.
    ``LABFLOW_DATABASE_URL``). Values are validated by Pydantic so the
    process refuses to start on a bad config instead of crashing at
    request time.
    """

    model_config = SettingsConfigDict(
        env_prefix="LABFLOW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- core ----------------------------------------------------------
    database_url: str = "sqlite:///./labflow.db"
    environment: Literal["development", "staging", "production", "test"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = False  # default to human-readable in dev
    cors_origins: list[str] = Field(default_factory=list)

    # --- security ------------------------------------------------------
    # When auth_enabled is False (default) we run in single-team mode and
    # auto-create a "default" team + key for backwards-compatible local use.
    # Production deployments should always set this True.
    auth_enabled: bool = False
    # If set, auto-create this team + a single API key on first boot.
    bootstrap_team: str = "default"
    bootstrap_api_key: str = ""

    # --- limits --------------------------------------------------------
    max_transcript_bytes: int = 1_000_000  # 1 MB
    default_page_size: int = 50
    max_page_size: int = 200

    # --- extraction ----------------------------------------------------
    extraction_backend: Literal["rules", "llm"] = "rules"
    # Optional dotted path to a callable used by the LLM backend, e.g.
    # "myproj.llm:complete". The callable receives the prompt and must
    # return a JSON string conforming to ExtractionResult.
    llm_callable: str = ""

    # --- semantic search (v0.4) ---------------------------------------
    # Embeddings: deterministic offline by default. Set
    # ``LABFLOW_EMBEDDING_CALLABLE`` to a "module:fn" path to use a real
    # provider. Vectors are persisted as JSON arrays in SQL — no special
    # vector store required at the scale LabFlow targets.
    embedding_callable: str = ""
    embedding_dim: int = 256
    # Hybrid ranking weights for /api/search. ``alpha`` is the weight on
    # semantic similarity; (1-alpha) is the weight on lexical/BM25-ish.
    search_alpha: float = 0.5

    # --- rate limiting (v0.4) -----------------------------------------
    # Per-team token bucket. Set rate_limit_per_minute<=0 to disable.
    rate_limit_per_minute: int = 600
    rate_limit_burst: int = 60

    # --- idempotency (v0.4) -------------------------------------------
    # How long to retain Idempotency-Key replays (in seconds).
    idempotency_ttl_seconds: int = 24 * 3600

    # --- encryption at rest (v0.5) ------------------------------------
    # Optional Fernet key (urlsafe base64). When set, transcripts and
    # notes are encrypted before being written to the DB. Generate with
    # ``python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"``.
    data_key: str = ""

    # --- summary (v0.6) ------------------------------------------------
    # Optional dotted path "module:fn" used by /api/meetings/{id}/summary.
    # The callable receives ``(text: str, *, max_sentences: int) -> str``.
    summary_callable: str = ""

    # --- observability (v0.7) -----------------------------------------
    otel_enabled: bool = False

    # --- webhooks (v0.3) -----------------------------------------------
    webhook_signing_secret: str = ""
    github_webhook_secret: str = ""

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors(cls, v):
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @field_validator("database_url")
    @classmethod
    def _check_db_url(cls, v: str) -> str:
        if not v or "://" not in v:
            raise ValueError("LABFLOW_DATABASE_URL must be a SQLAlchemy URL")
        return v


@lru_cache(maxsize=1)
def _cached() -> Settings:
    return Settings()


def get_settings() -> Settings:
    """Return the cached :class:`Settings`. Use :func:`reset_settings_cache`
    in tests when env vars change between cases.
    """
    return _cached()


def reset_settings_cache() -> None:
    """Clear the settings cache so the next ``get_settings()`` call re-reads env."""
    _cached.cache_clear()

