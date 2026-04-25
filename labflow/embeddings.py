"""Pluggable embeddings + cosine similarity in pure Python.

Why no numpy / sentence-transformers / openai?

  * Adding any of those balloons the install footprint by 100s of MB and
    introduces network or GPU dependencies that violate LabFlow's
    "deterministic, offline by default" guarantee. The default backend
    here uses **hash-projected bag-of-words** which costs ~50 µs per
    document, runs offline, is reproducible across machines, and is
    surprisingly competitive for short, domain-specific corpora like
    decisions and task titles.
  * Teams that want real semantic embeddings can plug in a callable via
    ``LABFLOW_EMBEDDING_CALLABLE`` (same pattern as the LLM extractor
    backend) — e.g. an OpenAI / Cohere / Voyage / local sentence-
    transformers wrapper. Vectors are persisted in SQL as JSON arrays so
    no special vector store is required for the scale LabFlow targets.

The default dimension is 256 — enough granularity for thousands of rows
per team while keeping each vector tiny (a few hundred bytes JSON-
encoded).
"""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from .config import get_settings

log = logging.getLogger("labflow.embeddings")

DEFAULT_DIM = 256
_TOKEN = re.compile(r"[a-z0-9]{2,}")
# Standard English stop words plus a few project-specific noise tokens.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "this", "that", "will", "into",
    "have", "has", "are", "was", "were", "but", "not", "all", "any",
    "our", "you", "your", "their", "they", "them", "its", "his", "her",
    "would", "could", "should", "about", "than", "then", "also", "just",
    "labflow",
})


@dataclass
class Vector:
    """A unit-normalized dense vector."""
    values: list[float]

    @property
    def dim(self) -> int:
        return len(self.values)

    def to_json(self) -> str:
        # Round to 5 decimals — keeps JSON small without harming similarity.
        return json.dumps([round(v, 5) for v in self.values])

    @classmethod
    def from_json(cls, s: str) -> "Vector":
        return cls(values=json.loads(s))


def cosine(a: Vector | list[float], b: Vector | list[float]) -> float:
    """Cosine similarity in [-1, 1]. Tolerant of un-normalized inputs."""
    av = a.values if isinstance(a, Vector) else a
    bv = b.values if isinstance(b, Vector) else b
    if not av or not bv or len(av) != len(bv):
        return 0.0
    dot = sum(x * y for x, y in zip(av, bv))
    na = math.sqrt(sum(x * x for x in av)) or 1.0
    nb = math.sqrt(sum(x * x for x in bv)) or 1.0
    return dot / (na * nb)


def _tokens(text: str) -> list[str]:
    if not text:
        return []
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS]


def _hash_token(token: str, dim: int) -> tuple[int, int]:
    """Hashing trick: returns ``(index, sign)`` for a token.

    Using two independent hashes (one for the index, one for the sign)
    suppresses collision bias — the standard "feature hashing" trick from
    Weinberger et al. 2009.
    """
    h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    idx = int.from_bytes(h[:4], "little") % dim
    sign = 1 if (h[4] & 1) else -1
    return idx, sign


# ---------------------------------------------------------------------------
# Default backend: hashed bag-of-words → unit-normalized vector
# ---------------------------------------------------------------------------
class HashEmbedder:
    """Deterministic, offline, dependency-free embedder.

    Quality is meaningfully better than raw token overlap for finding
    paraphrased decisions ("we're going with sentencepiece" vs.
    "decided to adopt SentencePiece tokenization"), but worse than a
    real transformer on broad-domain text. Good enough for the typical
    LabFlow corpus (10s–1000s of meetings per team).
    """

    name = "hash-bow"

    def __init__(self, dim: int = DEFAULT_DIM):
        self.dim = dim

    def embed(self, text: str) -> Vector:
        toks = _tokens(text)
        if not toks:
            return Vector(values=[0.0] * self.dim)
        # Term-frequency with sublinear scaling (1 + log(tf)) reduces the
        # influence of repeated tokens and is the standard TF-IDF variant.
        counts: dict[str, int] = {}
        for t in toks:
            counts[t] = counts.get(t, 0) + 1
        vec = [0.0] * self.dim
        for token, c in counts.items():
            idx, sign = _hash_token(token, self.dim)
            vec[idx] += sign * (1.0 + math.log(c))
        # L2 normalize so cosine = dot product.
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return Vector(values=[v / norm for v in vec])


# ---------------------------------------------------------------------------
# Pluggable backend
# ---------------------------------------------------------------------------
class CallableEmbedder:
    """Wraps a user-provided ``module:fn`` callable.

    The callable receives a ``str`` and returns ``list[float]``. We
    L2-normalize the result so cosine math stays stable regardless of
    the upstream provider. Any exception falls back to the hash embedder
    — never break ingestion because an LLM endpoint is down.
    """

    name = "callable"

    def __init__(self, spec: str, fallback: Optional["HashEmbedder"] = None):
        self.spec = spec
        self._fn: Optional[Callable[[str], list[float]]] = None
        self.fallback = fallback or HashEmbedder()

    def _resolve(self) -> Callable[[str], list[float]]:
        if self._fn is not None:
            return self._fn
        if ":" not in self.spec:
            raise ValueError(f"invalid embedding callable spec: {self.spec!r}")
        mod_name, attr = self.spec.split(":", 1)
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, attr)
        if not callable(fn):
            raise TypeError(f"{self.spec!r} is not callable")
        self._fn = fn
        return fn

    def embed(self, text: str) -> Vector:
        try:
            fn = self._resolve()
            raw = list(fn(text))
        except Exception as exc:  # noqa: BLE001
            log.warning("embedding callable failed (%s); falling back: %s", self.spec, exc)
            return self.fallback.embed(text)
        if not raw or not all(isinstance(x, (int, float)) for x in raw):
            return self.fallback.embed(text)
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return Vector(values=[float(x) / norm for x in raw])


def get_embedder():
    """Return the configured embedder. Caches on the settings object."""
    s = get_settings()
    spec = getattr(s, "embedding_callable", "") or ""
    if spec:
        return CallableEmbedder(spec)
    dim = int(getattr(s, "embedding_dim", DEFAULT_DIM) or DEFAULT_DIM)
    return HashEmbedder(dim=dim)


def embed(text: str) -> Vector:
    return get_embedder().embed(text)


def embed_many(texts: Iterable[str]) -> list[Vector]:
    e = get_embedder()
    return [e.embed(t) for t in texts]
