# LabFlow — Product Spec (v0.1 MVP)

## One-line product
LabFlow is the meeting-to-execution operating system for research and technical teams: it turns calls, papers, and planning docs into structured experiments, tickets, code tasks, owners, deadlines, and verified evidence of completion.

## Why now
The meeting-assistant space is crowded but stops at summaries. Technical teams still spend hours translating conversations into structured work. Research and engineering meetings have a richer surface area (decisions, experiments, hypotheses, blockers, assumptions, evidence) that generic note-takers ignore — and that surface area is exactly what a vertical product can monetize.

## Target users (in order of go-to-market priority)
1. ML / AI research teams (research standups, experiment reviews)
2. Startup engineering teams (kickoffs, postmortems, planning)
3. Biotech / AI labs running many experiments per week
4. Hackathon and prototype-heavy teams

## Core product loops
1. **Ingest** transcript / notes / linked docs.
2. **Extract** decisions, experiments, blockers, owners, deadlines, assumptions into a typed schema.
3. **Review** in a human-in-the-loop UI; corrections are cheap and fast.
4. **Execute** by exporting to Markdown / JSON / task-list, or pushing to integrated tools (Phase 2+).
5. **Verify** by ingesting evidence (commits, eval results, docs, checklists) that closes tasks automatically when the score crosses a confidence threshold.
6. **Brief** weekly via an automated digest covering closed work, overdue tasks, high-uncertainty items, open blockers, and high-risk assumptions.

## What's in v0.1 (this MVP)
- Transcript upload (file or JSON body) + manual notes
- Deterministic, rule-based extraction with a clearly factored LLM swap-in point (`LABFLOW_LLM_PROVIDER`)
- Strict Pydantic schema for the extraction output, validated end-to-end
- SQLAlchemy persistence for meetings / decisions / tasks / experiments / assumptions / blockers / evidence / owners
- Cross-meeting decision continuity (`superseded_by_id`)
- Dependency linking between tasks (explicit + heuristic)
- Evidence ingestion + auto-completion verification
- Weekly digest (Markdown)
- Markdown + JSON exports per meeting
- Minimal HTML review UI + landing page
- 60+ unit and integration tests covering schema, dates, owners, uncertainty, dependencies, verification, digest, and full HTTP lifecycle

## What's deliberately out of v0.1
- Live transcription / speech ingestion (we accept text)
- Connectors to GitHub / Linear / Jira / Notion (we provide exports + an API)
- Multi-tenant auth (single-team mode is the MVP)
- Vector search over historical decisions (planned for Phase 2)

## Schema overview
See `labflow/models.py` and `labflow/schemas.py`. Highlights:
- `Meeting` owns `Decisions`, `Tasks`, `Experiments`, `Assumptions`, `Blockers`.
- `Task` references an `Owner`, has `due_date`, `status`, `kind`, `uncertainty`, and may `depends_on` other tasks.
- `Evidence` attaches to `Task`, has a `kind` ∈ {commit, eval, doc, checklist, link}, a free-form `summary`/`uri`, and a verification `score`.
- `Decision` rows link forward via `superseded_by_id` to form a long-running decision graph across meetings.

## Differentiators / moat
1. **Technical-team specificity** — vocabulary, kinds, and templates tuned for research/engineering, not generic SMB notes.
2. **Execution, not note-taking** — outputs are typed work items, not bullet summaries.
3. **Evidence-linked task closure** — verifiable completion is the integrity layer competitors lack.
4. **Decision graph over time** — the long-tail value compounds with usage.
5. **Strong domain adaptation** — accumulated labeled meeting-to-execution data becomes a defensible dataset.
