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

### Local (SQLite)

```bash
pip install -r requirements.txt
alembic upgrade head        # create tables (or use init_db() in dev)
uvicorn labflow.main:app --reload
```

Open <http://localhost:8000> for the landing page, <http://localhost:8000/app>
for the dashboard, and <http://localhost:8000/docs> for the auto-generated
OpenAPI explorer.

### Docker (Postgres + API)

```bash
cp .env.example .env        # then edit LABFLOW_BOOTSTRAP_API_KEY
docker compose up --build
```

A SQLite database is created in the working directory by default. Point
`LABFLOW_DATABASE_URL` at Postgres for production
(e.g. `postgresql+psycopg://user:pass@host/labflow`). All settings are
documented in [`.env.example`](.env.example).

## Authentication

LabFlow ships with API-key authentication and per-team isolation. Every
row carries a `team_id` and is invisible to other teams.

| Mode | When | How |
|---|---|---|
| **Single-team** (default) | Local dev, demos | `LABFLOW_AUTH_ENABLED=false` — every request resolves to the bootstrap team |
| **Multi-tenant** (production) | Always | `LABFLOW_AUTH_ENABLED=true` — send `Authorization: Bearer lfk_…` or `X-LabFlow-Key: lfk_…` on every API call |

Mint keys via the CLI:

```bash
labflow team create acme
labflow keys create acme --name "ci-bot"   # prints plaintext exactly once
labflow keys revoke <key-id>
```

API keys are stored as SHA-256 hashes — the plaintext is shown only at
creation time.

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

To plug an LLM backend, set `LABFLOW_EXTRACTION_BACKEND=llm` and
`LABFLOW_LLM_CALLABLE=mypkg.module:complete` to a callable that returns
JSON conforming to `ExtractionResult`. The strict Pydantic schema
(`extra="forbid"`) will catch hallucinations or schema drift early; on
any error (network failure, bad JSON, validation error) the LLM backend
falls back to the rules pipeline so ingestion never breaks.

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

- **0.1 (MVP):** transcript upload, extraction, review, export, weekly
  digest, evidence ingestion + auto-completion.
- **0.2 (production foundations):** API-key auth + per-team isolation,
  typed config, structured logs, error envelope, pagination, pluggable
  extractor backends, Alembic migrations, Docker, CI.
- **0.3 (production scale):** background job queue, async extraction,
  audit log, outbound webhooks, GitHub inbound webhook, search,
  Prometheus metrics, CLI, operations docs.

See [`CHANGELOG.md`](CHANGELOG.md) for the detailed change log and
[`docs/operations.md`](docs/operations.md) for production deployment
notes.

See [`docs/product_spec.md`](docs/product_spec.md),
[`docs/pricing.md`](docs/pricing.md), and
[`docs/onboarding.md`](docs/onboarding.md) for the product brief, pricing,
and pilot flow.

## License

See [`LICENSE`](LICENSE).
