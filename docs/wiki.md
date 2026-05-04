# Wiki & smart links

LabFlow's wiki (v0.11) is a slug-addressed Markdown knowledge base
with **immutable revision history** and **bidirectional links**.

## Pages

```bash
# Upsert (create or replace by slug; auto-slug from title if omitted)
curl -X POST -H "Content-Type: application/json" -H "X-API-Key: $KEY" \
     -d '{"title": "Onboarding",
          "body": "Welcome to LabFlow. See [[Architecture]] and #task-12."}' \
     https://labflow.example/api/wiki/pages

# Read
curl https://labflow.example/api/wiki/pages/onboarding

# History (immutable, newest first)
curl https://labflow.example/api/wiki/pages/onboarding/revisions

# Search (case-insensitive substring)
curl "https://labflow.example/api/wiki/search?q=postgres"

# Soft delete (preserves backlinks from older revisions of other pages)
curl -X DELETE -H "X-API-Key: $KEY" \
     https://labflow.example/api/wiki/pages/onboarding
```

Every save writes a new row in `wiki_revisions`, so you can recover
the exact text of any prior version.

## Smart entity links

The body of every wiki page, comment, and meeting transcript is
parsed for three patterns. Hits become `entity_links` rows — typed
cross-references you can query in either direction:

| pattern         | becomes a link to                       | kind        |
|-----------------|-----------------------------------------|-------------|
| `#task-N`       | the existing `Task` with that id        | `ref`       |
| `[[Page Name]]` | the `WikiPage` with that slug (auto-creates a placeholder if missing) | `wikilink` |
| `@handle`       | the `Owner` with that handle            | `mention`   |

### Backlinks

```bash
curl "https://labflow.example/api/links/backlinks?target_type=task&target_id=4127"
```

```json
{
  "backlinks": [
    {"source_type": "wiki",    "source_id": 12, "kind": "ref",     "created_at": "..."},
    {"source_type": "comment", "source_id": 84, "kind": "ref",     "created_at": "..."}
  ]
}
```

Backlinks are also embedded directly in `GET /api/wiki/pages/{slug}`
so the page renderer can show "Linked from…" without a second call.

### Placeholder pages

When a transcript writes `[[Architecture]]` and no Architecture page
exists yet, LabFlow creates an empty placeholder. Why?

* The backlink survives until the page is authored.
* The author can click through and start the page from the link.
* The link target id is stable, so renaming the page later doesn't
  break inbound references.

Placeholders are normal pages with empty `body`; prune them with a
single SQL query if they accumulate.

### Why a regex parser, not a full Markdown AST?

Each pattern uses a strictly-bounded character class — no nested
quantifiers, no catastrophic backtracking. The cost of a false
positive (a `#task-12` inside a code fence becoming a real link) is
one stray row, not a security issue. See
[ADR-0013](adr/0013-wiki-and-smart-links.md).
