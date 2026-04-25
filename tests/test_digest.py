"""Weekly digest tests."""
from datetime import datetime, timedelta

from labflow import digest as digest_mod
from labflow import models, services
from labflow.extraction import extract


def test_digest_includes_recent_activity(session, default_team):
    text = "@alice will retrain the model tomorrow. We decided to use SentencePiece."
    meeting = models.Meeting(
        team_id=default_team.id,
        title="standup",
        transcript=text,
        occurred_at=datetime.utcnow() - timedelta(days=1),
    )
    session.add(meeting)
    session.flush()
    services.persist_extraction(session, meeting, extract(text))

    d = digest_mod.build_weekly_digest(session)
    assert meeting in d.meetings
    assert any("sentencepiece" in dec.statement.lower() for dec in d.new_decisions)
    md = d.to_markdown()
    assert "Weekly Digest" in md
    assert "Decisions" in md


def test_digest_overdue_surfacing(session, default_team):
    past = datetime.utcnow() - timedelta(days=3)
    meeting = models.Meeting(team_id=default_team.id, title="kickoff", occurred_at=past)
    session.add(meeting)
    session.flush()
    owner = models.Owner(team_id=default_team.id, handle="alice", display_name="Alice")
    session.add(owner)
    session.flush()
    overdue = models.Task(
        team_id=default_team.id,
        meeting=meeting,
        title="ship the API",
        owner=owner,
        due_date=datetime.utcnow() - timedelta(days=1),
        status="open",
    )
    session.add(overdue)
    session.flush()

    d = digest_mod.build_weekly_digest(session)
    assert overdue in d.overdue_tasks
    assert "Overdue" in d.to_markdown()


def test_digest_high_uncertainty_surfacing(session, default_team):
    meeting = models.Meeting(team_id=default_team.id, title="m")
    session.add(meeting)
    session.flush()
    t = models.Task(
        team_id=default_team.id,
        meeting=meeting,
        title="maybe try a new optimizer",
        uncertainty=0.7,
        status="open",
    )
    session.add(t)
    session.flush()

    d = digest_mod.build_weekly_digest(session)
    assert t in d.high_uncertainty_tasks
    assert "Needs review" in d.to_markdown()

