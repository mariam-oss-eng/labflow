# Automation rules

Operators can describe when/then automation **declaratively in JSON**
without shipping code (v0.10). Rules are stored per-team in the
`automation_rules` table and dispatched synchronously from the audit
layer, so any audited event can trigger automation.

## Anatomy of a rule

```json
{
  "name": "review-handoff",
  "trigger_event": "task.transition",
  "condition": {"to": "in_review"},
  "actions": [
    {"kind": "tag",     "params": {"tag": "needs-review"}},
    {"kind": "notify",  "params": {"message": "Task moved to review"}},
    {"kind": "webhook", "params": {"event": "task.review_requested"}}
  ],
  "enabled": true
}
```

* **`trigger_event`** matches the audit `action` field
  (`task.transition`, `meeting.finalized`, `wiki.page.updated`, etc.).
* **`condition`** is a flat dict; every key must equal the same key in
  the event metadata. `null` or `{}` means "always".
* **`actions`** are dispatched in declared order. Three kinds ship
  built-in:
  * `notify` → emits an SSE event `automation.notify` to anyone
    subscribed to the team's stream.
  * `webhook` → fires a custom event via the standard webhooks
    machinery (HMAC-signed, retried).
  * `tag` → writes an `automation.tag` audit row (extending the hash
    chain) so you can query "everything tagged needs-review".

## CRUD

```bash
# Upsert (create or replace by name)
curl -X POST -H "Content-Type: application/json" -H "X-API-Key: $KEY" \
     -d @rule.json \
     https://labflow.example/api/automation/rules

# List
curl -H "X-API-Key: $KEY" \
     https://labflow.example/api/automation/rules

# Delete
curl -X DELETE -H "X-API-Key: $KEY" \
     https://labflow.example/api/automation/rules/review-handoff
```

The list response includes `fires` and `last_fired_at` per rule, so
you can see which rules are actually doing work.

## Why no expression language?

Operator-supplied expressions are a notorious attack surface (sandbox
escape, ReDoS, infinite loop). The flat-dict matcher has zero CVE
budget and covers ~90% of real-world rules. If you have a use case
the matcher can't express, please file an issue — but the bar for
adding boolean operators / regex / nested fields is high. See
[ADR-0012](adr/0012-automation-rules.md).

## Failure isolation

Rule actions are best-effort: a failure inside an action is **logged
but not raised**, so a misconfigured webhook URL never blocks the
underlying state change. Inspect the application log if a rule isn't
producing the expected effect.
