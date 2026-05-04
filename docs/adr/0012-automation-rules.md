# ADR-0012: Declarative automation rules engine

* **Status**: accepted (v0.10)
* **Context**: operators want "when task moves to in_review, ping
  Slack and tag it `needs-review`" without writing Python and shipping
  a release. Hard-coding workflow side effects has caused merge
  conflicts and surprise behaviour in past releases.
* **Decision**: a JSON-only rules engine, dispatched from
  `audit.record`, with a tiny condition DSL and three built-in action
  kinds (`tag`, `notify`, `webhook`).

## Why JSON, not a full expression language?

Operator-supplied expressions (CEL, JEXL, hand-rolled) are a notorious
attack surface — sandbox escapes, ReDoS, infinite loops. LabFlow's
condition DSL is intentionally a **flat dict** matched key-by-key
against the event metadata: `{"to": "in_review"}` matches when the
event's metadata has `to == "in_review"` and not otherwise. Anything
fancier (boolean OR, regex, nested fields) is deliberately out of
scope; we'll cross that bridge when an operator presents a real use
case the current model can't satisfy.

## Why dispatch from `audit.record`?

Every meaningful state transition already calls `audit.record` (it's
how the audit chain stays continuous). Hooking the dispatcher there
gives us:

* **complete coverage** — anything audited is automatable, no extra
  sites to instrument;
* **synchronous semantics** — the rule fires inside the same
  transaction as the change, so a `tag` action correctly extends the
  hash chain;
* **failure isolation** — `dispatch` swallows action exceptions so a
  broken webhook URL never blocks a state change.

## Recursion bound

A `tag` action writes an `automation.tag` audit row, which itself
re-enters the dispatcher. This terminates because the new event's
trigger name is `automation.tag`, not the original action — and an
operator would have to deliberately write a rule on `automation.tag`
to re-trigger anything. Even then, the chain bottoms out at a
constant: a rule can fire on its own output at most once per chain
because the second-level event has a different trigger string from
the first.

## Scaling concerns

The dispatcher does one indexed `SELECT * FROM automation_rules WHERE
team_id = ? AND trigger_event = ? AND enabled` per audit row. Index
`ix_rule_team_event` makes this O(matching rules), which in the
expected operating regime (≤30 rules per team) is "free". The
`fires` counter is a non-transactional hint — concurrent dispatches
may under-count slightly, which we accept to avoid a hot-row.

## Consequences

* Operators get a CRUD API for rules (`/api/automation/rules`) and
  zero-deploy iteration.
* The `webhooks.emit` integration means a rule can fire any custom
  outbound webhook without touching code — webhooks already carry
  HMAC signatures and retry semantics (v0.3).
* Future work: action kinds for `assign`, `comment`, and a
  `scope_actor` so an audit author can be impersonated for downstream
  events, gated behind a sandbox / capability model. We didn't ship
  these because each carries non-trivial security implications and
  the three current kinds are enough to be useful.
