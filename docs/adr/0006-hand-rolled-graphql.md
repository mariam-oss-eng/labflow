# ADR-0006 · Hand-rolled GraphQL instead of a framework

* Status: Accepted
* Date: 2026-04-26 (v0.7)

## Context

We want a GraphQL endpoint for power users who prefer "ask for exactly
the fields I want" over REST. Three options:

1. **Strawberry / Ariadne / Graphene** — full-featured GraphQL servers
   with type registries, async resolvers, and federation hooks.
2. **A minimal hand-rolled parser + dispatch table** — implements just
   enough of the spec for the queries we care about.
3. **No GraphQL at all** — push users to compose REST calls.

## Decision

We pick **(2)**. The implementation lives in `labflow/graphql_api.py`
and is ~300 lines, including the tokenizer, parser, schema literal,
and resolvers. It supports:

* Anonymous + named queries.
* Selection sets nested arbitrarily deep.
* Field arguments (string / int / bool / null literals).
* `__schema { types { name fields { name } } }` introspection so client
  tooling (Apollo DevTools, the Insomnia GraphQL panel) autocompletes.
* A typed error array — unknown fields and resolver exceptions surface
  in `errors[]` instead of crashing the request.

It does **not** support: mutations, fragments, variables, directives,
unions/interfaces, subscriptions. If we need any of those, we swap to
Strawberry — the route signature stays.

## Why this is OK

* **Read-only by design.** All write-side mutation in LabFlow happens
  through REST (which has the existing RBAC, rate-limit, idempotency,
  audit, and webhook plumbing); GraphQL is for *reads*. Mutations
  through GraphQL would re-implement all that infrastructure.
* **Zero dependencies.** Strawberry pulls Pydantic v2-compatible
  resolvers, `python-graphql-core`, and a parser combinator — ~5 MB of
  install footprint. For a read API of six fields, that's not worth it.
* **Explicit beats clever.** Each resolver is a 3-line function that
  calls the same SQLAlchemy query the REST handler does, returning a
  dict. There's no schema-generation magic to debug.

## Trade-offs

* **No real validator.** We don't reject extra arguments or wrong types
  before resolution — the resolver itself errors. Clients see useful
  messages but linting tools that rely on schema introspection will not
  catch errors.
* **`__schema` is a literal.** It's hand-maintained. If a resolver and
  the literal drift, introspection lies. We mitigate via a smoke test
  asserting the documented field names match the resolver dispatch.
