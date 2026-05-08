"""Interactive read-eval-print loop for the LabFlow CLI (v0.13).

Run with ``python -m labflow.cli repl`` or ``labflow repl``. Built on
stdlib :mod:`cmd` so it works on every platform Python supports without
extra dependencies — ``readline`` (when available) gives history and
arrow-key navigation for free.

Supported commands:

* ``tasks``                  — list open tasks (most recent 20)
* ``task <id> show``         — show a single task
* ``task <id> done``         — mark a task as done (audited)
* ``meetings``               — list recent meetings
* ``decisions``              — list recent decisions
* ``ingest <path> <title>``  — create a meeting from a transcript file
* ``team [slug]``            — show / switch the active team
* ``help`` / ``?``           — list commands
* ``quit`` / ``exit`` / ``EOF`` — leave

The REPL reads / writes the same database as the API, so changes made
here are immediately visible everywhere else.
"""
from __future__ import annotations

import cmd
import io
import sys
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select


def _setup_session():
    from . import models  # noqa: F401
    from .auth import ensure_bootstrap_team
    from .db import get_session_factory, init_db
    init_db()
    SessionLocal = get_session_factory()
    with SessionLocal() as s:
        team = ensure_bootstrap_team(s)
        team_slug = team.slug
    return SessionLocal, team_slug


def _resolve_team(sess, slug: str):
    from . import models
    return sess.execute(
        select(models.Team).where(models.Team.slug == slug)
    ).scalar_one_or_none()


def _print_table(rows: Iterable[Iterable[Any]], headers: list[str], out) -> None:
    rows_l = [[str(c) if c is not None else "" for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in rows_l:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    line = " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    out.write(line + "\n")
    out.write("-+-".join("-" * w for w in widths) + "\n")
    for r in rows_l:
        out.write(" | ".join(c.ljust(widths[i]) for i, c in enumerate(r)) + "\n")
    if not rows_l:
        out.write("(no rows)\n")


class LabflowRepl(cmd.Cmd):
    intro = (
        "LabFlow REPL — type 'help' for commands, 'quit' to exit.\n"
        "Connected to the same database as the API server."
    )
    prompt = "labflow> "

    def __init__(self, *, stdin=None, stdout=None) -> None:
        super().__init__(stdin=stdin or sys.stdin, stdout=stdout or sys.stdout)
        self.SessionLocal, self.team_slug = _setup_session()
        self.use_rawinput = stdin is None  # tests inject stdin

    # ---- helpers -------------------------------------------------
    def _team(self, sess):
        team = _resolve_team(sess, self.team_slug)
        if team is None:
            self.stdout.write(f"team {self.team_slug!r} no longer exists\n")
        return team

    # ---- commands ------------------------------------------------
    def do_tasks(self, _arg: str) -> None:
        """List the 20 most recent open tasks."""
        from . import models
        with self.SessionLocal() as s:
            team = self._team(s)
            if team is None:
                return
            tasks = s.execute(
                select(models.Task)
                .where(models.Task.team_id == team.id, models.Task.status != "done")
                .order_by(models.Task.id.desc()).limit(20)
            ).scalars().all()
            _print_table(
                ((t.id, t.title[:60], t.status, t.priority or "", t.owner_id)
                 for t in tasks),
                ["id", "title", "status", "prio", "owner"],
                self.stdout,
            )

    def do_meetings(self, _arg: str) -> None:
        """List the 20 most recent meetings."""
        from . import models
        with self.SessionLocal() as s:
            team = self._team(s)
            if team is None:
                return
            ms = s.execute(
                select(models.Meeting)
                .where(models.Meeting.team_id == team.id)
                .order_by(models.Meeting.id.desc()).limit(20)
            ).scalars().all()
            _print_table(
                ((m.id, m.title[:60], m.meeting_type, str(m.finalized)) for m in ms),
                ["id", "title", "type", "final"],
                self.stdout,
            )

    def do_decisions(self, _arg: str) -> None:
        """List the 20 most recent decisions."""
        from . import models
        with self.SessionLocal() as s:
            team = self._team(s)
            if team is None:
                return
            ds = s.execute(
                select(models.Decision)
                .where(models.Decision.team_id == team.id)
                .order_by(models.Decision.id.desc()).limit(20)
            ).scalars().all()
            _print_table(
                ((d.id, d.statement[:80], f"{d.confidence:.2f}") for d in ds),
                ["id", "statement", "conf"],
                self.stdout,
            )

    def do_task(self, arg: str) -> None:
        """task <id> show|done — show or close a task."""
        parts = arg.split()
        if len(parts) < 2:
            self.stdout.write("usage: task <id> show|done\n")
            return
        try:
            tid = int(parts[0])
        except ValueError:
            self.stdout.write("task id must be an integer\n")
            return
        action = parts[1]
        from . import audit as audit_mod, models
        from .time_utils import now_utc
        with self.SessionLocal() as s:
            team = self._team(s)
            if team is None:
                return
            t = s.get(models.Task, tid)
            if t is None or t.team_id != team.id:
                self.stdout.write(f"task #{tid} not found\n")
                return
            if action == "show":
                self.stdout.write(
                    f"#{t.id} {t.title}\n"
                    f"  status={t.status}  state={t.state}  "
                    f"priority={t.priority}  owner={t.owner_id}\n"
                    f"  due={t.due_date}\n"
                )
            elif action == "done":
                if t.status == "done":
                    self.stdout.write(f"#{t.id} already done\n")
                    return
                prev = t.status
                t.status = "done"
                t.closed_at = now_utc().replace(tzinfo=None)
                audit_mod.record(
                    s, team_id=team.id, action="task.status_changed",
                    entity_type="task", entity_id=t.id, actor="repl",
                    metadata={"from": prev, "to": "done"},
                )
                s.commit()
                self.stdout.write(f"#{t.id} marked done\n")
            else:
                self.stdout.write(f"unknown task action {action!r}\n")

    def do_ingest(self, arg: str) -> None:
        """ingest <path> <title…> — create a meeting from a transcript file."""
        parts = arg.split(maxsplit=1)
        if len(parts) < 1:
            self.stdout.write("usage: ingest <path> [title]\n")
            return
        path = Path(parts[0])
        title = parts[1] if len(parts) > 1 else path.stem
        if not path.is_file():
            self.stdout.write(f"file not found: {path}\n")
            return
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            self.stdout.write("file is not utf-8 text\n")
            return
        from . import models as _models
        from .time_utils import now_utc as _now
        with self.SessionLocal() as s:
            team = self._team(s)
            if team is None:
                return
            m = _models.Meeting(
                team_id=team.id, title=title, meeting_type="standup",
                transcript=text, notes="",
                occurred_at=_now().replace(tzinfo=None),
            )
            s.add(m)
            s.commit()
            self.stdout.write(f"created meeting #{m.id} ({title!r})\n")

    def do_team(self, arg: str) -> None:
        """team [slug] — show or switch the active team."""
        slug = arg.strip()
        if not slug:
            self.stdout.write(f"active team: {self.team_slug}\n")
            return
        with self.SessionLocal() as s:
            t = _resolve_team(s, slug)
            if t is None:
                self.stdout.write(f"team {slug!r} not found\n")
                return
        self.team_slug = slug
        self.stdout.write(f"switched to team {slug!r}\n")

    # ---- meta ----------------------------------------------------
    def do_quit(self, _arg: str) -> bool:
        return True

    def do_exit(self, _arg: str) -> bool:
        return True

    def do_EOF(self, _arg: str) -> bool:  # noqa: N802 (cmd convention)
        self.stdout.write("\n")
        return True

    def emptyline(self) -> bool:  # don't repeat last command
        return False


def run(argv: list[str] | None = None) -> int:
    LabflowRepl().cmdloop()
    return 0


def run_script(commands: list[str]) -> str:
    """Execute a list of REPL commands and return collected output.

    Used by tests to drive the REPL non-interactively.
    """
    out = io.StringIO()
    inp = io.StringIO("\n".join(commands) + "\nquit\n")
    repl = LabflowRepl(stdin=inp, stdout=out)
    repl.cmdloop(intro="")
    return out.getvalue()


if __name__ == "__main__":
    raise SystemExit(run())
