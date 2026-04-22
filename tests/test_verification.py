"""Verification engine tests."""
from labflow import models, services, verification
from labflow.extraction import extract


def _seed_meeting(session, transcript: str):
    meeting = models.Meeting(title="t", transcript=transcript)
    session.add(meeting)
    session.flush()
    services.persist_extraction(session, meeting, extract(transcript))
    return meeting


def test_commit_evidence_closes_matching_task(session):
    m = _seed_meeting(
        session,
        "@alice will retrain the tokenizer model tomorrow.",
    )
    task = m.tasks[0]
    assert task.status == "open"

    ev = models.Evidence(
        task=task,
        kind="commit",
        uri="https://github.com/lab/repo/commit/abc",
        summary="alice: retrain tokenizer model",
    )
    session.add(ev)
    session.flush()
    closed = verification.verify_evidence(session, ev)
    assert closed is True
    assert ev.verified is True
    assert task.status == "done"
    assert task.closed_at is not None


def test_unrelated_evidence_does_not_close(session):
    m = _seed_meeting(
        session, "@alice will retrain the tokenizer model tomorrow."
    )
    task = m.tasks[0]
    ev = models.Evidence(
        task=task,
        kind="link",
        uri="https://example.com/coffee",
        summary="latte art tutorial",
    )
    session.add(ev)
    session.flush()
    closed = verification.verify_evidence(session, ev)
    assert closed is False
    assert ev.verified is False
    assert task.status == "open"


def test_owner_handle_in_evidence_boosts_score(session):
    m = _seed_meeting(session, "@alice will refactor the ingest service.")
    task = m.tasks[0]
    ev = models.Evidence(
        task=task,
        kind="commit",
        uri="https://github.com/lab/repo/pull/42",
        summary="alice refactor ingest service",
    )
    session.add(ev)
    session.flush()
    score = verification.score_evidence(task, ev)
    assert score >= 0.5
