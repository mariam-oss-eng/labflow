"""Terminal user interface — ``labflow tui`` (v0.17).

A zero-dependency, ANSI-rendered dashboard that prints to the
terminal. Designed for ops use (`labflow tui` over SSH, in a tmux pane,
or on a Raspberry Pi-style display): no curses, no third-party libs,
nothing that breaks when a dumb terminal is attached.

The renderer pulls a snapshot from the live database via the standard
LabFlow modules (``analytics``, ``time_tracking``, ``webhook_dlq``,
``heatmap``) and prints:

* team header + version
* counts (open / in-progress / blocked / done last 7d)
* top 5 owners by open task count
* webhook DLQ stats
* a tiny ASCII heatmap for the last 12 weeks

The ``run`` entry point loops until the user hits ``q`` (default poll
1s; ``--once`` mode prints a single snapshot and exits — the form used
by tests and by people piping the output into another tool).
"""
from __future__ import annotations

import os
import select as select_mod
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import IO

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import __version__, heatmap, models, webhook_dlq

_CLEAR = "\x1b[2J\x1b[H"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_RESET = "\x1b[0m"
_FG = {
    "blue":   "\x1b[34m",
    "green":  "\x1b[32m",
    "yellow": "\x1b[33m",
    "red":    "\x1b[31m",
    "cyan":   "\x1b[36m",
    "grey":   "\x1b[90m",
}
_HEAT = ["·", "░", "▒", "▓", "█"]


def _stats(sess: Session, *, team_id: int) -> dict[str, int]:
    rows = list(sess.execute(
        select(models.Task.status).where(models.Task.team_id == team_id)
    ).all())
    out = Counter(r[0] for r in rows)
    return {
        "open":        out.get("open", 0),
        "in_progress": out.get("in_progress", 0),
        "blocked":     out.get("blocked", 0),
        "done":        out.get("done", 0),
    }


def _top_owners(sess: Session, *, team_id: int, n: int = 5) -> list[tuple[str, int]]:
    rows = list(sess.execute(
        select(models.Owner.handle, models.Task.id)
        .join(models.Task, models.Task.owner_id == models.Owner.id)
        .where(
            models.Task.team_id == team_id,
            models.Task.status.in_(("open", "in_progress")),
        )
    ).all())
    c = Counter(handle for handle, _ in rows)
    return c.most_common(n)


def _heat_strip(counts: dict[str, int], weeks: int = 12) -> str:
    today = datetime.utcnow().date()
    start = today - timedelta(days=weeks * 7 - 1)
    cells: list[str] = []
    for i in range(weeks * 7):
        d = (start + timedelta(days=i)).isoformat()
        n = counts.get(d, 0)
        # Bucket: 0,1-2,3-6,7-15,16+
        if n == 0:   b = 0
        elif n <= 2: b = 1
        elif n <= 6: b = 2
        elif n <= 15: b = 3
        else:        b = 4
        cells.append(_HEAT[b])
    return "".join(cells)


def render_snapshot(sess: Session, *, team: models.Team) -> str:
    """Return one full screen as a string. Used by ``run`` and tests."""
    s = _stats(sess, team_id=team.id)
    owners = _top_owners(sess, team_id=team.id)
    dlq = webhook_dlq.stats(sess, team_id=team.id)
    counts = heatmap.daily_counts(sess, team_id=team.id, days=84)  # 12 weeks
    strip = _heat_strip(counts)

    lines = [
        f"{_BOLD}LabFlow {__version__}{_RESET} "
        f"{_DIM}— team {team.slug}{_RESET}",
        "",
        f"{_BOLD}Tasks{_RESET}  "
        f"{_FG['blue']}open: {s['open']:>4}{_RESET}  "
        f"{_FG['cyan']}in-progress: {s['in_progress']:>4}{_RESET}  "
        f"{_FG['red']}blocked: {s['blocked']:>4}{_RESET}  "
        f"{_FG['green']}done: {s['done']:>5}{_RESET}",
        "",
        f"{_BOLD}Top owners (open + in_progress){_RESET}",
    ]
    if owners:
        for h, n in owners:
            bar = "▇" * min(n, 30)
            lines.append(f"  {_FG['cyan']}{h:<24}{_RESET} {bar} {n}")
    else:
        lines.append(f"  {_DIM}— no assigned owners{_RESET}")
    lines += [
        "",
        f"{_BOLD}Webhook DLQ{_RESET}  "
        f"{_FG['red']}dead: {dlq['dead']}{_RESET}  "
        f"{_FG['yellow']}pending: {dlq['pending']}{_RESET}  "
        f"{_FG['green']}success: {dlq['success']}{_RESET}",
        "",
        f"{_BOLD}Activity (last 12 weeks){_RESET}  {_DIM}(· empty  █ busy){_RESET}",
        f"  {_FG['green']}{strip}{_RESET}",
        "",
        f"{_DIM}press q to quit{_RESET}",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------
def _key_pressed(stream: IO[str] = sys.stdin) -> str | None:
    """Non-blocking read of one key, if any. Returns None on dumb streams."""
    try:
        if not stream.isatty():
            return None
        r, _, _ = select_mod.select([stream], [], [], 0)
        if r:
            return stream.read(1)
    except Exception:  # noqa: BLE001
        return None
    return None


def run(
    sess: Session, *, team: models.Team, refresh_s: float = 1.0,
    once: bool = False, out: IO[str] = sys.stdout,
) -> None:
    """Main loop. Set ``once=True`` for a single render (used in tests)."""
    while True:
        out.write(_CLEAR)
        out.write(render_snapshot(sess, team=team))
        out.flush()
        if once:
            return
        # Sleep responsively so 'q' to quit isn't laggy.
        deadline = time.monotonic() + refresh_s
        while time.monotonic() < deadline:
            if _key_pressed() == "q":
                return
            time.sleep(0.05)


__all__ = ["render_snapshot", "run"]
