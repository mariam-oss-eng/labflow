"""Owner detection tests."""
from labflow.extraction.owners import (
    assign_owner,
    collect_unique_owners,
    find_owner_mentions,
)


def test_at_mention():
    assert assign_owner("@alice will retrain the model") == "alice"


def test_bracket_mention():
    assert assign_owner("[Alice] retrain the model") == "alice"


def test_capitalized_name_will():
    assert assign_owner("Alice will retrain the model") == "alice"


def test_no_mention():
    assert assign_owner("Retrain the model") is None


def test_multiple_mentions_picks_first():
    assert assign_owner("@alice will sync with @bob tomorrow") == "alice"


def test_handle_normalization():
    assert assign_owner("@Alice-Smith will do it") == "alicesmith"


def test_collect_unique_dedupes_across_text():
    text = "@alice will run training. Alice will also write the doc. @alice did the review."
    owners = collect_unique_owners(text)
    handles = [o.handle for o in owners]
    assert handles.count("alice") == 1


def test_find_mentions_returns_in_order():
    text = "@alice ... @bob ... [Carol]"
    mentions = find_owner_mentions(text)
    assert [m.handle for m in mentions] == ["alice", "bob", "carol"]


def test_no_false_positive_on_lowercase_word():
    # "we will" should not produce an owner
    assert assign_owner("we will train tomorrow") is None
