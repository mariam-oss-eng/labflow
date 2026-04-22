"""Dependency linking tests."""
from labflow.extraction.deps import link_dependencies, topological_levels
from labflow.schemas import ExtractedTask


def make_task(title, span=""):
    return ExtractedTask(title=title, source_span=span or title)


def test_explicit_depends_on():
    a = make_task("Set up the FAISS index")
    b = make_task(
        "Implement the retrieval API endpoint",
        span="Implement the retrieval API endpoint. Depends on the FAISS index.",
    )
    link_dependencies([a, b])
    assert a.depends_on_titles == []
    assert "Set up the FAISS index" in b.depends_on_titles


def test_blocked_by():
    a = make_task("Land the tokenizer change")
    b = make_task(
        "Run the dropout ablation",
        span="Run the dropout ablation. Blocked by the tokenizer change.",
    )
    link_dependencies([a, b])
    assert "Land the tokenizer change" in b.depends_on_titles


def test_no_link_when_unrelated():
    a = make_task("Buy office snacks")
    b = make_task(
        "Run the dropout ablation",
        span="Run the dropout ablation. Depends on cluster access.",
    )
    link_dependencies([a, b])
    assert b.depends_on_titles == []


def test_topological_levels_simple_chain():
    a = make_task("Task one")
    b = make_task("Task two", span="Task two depends on Task one")
    c = make_task("Task three", span="Task three depends on Task two")
    link_dependencies([a, b, c])
    levels = topological_levels([a, b, c])
    assert levels == [["Task one"], ["Task two"], ["Task three"]]


def test_topological_handles_cycle():
    a = make_task("Foo task")
    b = make_task("Bar task")
    a.depends_on_titles.append("Bar task")
    b.depends_on_titles.append("Foo task")
    levels = topological_levels([a, b])
    # Cycle is dumped in a final level rather than infinite-looping.
    assert any(set(level) == {"Foo task", "Bar task"} for level in levels)


def test_self_dependency_is_skipped():
    a = make_task("Foo bar baz", span="Foo bar baz depends on foo bar baz")
    link_dependencies([a])
    assert a.depends_on_titles == []
