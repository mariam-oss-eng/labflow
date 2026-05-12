# ADR-0029: Activity heatmap from existing audit data, rendered as SVG

* **Status**: accepted (v0.17)
* **Context**: We want the GitHub-style "365 days of activity" grid on
  the dashboard, in the README, and in scheduled-report payloads. We
  refused to add a chart library or a "metrics" sub-table — both
  would bloat the runtime and would mean another consistency story.
* **Decision**: Compute daily counts straight from
  `audit_events.created_at` (`heatmap.daily_counts`), and render
  inline SVG (`heatmap.render_svg`) — no JS, no canvas, just a
  `<svg>` with `<rect>` children. The endpoint is
  `GET /api/heatmap.svg`.

## Why on top of `audit_events`

* The audit log is the canonical "something happened" stream. Counts
  retroactively cover every team and any feature past or future
  without a migration.
* Adding a separate `daily_activity_counts` table would be faster to
  query but slower to evolve — every new event source would have to
  remember to increment the rollup. The 365-row scan over a single
  team's audit is a few milliseconds even on SQLite.

## Why server-rendered SVG (no JS)

* Embeds in a README via `![](https://…/api/heatmap.svg)` — works on
  GitHub, GitLab, anywhere an `<img>` renders.
* Embeds in PDFs and Slack attachments without a Chromium dependency.
* Renders the same in dark and light themes — colours are part of
  the SVG.
* No XSS surface: all content is generated server-side from
  formatted dates and integer counts; we never interpolate raw user
  input.

## What we sacrifice

* No interactive tooltips beyond `<title>` (which the browser shows
  on hover but isn't keyboard-accessible). Good enough for the
  use cases.
* No per-actor / per-event-type breakdown. Add a `?actor=` filter
  later if asked; the current shape was the smallest unit shippable.
