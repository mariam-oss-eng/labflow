# ADR-0013: Wiki + smart entity links

* **Status**: accepted (v0.11)
* **Context**: meeting transcripts and decision rationales reference
  prior tasks, prior decisions, and prior conversations every day.
  Without a structured cross-reference, those links rot — the user
  reading a 6-month-old decision has no way to follow `task #42` to
  see how it turned out, and a `[[Tooling]]` aside in a transcript
  becomes dead text.
* **Decision**: a slug-addressed Markdown wiki (`wiki_pages` +
  immutable `wiki_revisions`) plus a regex-based smart-link parser
  (`labflow.links`) that materialises three patterns into the new
  `entity_links` table on every wiki / comment / transcript save:
  `#task-N`, `[[Page Name]]`, `@handle`.

## Why a separate `entity_links` table?

We could have re-parsed source bodies on read. We don't, for two reasons:

1. **Backlinks are a read-heavy workload.** The Tooling page wants to
   answer "who links to me" instantly, not after a full-table scan
   over every wiki page and comment body.
2. **Renames are a write-heavy event.** Materialised links survive a
   wiki page rename in the obvious way (the link points at the row
   id, not the slug); re-parsing on read would either need slug-
   redirect tables or accept dangling references.

The materialised table has indexes on both `(team_id, source_*)` and
`(team_id, target_*)` so both directions are O(matching rows).

## Why placeholder pages for unknown wikilinks?

When a transcript says "see [[Architecture]]" but no Architecture page
exists yet, we create an *empty page* and link to it. This means:

* the backlink survives until the page is authored;
* the writer can click through and start the page from the link;
* the link target id is stable from day one, even if a different
  author later writes the page.

The downside — placeholder pages clutter the page index — is
mitigated by the page summary being empty and the `deleted` flag, so
operators can prune them with a one-line query.

## Why a regex-based parser?

A real Markdown AST would correctly skip code blocks. We don't ship
one because:

* Each pattern has a strictly bounded character class — no nested
  quantifiers, no catastrophic backtracking.
* The cost of a false positive (a `#task-12` inside a code fence
  becoming a real link) is one stray `entity_links` row, not a
  security issue.
* Adding `mistune` / `markdown-it-py` for a 50-line regex would
  approximately double the install footprint.

## Soft delete, not hard delete

`wiki_pages.deleted = True` is a flag rather than a row removal so
that an old revision of *another* page that links to the deleted page
still resolves: a backlink from a 6-month-old transcript to "Old
Spec" does not silently 404.

## Consequences

* Wiki pages, comment bodies, and meeting transcripts gain
  bidirectional navigation for free.
* Storage cost: one `entity_links` row per resolved reference. At our
  observed rate (~5 references per meeting), this is negligible.
* GraphQL `wikiPageUpsert` mutation reuses the same code path
  (`wiki.upsert_page`) so REST and GraphQL stay byte-identical in
  behaviour. ADR-0014 covers the mutation layer.
