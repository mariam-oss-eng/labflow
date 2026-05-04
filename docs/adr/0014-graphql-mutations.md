# ADR-0014: GraphQL mutations on the hand-rolled engine

* **Status**: accepted (v0.11)
* **Context**: ADR-0006 ships a deliberately tiny read-only GraphQL
  engine. v0.11 needs write access from the same surface so that the
  shipping TypeScript SDK can operate against a single endpoint.
* **Decision**: extend the parser with a one-token operation kind
  (`query` vs `mutation`) and add a separate dispatch table for
  mutations. No new dependency, no new endpoint.

## Why not just add Strawberry now?

The original ADR-0006 trade-off (parser + executor in ≤300 lines vs a
2-3 dependency stack) still holds. Adding mutation support pushed us
to ≈400 lines, well within budget. We will revisit when:

* the surface exceeds ≈10 mutations or ≈25 query fields;
* clients want subscriptions (we'd need to bridge to SSE / WebSocket
  anyway);
* operator-supplied schema extension lands.

Until any of those, "small, single-file, zero-dep" remains the right
trade.

## What's the operation parser doing?

The parser was previously stateless. We added a `self.operation`
attribute set during the optional operation prefix
(`query`/`mutation`/`<op> Name`). The public `parse(src)` keeps its
back-compat signature; a new `parse_with_op(src)` returns
`(operation, fields)` for the executor.

## Mutation surface (initial set)

Three mutations land in v0.11. They were chosen because they're the
operations the SDK absolutely needs and the operations whose REST
counterparts already enforce ACL/scope correctly:

* `commentCreate(entity_type, entity_id, body)` → `Comment`
* `taskTransition(id, to_state)` → `Task`
* `wikiPageUpsert(slug?, title, body)` → `WikiPage`

Each mutation calls the same service-layer function the REST handler
calls (`collab.add_comment`, `workflows.transition_task`,
`wiki.upsert_page`), so:

* role / scope enforcement is uniform with REST;
* the audit chain extends with the same actor labels;
* automation rules fire identically.

This is the whole reason a thin service layer exists.

## Error handling

GraphQL servers traditionally return partial data plus an `errors`
array. We follow that here so a request with three mutations doesn't
abort if the second one violates a workflow guard. Each mutation is
wrapped in its own `try/except` and the error message is bounded to
200 chars to avoid leaking stack traces.

## Consequences

* Same `/graphql` endpoint accepts queries and mutations.
* Schema introspection (`__schema`) still lists only queries; we
  haven't extended introspection to cover mutation types because
  Strawberry-style introspection brings real complexity (variables,
  argument types, nullability flags) and the SDK doesn't need it.
* If we land subscriptions next, the operation-kind hook is already
  in place — `subscription` would be a third dispatch path.
