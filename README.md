# LabFlow

**Meeting-to-execution operating system for research and technical teams.**

LabFlow turns calls, papers, and planning docs into structured experiments,
tickets, code tasks, owners, deadlines, and verified evidence of completion.
It is built for ML research groups, startup engineering teams, biotech / AI
labs, and prototype-heavy hackathon teams — not for generic SMB note-taking.

> Most meeting assistants stop at summaries. LabFlow ships an **execution
> structure plus verification layer** on top: typed tasks, dependency graph,
> evidence-driven completion, and a long-lived decision graph.

---

## Quickstart

```bash
pip install -r requirements.txt
uvicorn labflow.main:app --reload
```

Open <http://localhost:8000> for the landing page, <http://localhost:8000/app>
for the dashboard, and <http://localhost:8000/docs> for the auto-generated
OpenAPI explorer.

A SQLite database is created in the working directory by default. Point
`LABFLOW_DATABASE_URL` at Postgres for production
(e.g. `postgresql+psycopg://user:pass@host/labflow`).

## Try it on the demo transcripts

```bash
curl -F "title=Research standup" -F "meeting_type=standup" \
     -F "transcript_file=@demo/research_standup.txt" \
     http://localhost:8000/api/meetings/upload
```

Then visit `/meetings/{id}/review` to inspect the structured output, and
`GET /api/meetings/{id}/export.md` for a clean Markdown export.

## What it extracts

| Entity | What we capture |
|---|---|
| **Decision** | statement, rationale, confidence, supersession across meetings |
| **Task** | title, owner, deadline, kind (task / code / experiment / review), uncertainty, dependencies |
| **Experiment** | name, hypothesis, method, metrics, dataset, owner |
| **Assumption** | statement, risk level (low / medium / high) |
| **Blocker** | description, optional blocked task |
| **Evidence** | commit / eval / doc / checklist / link, scored against the task |

The full schema lives in [`labflow/schemas.py`](labflow/schemas.py) and
[`labflow/models.py`](labflow/models.py).

## How extraction works

The default extractor is **deterministic and rule-based**, which means tests
are reproducible and the system runs offline. The pipeline is in
`labflow/extraction/`:

- `dates.py` — relative + absolute deadline parsing
- `owners.py` — `@handle`, `[Name]`, and `Name will…` detection
- `uncertainty.py` — hedging-based 0..1 score and assumption classification
- `deps.py` — explicit + heuristic task dependency linking
- `rules.py` — decision / task / experiment / assumption / blocker rules
- `pipeline.py` — orchestration, validates output against `ExtractionResult`

To plug an LLM backend, set `LABFLOW_LLM_PROVIDER` and replace
`extract()` with a function that returns a validated
`labflow.schemas.ExtractionResult`. The strict Pydantic schema (`extra="forbid"`)
will catch hallucinations or schema drift early.

## Verification

`POST /api/evidence` attaches an evidence item to a task. The verifier
combines token overlap (evidence vs. task title), kind-specific bonuses,
and owner-handle matches to produce a 0..1 score. Crossing the threshold
marks the evidence verified and the task `done`. Below threshold the
evidence is still stored — for review, not silent dismissal.

## Weekly digest

`GET /api/digest/weekly` returns a Markdown brief covering the last 7 days:
meetings, new decisions, closed tasks, overdue tasks, high-uncertainty work
that needs review, open blockers, and high-risk assumptions.

## Tests

```bash
pip install -r requirements.txt pytest httpx
pytest
```

Coverage:

- **Unit:** schema validation, date parsing, owner assignment, uncertainty
  classification, dependency linking
- **Integration:** upload → extract → review → finalize → export → digest;
  evidence-driven task closure; multi-meeting decision continuity; HTML
  pages render

## Repo layout

```
labflow/
├── labflow/                # application package
│   ├── extraction/         # rule-based extractors + pipeline
│   ├── web/                # Jinja templates + static assets
│   ├── models.py           # SQLAlchemy ORM
│   ├── schemas.py          # Pydantic I/O + extraction schema
│   ├── services.py         # persistence / extraction → DB
│   ├── verification.py     # evidence scoring + auto-close
│   ├── digest.py           # weekly digest generation
│   ├── exports.py          # Markdown / JSON exports
│   └── main.py             # FastAPI app + routes
├── demo/                   # sample transcripts (research, kickoff, exp review)
├── docs/                   # product spec, pricing memo, onboarding flow
└── tests/                  # unit + integration tests
```

## Roadmap

- **Phase 1 (this MVP):** transcript upload, extraction, review, export,
  weekly digest, evidence ingestion + auto-completion.
- **Phase 2:** GitHub / Linear / Notion connectors, vector search over the
  decision graph, cross-meeting continuity UI, eval-result ingestion.
- **Phase 3:** custom templates per team, hosted LLM extraction with
  fine-tuned domain adapters.

See [`docs/product_spec.md`](docs/product_spec.md),
[`docs/pricing.md`](docs/pricing.md), and
[`docs/onboarding.md`](docs/onboarding.md) for the product brief, pricing,
and pilot flow.

## License

See [`LICENSE`](LICENSE).
