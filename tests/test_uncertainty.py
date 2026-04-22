"""Uncertainty classification tests."""
from labflow.extraction.uncertainty import (
    assumption_risk,
    is_assumption,
    score_uncertainty,
)


def test_certain_sentence_low_score():
    assert score_uncertainty("We will ship on Friday.") < 0.2


def test_hedged_sentence_high_score():
    s = score_uncertainty("Maybe we should try this, I'm not sure if it works.")
    assert s >= 0.5


def test_tbd_is_uncertain():
    assert score_uncertainty("Owner: TBD") >= 0.5


def test_definitely_reduces_score():
    s_with = score_uncertainty("We definitely will ship Friday.")
    s_without = score_uncertainty("We might ship Friday.")
    assert s_with < s_without


def test_score_is_bounded():
    s = score_uncertainty("maybe perhaps probably might could possibly TBD assuming uncertain")
    assert 0.0 <= s <= 1.0


def test_is_assumption_positive():
    assert is_assumption("Assuming the GPU is available next week.")
    assert is_assumption("We assume the data is clean.")
    assert is_assumption("Assumption: latency stays under 100ms.")


def test_is_assumption_negative():
    assert not is_assumption("We will ship Friday.")
    assert not is_assumption("")


def test_assumption_risk_buckets():
    assert assumption_risk("We assume something") == "low"
    assert assumption_risk("Maybe assuming the cluster works") in {"medium", "high"}
    assert assumption_risk("TBD, assuming everything, not sure if it works") == "high"
