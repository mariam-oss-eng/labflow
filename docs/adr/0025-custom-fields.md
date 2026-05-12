# ADR-0025: Per-team custom fields without an EAV explosion

* **Status**: accepted (v0.16)
* **Context**: Different teams want different metadata on tasks and
  decisions — `epic`, `cost_center`, `risk_level`, `customer_id`. We
  refuse to ship one mega-table with 50 nullable columns, and we
  refuse "JSON blob, you sort it out" because then LFQL and reports
  can't filter on it.
* **Decision**: Two normalised tables — `custom_field_defs` (one row
  per `(team, entity_type, key)`) and `custom_field_values` (one row
  per `(def, entity)`) — with strict server-side validation per kind.

## Schema

```
custom_field_defs(team_id, entity_type, key, label, kind,
                  options_json, required)
custom_field_values(team_id, def_id, entity_id, value)
```

* `kind ∈ {text, number, date, select}` — driven entirely by the
  parser in `custom_fields._coerce_value`.
* Values are always stored as **text**; clients get back the typed
  value via `get_typed_value(field, raw)`.
* `(def_id, entity_id)` is unique so set-then-set is an `UPDATE`, not
  an `INSERT` — avoids the value-history sprawl that EAV systems hit.

## Why two tables (not JSON-on-the-row)

* `JSON column on tasks/decisions` would force every read to know
  about custom fields and would skip the validator on writes.
* `tasks.custom_fields → JSON` ties the schema to one entity kind. The
  current shape works for tasks **and** decisions today and any
  entity tomorrow, just by adding a string to `VALID_ENTITIES`.

## What we deferred

* Indexing values for filter pushdown — the v0.16 shape is fine for
  ≤ 10⁵ values per team. When we need it we can add a secondary index
  per `kind` (e.g. a `value_num` shadow column for `number` fields)
  without changing the public API.
* Per-field ACLs. For now, anyone with `member` role can read/write
  any value; admins manage definitions.
