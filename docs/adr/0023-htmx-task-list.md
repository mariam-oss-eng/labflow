# ADR-0023: HTMX over a SPA for the task list page

* **Status**: accepted (v0.15)
* **Context**: We want a polished, *interactive* task list at
  `/app/tasks` — search-as-you-type, inline status transitions — but
  the codebase is a Python monolith with **zero** JS toolchain. Adding
  React/Vue/Svelte means: a build step, a `package.json` for the web
  app, a CDN or asset pipeline, hydration, and a parallel data layer.
* **Decision**: Server-render the page, drop in `htmx@2` from a CDN,
  and use HTML fragments for partial updates. No build step, no
  framework, no extra runtime cost.

## What the page does

* `GET /app/tasks`               — full HTML page (header, filters, table).
* `GET /api/tasks/_table`        — just the `<table>` HTMX swaps in.
* `POST /api/tasks/_status/{id}` — change status, return refreshed row.

The fragment endpoints return `text/html` because htmx's default
`hx-swap` operates on HTML fragments.

## Why this beats a SPA at our scale

* **Zero new dependencies**. The CDN tag is the entire client.
* **Ships in one commit**. No webpack config, no ESLint, no
  TypeScript.
* **First paint is the table**. No "loading…" flash before client JS
  hydrates.
* **Accessibility for free**. The page works without JS — the filter
  inputs are plain `<input>`/`<select>` and the buttons POST forms.
* **Same data layer**. The fragment routes use the same SQLAlchemy
  helpers as the JSON API, so there's no second source of truth.

## What we sacrifice

* Rich client-side state (drag-and-drop kanban) is harder. For that
  we already have `/app/board/{workflow}` (also server-rendered) and
  can adopt Alpine.js or htmx-extensions if needed.
* No offline mode. The PWA shell still covers offline read for the
  JSON API.
