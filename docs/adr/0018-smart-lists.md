# ADR-0018: Smart lists are declarative, not query DSL

* **Status**: accepted (v0.13)
* **Context**: Operators want named, reusable filters ("tasks
  assigned to me, due this week, in the current sprint"). The
  obvious move would be to ship a tiny query DSL.
* **Decision**: smart lists store a JSON object with a fixed,
  whitelisted key set. The runtime translates the object directly
  into SQLAlchemy filter clauses. No parser, no eval, no surprises.

## What the filter looks like

```json
{
  "state": "in_progress",
  "assignee_handle": "alice",
  "label": "deploy",
  "due_before": "2026-06-01",
  "sprint_slug": "Q2-W4"
}
```

Every key is optional. Missing keys are wildcards. Unknown keys raise
`ValidationError` at *save* time (not just at run time), so a typo can
never silently match everything.

## Why not a real DSL?

A DSL would buy expressivity (`OR`, `NOT`, nested groups). The cost
is a parser, a security-review surface (every parser is an injection
risk), and an executor that has to enforce the team-scoping invariant
through arbitrarily-shaped trees.

We deliberately picked a flat AND-of-equality shape because:

* every shipped filter so far has been expressible this way;
* the SQL is one `.where()` per key, trivially auditable;
* team-scoping is enforced by the outer query, not by the filter
  contents (so no filter can ever escape the team boundary);
* if a future need really requires `OR`, we can extend the schema
  with a `clauses: [{...}]` array — backwards-compatible.

## Performance

Smart lists hit the same indexes as the underlying entity routes —
nothing special. They cap at 1000 results per call (operator-tunable
on the per-call URL parameter, hard cap in the helper) so a poorly
chosen filter can't exhaust memory.

## Consequences

* The save → run path is two short helper functions. No new parser to
  fuzz, no new evaluator to review.
* Any field on `Task` can be added to the whitelist in a single
  diff; the runtime path remains a string-keyed dispatch.
* If we eventually need disjunction, the document model degrades
  gracefully into a `clauses: [...]` shape rather than needing a
  parallel system.
