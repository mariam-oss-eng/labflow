"""End-to-end tests of the extraction pipeline."""
from datetime import datetime
from pathlib import Path

from labflow.extraction import extract


REF = datetime(2026, 4, 22, 10, 0)


def test_research_standup_extraction():
    text = Path("demo/research_standup.txt").read_text()
    res = extract(text, reference=REF)

    handles = {o.handle for o in res.owners}
    assert {"alice", "bob", "carol"}.issubset(handles)

    decisions = [d.statement.lower() for d in res.decisions]
    assert any("sentencepiece" in d for d in decisions)
    assert any("accuracy" in d for d in decisions)

    titles = [t.title.lower() for t in res.tasks]
    assert any("dataset card" in t for t in titles)
    assert any("ablation" in t for t in titles)

    # Owner assignment
    by_title = {t.title.lower(): t for t in res.tasks}
    ablation_task = next((t for t in res.tasks if "ablation" in t.title.lower()), None)
    assert ablation_task is not None and ablation_task.owner_handle == "carol"

    # Deadline parsing — Friday from Wednesday is +2 days
    assert ablation_task.due_date == datetime(2026, 4, 24, 17, 0)

    # Experiment captured with metrics
    assert any("perplexity" in (e.method or "").lower() or
               "perplexity" in (e.hypothesis or "").lower() or
               "perplexity" in (e.metrics or [])
               for e in res.experiments)

    # Assumptions captured
    assert any("4-gpu" in a.statement.lower() or "gpu" in a.statement.lower()
               for a in res.assumptions)

    # Blockers captured
    assert any("eval cluster" in b.description.lower() or
               "infra" in b.description.lower()
               for b in res.blockers)

    # Uncertainty surfaced
    assert any(t.uncertainty >= 0.3 for t in res.tasks)


def test_kickoff_dependency_link():
    text = Path("demo/kickoff.txt").read_text()
    res = extract(text, reference=REF)

    # "Implement the retrieval API endpoint" should depend on the FAISS task.
    api_task = next(
        (t for t in res.tasks if "retrieval api" in t.title.lower()), None
    )
    assert api_task is not None
    assert any("faiss" in dep.lower() for dep in api_task.depends_on_titles)


def test_experiment_review_decision():
    text = Path("demo/experiment_review.txt").read_text()
    res = extract(text, reference=REF)
    statements = " ".join(d.statement.lower() for d in res.decisions)
    assert "0.05" in statements


def test_empty_input_returns_empty():
    res = extract("", reference=REF)
    assert res.tasks == []
    assert res.decisions == []


def test_extraction_validates_against_schema():
    # Round-tripping through model_dump_json must not raise.
    text = Path("demo/research_standup.txt").read_text()
    res = extract(text, reference=REF)
    js = res.model_dump_json()
    assert "tasks" in js
