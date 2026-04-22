"""Schema validation tests."""
import pytest
from pydantic import ValidationError

from labflow.schemas import (
    ExtractedAssumption,
    ExtractedDecision,
    ExtractedTask,
    ExtractionResult,
)


def test_task_title_strip_and_dedot():
    t = ExtractedTask(title="  retrain the model.  ")
    assert t.title == "retrain the model"


def test_task_uncertainty_bounds():
    with pytest.raises(ValidationError):
        ExtractedTask(title="foo", uncertainty=1.5)


def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        ExtractedDecision(statement="x", rationale=None, confidence=0.5, extra="nope")


def test_assumption_default_risk():
    a = ExtractedAssumption(statement="we have GPUs")
    assert a.risk == "medium"


def test_extraction_result_roundtrip():
    payload = {
        "owners": [{"handle": "alice", "display_name": "Alice"}],
        "decisions": [{"statement": "use sentencepiece"}],
        "tasks": [{"title": "retrain model"}],
        "experiments": [],
        "assumptions": [],
        "blockers": [],
    }
    res = ExtractionResult.model_validate(payload)
    assert res.tasks[0].title == "retrain model"
    assert res.owners[0].handle == "alice"
