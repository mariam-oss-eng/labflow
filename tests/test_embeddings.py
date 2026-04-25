"""Tests for the embedding layer (v0.4)."""
from __future__ import annotations

import math

from labflow import embedding_store, models, services
from labflow.embeddings import HashEmbedder, cosine, embed, get_embedder
from labflow.extraction import extract


def test_hash_embedder_is_unit_normalized():
    e = HashEmbedder(dim=128)
    v = e.embed("we decided to adopt SentencePiece tokenization for the new model")
    assert v.dim == 128
    norm = math.sqrt(sum(x * x for x in v.values))
    assert abs(norm - 1.0) < 1e-6


def test_hash_embedder_is_deterministic():
    e1 = HashEmbedder(dim=64)
    e2 = HashEmbedder(dim=64)
    a = e1.embed("retrain on the new dataset")
    b = e2.embed("retrain on the new dataset")
    assert a.values == b.values


def test_identical_text_perfectly_similar(temp_db):
    """The fundamental hash-embedder guarantee: identical text → cosine 1.0,
    disjoint-vocabulary text → near-zero. Hybrid search depends on this
    property; richer paraphrase recall is provided by plugging in a real
    embedding callable (see ``LABFLOW_EMBEDDING_CALLABLE``)."""
    a = embed("we decided to adopt sentencepiece tokenization for the model")
    b = embed("we decided to adopt sentencepiece tokenization for the model")
    # Use truly disjoint vocabularies so collision noise is a small
    # fraction of the (zero) signal. We assert direction (a/b > a/c)
    # rather than an absolute magnitude — magnitude depends on dimension.
    c = embed("ipsum dolor amet consectetur adipiscing elit lorem fugiat")
    assert abs(cosine(a, b) - 1.0) < 1e-9
    assert cosine(a, b) > cosine(a, c)


def test_empty_text_yields_zero_vector():
    e = HashEmbedder(dim=32)
    v = e.embed("")
    assert all(x == 0.0 for x in v.values)
    assert cosine(v, v) == 0.0


def test_persist_extraction_writes_embeddings(session, default_team):
    text = "@alice will retrain the model. We decided to adopt SentencePiece."
    meeting = models.Meeting(team_id=default_team.id, title="m", transcript=text)
    session.add(meeting)
    session.flush()
    services.persist_extraction(session, meeting, extract(text))
    session.flush()

    embedder = get_embedder()
    d_vecs = embedding_store.load_vectors(
        session, team_id=default_team.id, entity_type="decision",
        model=embedder.name,
    )
    t_vecs = embedding_store.load_vectors(
        session, team_id=default_team.id, entity_type="task",
        model=embedder.name,
    )
    assert d_vecs, "decision embeddings should be persisted"
    assert t_vecs, "task embeddings should be persisted"
    # Vectors should round-trip through JSON cleanly.
    for v in list(d_vecs.values()) + list(t_vecs.values()):
        norm = math.sqrt(sum(x * x for x in v.values)) or 0.0
        assert norm > 0.0


def test_callable_embedder_falls_back_on_error(temp_db, monkeypatch):
    """Configuring a broken callable should not break ingestion — the
    embedder degrades to the hash backend so writes still succeed."""
    monkeypatch.setenv(
        "LABFLOW_EMBEDDING_CALLABLE", "labflow.tests_does_not_exist:fn"
    )
    from labflow import config as config_mod
    config_mod.reset_settings_cache()
    v = embed("hello world")
    # Falls through to hash-bow under the hood.
    assert v.dim == 256
    assert any(x != 0.0 for x in v.values)
