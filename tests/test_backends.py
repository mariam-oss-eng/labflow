"""Tests for the pluggable extractor backends."""
from __future__ import annotations

import json

from labflow.extraction import extract
from labflow.extraction.backends import LLMBackend, RulesBackend, get_backend


def test_default_backend_is_rules():
    b = get_backend()
    assert isinstance(b, RulesBackend)


def test_rules_backend_deterministic():
    text = "@alice will retrain the model tomorrow. We decided to use SentencePiece."
    a = RulesBackend().extract(text)
    b = RulesBackend().extract(text)
    assert a.model_dump() == b.model_dump()


def test_explicit_rules_via_facade(monkeypatch):
    monkeypatch.setenv("LABFLOW_EXTRACTION_BACKEND", "rules")
    from labflow import config as config_mod
    config_mod.reset_settings_cache()
    r = extract("@alice will retrain the model tomorrow.")
    assert any(t.title for t in r.tasks)


def test_llm_backend_falls_back_to_rules_when_unconfigured():
    """No LABFLOW_LLM_CALLABLE set → LLM backend silently falls back to rules."""
    backend = LLMBackend()
    r = backend.extract("@alice will retrain the model tomorrow.")
    assert any(t.title for t in r.tasks)


def test_llm_backend_falls_back_when_callable_raises(monkeypatch):
    def boom(prompt, *, system=None):
        raise RuntimeError("model unreachable")
    import sys, types
    mod = types.ModuleType("fake_llm")
    mod.complete = boom  # type: ignore[attr-defined]
    sys.modules["fake_llm"] = mod
    backend = LLMBackend(callable_spec="fake_llm:complete")
    r = backend.extract("@alice will retrain the model tomorrow.")
    assert any(t.title for t in r.tasks)


def test_llm_backend_falls_back_on_invalid_json(monkeypatch):
    def bad(prompt, *, system=None):
        return "not json at all"
    import sys, types
    mod = types.ModuleType("fake_llm2")
    mod.complete = bad  # type: ignore[attr-defined]
    sys.modules["fake_llm2"] = mod
    backend = LLMBackend(callable_spec="fake_llm2:complete")
    r = backend.extract("@alice will retrain the model tomorrow.")
    assert any(t.title for t in r.tasks)


def test_llm_backend_uses_valid_response():
    payload = json.dumps({
        "owners": [{"handle": "zed", "display_name": "Zed"}],
        "decisions": [{"statement": "Adopt llm-extracted plan", "rationale": None,
                       "confidence": 0.9}],
        "tasks": [{"title": "Run llm task", "description": None,
                   "owner_handle": "zed", "due_date": None, "kind": "task",
                   "uncertainty": 0.1, "confidence": 0.9,
                   "depends_on_titles": [], "source_span": None}],
        "experiments": [], "assumptions": [], "blockers": [],
    })

    def fake(prompt, *, system=None):
        return payload

    import sys, types
    mod = types.ModuleType("fake_llm3")
    mod.complete = fake  # type: ignore[attr-defined]
    sys.modules["fake_llm3"] = mod
    backend = LLMBackend(callable_spec="fake_llm3:complete")
    r = backend.extract("anything")
    assert [t.title for t in r.tasks] == ["Run llm task"]
    assert r.owners[0].handle == "zed"
