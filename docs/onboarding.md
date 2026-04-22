# Pilot Onboarding Flow

Designed for a single research or engineering team to get value within their first week.

## Day 0 — install (15 min)
1. `pip install -r requirements.txt`
2. `uvicorn labflow.main:app --reload`
3. Open `http://localhost:8000` and click **Open dashboard**.

## Day 1 — first meeting (5 min)
1. Run a normal standup.
2. Paste / upload the transcript on the dashboard, set type = `standup`.
3. Open the review page and correct any misattributed owners or wrong deadlines (usually <30s of edits).
4. Click **Finalize**.

## Day 2 — wire up evidence (10 min)
1. Pipe your CI / commit webhooks to `POST /api/evidence` with `kind=commit` and a short summary including the task title.
2. Watch tasks auto-close as commits land.

## Day 3-4 — second meeting and decision continuity
1. Ingest a second meeting that revisits a prior decision.
2. Confirm the prior decision is marked as superseded in the JSON export.

## End of week — first digest
1. `GET /api/digest/weekly` — Markdown digest covering meetings, decisions, closures, overdue, and risks.
2. Pipe to the team Slack channel as a recurring job.

## Success criteria
- A reviewer spends <2 minutes per meeting on corrections.
- ≥60% of action items get evidence-linked to a commit / doc / eval within their deadline window.
- The team's standup notes channel is replaced by the LabFlow review link.
