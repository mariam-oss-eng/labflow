"""LabFlow command-line interface.

Subcommands:

  * ``serve``                — run the API server
  * ``worker``               — run the background job worker
  * ``migrate``              — run alembic upgrade head
  * ``team create <slug>``   — create a team
  * ``keys create <slug>``   — mint an API key (plaintext printed once)
  * ``keys revoke <id>``     — revoke an API key
  * ``ingest <file>``        — upload a transcript to a team
  * ``digest [--team slug]`` — print the weekly digest

Implemented with stdlib ``argparse`` so the CLI doesn't add a runtime
dependency on Click/Typer.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select


def _bootstrap_db():
    from . import models  # noqa: F401  (registers tables)
    from .auth import ensure_bootstrap_team
    from .db import get_session_factory, init_db

    init_db()
    SessionLocal = get_session_factory()
    with SessionLocal() as s:
        ensure_bootstrap_team(s)
    return SessionLocal


# --- subcommands -----------------------------------------------------------
def _cmd_serve(args) -> int:
    import uvicorn
    uvicorn.run("labflow.main:app", host=args.host, port=args.port,
                reload=args.reload, workers=args.workers)
    return 0


def _cmd_worker(args) -> int:
    from . import jobs
    _bootstrap_db()
    jobs.run_forever(idle_sleep_s=args.idle_sleep)
    return 0


def _cmd_migrate(args) -> int:
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    return 0


def _cmd_team_create(args) -> int:
    from . import models
    from .time_utils import now_utc

    SessionLocal = _bootstrap_db()
    with SessionLocal() as s:
        existing = s.execute(
            select(models.Team).where(models.Team.slug == args.slug)
        ).scalar_one_or_none()
        if existing:
            print(f"team {args.slug!r} already exists (id={existing.id})", file=sys.stderr)
            return 1
        team = models.Team(slug=args.slug, name=args.name or args.slug.title(),
                           created_at=now_utc())
        s.add(team)
        s.commit()
        print(f"created team id={team.id} slug={team.slug}")
    return 0


def _cmd_keys_create(args) -> int:
    from . import models
    from .auth import generate_api_key, hash_api_key
    from .time_utils import now_utc

    SessionLocal = _bootstrap_db()
    with SessionLocal() as s:
        team = s.execute(
            select(models.Team).where(models.Team.slug == args.team)
        ).scalar_one_or_none()
        if team is None:
            print(f"team {args.team!r} not found", file=sys.stderr)
            return 1
        plaintext = generate_api_key()
        key = models.ApiKey(team_id=team.id, name=args.name,
                            key_hash=hash_api_key(plaintext), created_at=now_utc())
        s.add(key)
        s.commit()
        # Print the plaintext key to stdout exactly once. Operators are
        # expected to capture it from the terminal (or pipe it into their
        # secret manager). It is intentionally NOT routed through the
        # logging framework — that would risk it being shipped to a log
        # aggregator. CodeQL's py/clear-text-logging-sensitive-data rule
        # flags this print as sensitive data, but printing the key once
        # to the operator's TTY is the documented secure handoff.
        sys.stdout.write("API key created. Save this — it will not be shown again:\n")
        sys.stdout.write(plaintext + "\n")
        sys.stdout.flush()
    return 0


def _cmd_keys_revoke(args) -> int:
    from . import models
    from .time_utils import now_utc

    SessionLocal = _bootstrap_db()
    with SessionLocal() as s:
        key = s.get(models.ApiKey, args.id)
        if key is None:
            print(f"key id={args.id} not found", file=sys.stderr)
            return 1
        key.revoked_at = now_utc()
        s.commit()
        print(f"revoked key id={args.id}")
    return 0


def _cmd_ingest(args) -> int:
    from . import models, services
    from .extraction import extract
    from .time_utils import now_utc

    text = Path(args.path).read_text(encoding="utf-8")
    SessionLocal = _bootstrap_db()
    with SessionLocal() as s:
        team = s.execute(
            select(models.Team).where(models.Team.slug == args.team)
        ).scalar_one_or_none()
        if team is None:
            print(f"team {args.team!r} not found", file=sys.stderr)
            return 1
        meeting = models.Meeting(
            team_id=team.id, title=args.title, meeting_type=args.type,
            transcript=text, occurred_at=now_utc().replace(tzinfo=None),
        )
        s.add(meeting)
        s.flush()
        services.persist_extraction(s, meeting, extract(text))
        s.commit()
        print(f"created meeting id={meeting.id} "
              f"tasks={len(meeting.tasks)} decisions={len(meeting.decisions)}")
    return 0


def _cmd_digest(args) -> int:
    from . import digest as digest_mod
    from . import models

    SessionLocal = _bootstrap_db()
    with SessionLocal() as s:
        team_id = None
        if args.team:
            team = s.execute(
                select(models.Team).where(models.Team.slug == args.team)
            ).scalar_one_or_none()
            if team is None:
                print(f"team {args.team!r} not found", file=sys.stderr)
                return 1
            team_id = team.id
        d = digest_mod.build_weekly_digest(s, team_id=team_id)
        print(d.to_markdown())
    return 0


# --- entrypoint ------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="labflow",
                                description="LabFlow command-line interface")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="run the API server")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--reload", action="store_true")
    sp.add_argument("--workers", type=int, default=1)
    sp.set_defaults(func=_cmd_serve)

    sp = sub.add_parser("worker", help="run the background job worker")
    sp.add_argument("--idle-sleep", type=float, default=1.0)
    sp.set_defaults(func=_cmd_worker)

    sp = sub.add_parser("migrate", help="run alembic upgrade head")
    sp.set_defaults(func=_cmd_migrate)

    sp = sub.add_parser("team", help="team management")
    team_sub = sp.add_subparsers(dest="team_cmd", required=True)
    cp = team_sub.add_parser("create")
    cp.add_argument("slug")
    cp.add_argument("--name")
    cp.set_defaults(func=_cmd_team_create)

    sp = sub.add_parser("keys", help="API key management")
    keys_sub = sp.add_subparsers(dest="keys_cmd", required=True)
    cp = keys_sub.add_parser("create")
    cp.add_argument("team", help="team slug")
    cp.add_argument("--name", default="default")
    cp.set_defaults(func=_cmd_keys_create)
    cp = keys_sub.add_parser("revoke")
    cp.add_argument("id", type=int)
    cp.set_defaults(func=_cmd_keys_revoke)

    sp = sub.add_parser("ingest", help="upload a transcript file")
    sp.add_argument("path")
    sp.add_argument("--team", default="default")
    sp.add_argument("--title", default="ingested transcript")
    sp.add_argument("--type", default="standup")
    sp.set_defaults(func=_cmd_ingest)

    sp = sub.add_parser("digest", help="print the weekly digest")
    sp.add_argument("--team")
    sp.set_defaults(func=_cmd_digest)

    sp = sub.add_parser("repl", help="interactive REPL (v0.13)")
    sp.set_defaults(func=lambda _a: __import__("labflow.repl", fromlist=["run"]).run())

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
