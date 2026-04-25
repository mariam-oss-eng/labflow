# Contributing to LabFlow

Thanks for considering a contribution! LabFlow tries to stay small,
honest, and dependency-light — that goal informs how we review
patches.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
pytest          # 128 tests, ~6s
```

## Local commands

| Command | What it does |
| --- | --- |
| `labflow serve` | Start the API on http://localhost:8000 |
| `labflow worker` | Run the in-process job worker |
| `pytest` | Run the full test suite |
| `pytest -k v04` | Run a subset by keyword |
| `mkdocs serve -f docs/mkdocs.yml` | Preview the docs site |

## Conventions

* **Surgical changes only.** Every PR should map to a single ticket or
  intent. If you're tempted to "tidy up while you're in there",
  open a separate PR.
* **Tests are mandatory.** New behaviour ships with a test; bug fixes
  ship with a regression test. We currently have zero skipped tests
  and that's the bar.
* **No new dependencies without a one-line justification.** Standing
  goal: stay buildable on `python -m pip install -e .[dev]` with no
  Node tooling and no compiled extensions.
* **ADRs for big calls.** If the change introduces new operational
  surface (a new middleware, a new storage tier, a new external
  integration), add an ADR under `docs/adr/`.

## PR checklist

- [ ] Tests pass locally (`pytest`).
- [ ] Lint passes (`ruff check labflow tests`).
- [ ] Schema changes have an Alembic migration.
- [ ] Updated `CHANGELOG.md`.
- [ ] Updated `docs/` if user-facing behaviour changed.

## Release process

1. Bump `__version__` in `labflow/__init__.py`, the FastAPI version
   field in `main.py`, and the CHANGELOG.
2. Tag the commit (`git tag v0.X.0 && git push --tags`).
3. The `pages.yml` workflow rebuilds the docs site automatically.

## Code of conduct

Treat each other with respect. We use the
[Contributor Covenant](https://www.contributor-covenant.org/). Report
violations privately to the maintainers.
