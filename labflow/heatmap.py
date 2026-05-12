"""Activity heatmap (v0.17).

GitHub-style yearly activity grid built from :class:`labflow.models.AuditEvent`
rows. The heatmap is computed entirely from existing data — no new
table — so it works retroactively on any team.

Two outputs:

* :func:`daily_counts` — ``{"YYYY-MM-DD": int}`` for the API
* :func:`render_svg`   — a self-contained SVG string ready to embed in
  a README, dashboard, or wiki page

Buckets and bucket colours follow the conventional 5-step ramp::

    0          → empty
    1..2       → low
    3..6       → medium
    7..15      → high
    16+        → max

Colours match the dashboard CSS variables (``--lf-heat-0..4``); we also
ship sensible defaults so the SVG renders standalone.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .errors import ValidationError

DAYS_DEFAULT = 365
DAYS_MAX = 366
_BUCKETS = [(0, 0), (1, 1), (3, 2), (7, 3), (16, 4)]   # (>=count, bucket)
_DEFAULT_COLORS = ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"]


def _bucket(n: int) -> int:
    out = 0
    for threshold, b in _BUCKETS:
        if n >= threshold:
            out = b
    return out


def daily_counts(
    sess: Session, *, team_id: int, days: int = DAYS_DEFAULT,
    end: date | None = None,
) -> dict[str, int]:
    """Return audit-event counts per day for the last ``days`` days."""
    if days < 1 or days > DAYS_MAX:
        raise ValidationError(f"days must be in [1, {DAYS_MAX}]")
    end_date = end or datetime.utcnow().date()
    start_date = end_date - timedelta(days=days - 1)
    start_dt = datetime.combine(start_date, datetime.min.time())

    rows = sess.execute(
        select(models.AuditEvent.created_at).where(
            models.AuditEvent.team_id == team_id,
            models.AuditEvent.created_at >= start_dt,
        )
    ).all()
    counts: dict[str, int] = defaultdict(int)
    for (created,) in rows:
        d = created.date()
        if d > end_date:
            continue
        counts[d.isoformat()] += 1
    # Densify with zeros so the client doesn't have to.
    out: dict[str, int] = {}
    cursor = start_date
    while cursor <= end_date:
        key = cursor.isoformat()
        out[key] = int(counts.get(key, 0))
        cursor += timedelta(days=1)
    return out


def render_svg(
    counts: dict[str, int],
    *,
    cell_size: int = 11,
    cell_gap: int = 2,
    colors: list[str] | None = None,
) -> str:
    """Render a heatmap SVG. ``counts`` is the output of :func:`daily_counts`.

    Layout: weeks are columns, weekdays are rows (Sunday = 0 at the top
    to match GitHub). A title bar shows the total event count.
    """
    palette = colors or _DEFAULT_COLORS
    if len(palette) != 5:
        raise ValidationError("colors must have exactly 5 entries")
    if not counts:
        return _empty_svg()

    # Sort by date and locate the first Sunday <= start so the grid
    # aligns to whole weeks.
    dates = sorted(date.fromisoformat(k) for k in counts)
    start = dates[0]
    end = dates[-1]
    # weekday(): Monday=0..Sunday=6 → convert to Sunday=0..Saturday=6
    sun_offset = (start.weekday() + 1) % 7
    grid_start = start - timedelta(days=sun_offset)
    weeks = ((end - grid_start).days // 7) + 1

    width = weeks * (cell_size + cell_gap) + cell_gap
    height = 7 * (cell_size + cell_gap) + cell_gap + 18  # +18 for title
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="LabFlow activity heatmap">',
        f'<text x="{cell_gap}" y="12" font-family="system-ui,sans-serif" '
        f'font-size="11" fill="#57606a">'
        f'{sum(counts.values())} events · {len(counts)} days</text>',
    ]
    for i in range((end - grid_start).days + 1):
        d = grid_start + timedelta(days=i)
        wkday = (d.weekday() + 1) % 7  # Sun=0
        week = i // 7
        n = counts.get(d.isoformat(), 0)
        b = _bucket(n)
        x = cell_gap + week * (cell_size + cell_gap)
        y = 18 + cell_gap + wkday * (cell_size + cell_gap)
        parts.append(
            f'<rect x="{x}" y="{y}" width="{cell_size}" '
            f'height="{cell_size}" rx="2" ry="2" fill="{palette[b]}">'
            f'<title>{d.isoformat()}: {n}</title></rect>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _empty_svg() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="20" '
        'viewBox="0 0 120 20" role="img" aria-label="empty heatmap">'
        '<text x="2" y="14" font-family="system-ui,sans-serif" '
        'font-size="11" fill="#57606a">no activity yet</text></svg>'
    )


__all__ = ["DAYS_DEFAULT", "DAYS_MAX", "daily_counts", "render_svg"]
