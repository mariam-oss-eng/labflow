# ADR-0004 · Collaboration as a generic comment / reaction layer

* Status: Accepted
* Date: 2026-04-26 (v0.6)

## Context

We want comments and emoji reactions on decisions and tasks, and likely
on more entity types in future (experiments, evidence, blockers). Two
shapes are possible:

1. **One table per entity type** — `decision_comments`, `task_comments`,
   etc. Strongly typed FKs, dead-simple queries, but every new entity
   type duplicates the table + indexes + API + tests.
2. **Generic `(entity_type, entity_id)` polymorphism** — one table for
   comments and one for reactions, columns that name the target type and
   carry an integer id without a true FK.

## Decision

We pick **(2) — generic polymorphism**, with three guardrails:

* `entity_type` is a closed `frozenset` validated in the service layer
  before any insert, so unknown values are rejected at the application
  edge instead of silently writing junk rows.
* Each entity-type insert *re-fetches* the target row to verify (a) it
  exists and (b) it belongs to the caller's team. Tenant isolation
  doesn't depend on the absence of a FK.
* A composite index on `(team_id, entity_type, entity_id)` keeps the
  hot read path (list comments for a target) at one B-tree lookup.

## Trade-offs

* **No referential integrity at the DB level** — if a decision is
  hard-deleted (e.g. via the GDPR erase endpoint) its comments are not
  cascade-deleted. Mitigation: the erase endpoint already enumerates
  team-scoped tables; we add `comments` and `reactions` to its sweep
  list.
* **Reactions deduped on `actor` label, not `actor_key_id`** — single-team
  mode has no API key id, so we fall back to the `actor` string. In
  multi-tenant mode both are present and unique, so dedupe is precise.
* Adding a new commentable entity is a one-line change to the
  `VALID_ENTITIES` set + the dispatch dict — no new table, migration,
  or route.

## Alternatives considered

* **GitHub-style "issue body" model** where comments are first-class
  rows that own a "kind" column. Rejected: it conflates the target with
  the discussion thread and would have forced us to retrofit
  `decisions`/`tasks` rather than augment them.
* **An attached JSON field on the target row** holding inline comments.
  Rejected: kills append-only audit semantics, breaks pagination, and
  fights with our encryption-at-rest policy on `meetings.transcript`.
