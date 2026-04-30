# Plugin marketplace (v0.9)

LabFlow has two plugin layers:

1. **Runtime extension loader** (`labflow/plugins.py`, since v0.5) —
   loads Python entry-points and dotted paths. This is how operators
   actually *execute* plugin code today.
2. **Marketplace catalogue** (`labflow/plugin_marketplace.py`, v0.9) —
   per-team install / enable / disable / uninstall lifecycle for
   *signed manifests*. This is what end-users see in the UI.

This page is about the marketplace. See [ADR-0010](adr/0010-plugin-marketplace.md)
for why we deliberately deferred in-process code execution to v1.0.

## Manifest schema

```json
{
  "name": "weekly-report",
  "version": "1.2.0",
  "author": "Acme Co",
  "description": "Generates a polished weekly PDF report.",
  "labflow_min_version": "0.9.0",
  "permissions": ["read", "webhook:emit"],
  "hooks": ["meeting.finalized"]
}
```

* **`name`** — `[a-z0-9][a-z0-9-]{1,62}` (NPM-ish).
* **`version`** — semver (`1.2.0`, `1.2.0-rc.1`).
* **`permissions`** ⊆ scopes (`read / write / admin / webhook:emit /
  plugin:install`).
* **`hooks`** ⊆ valid event names (`task.transition`, `task.created`,
  `task.closed`, `task.sla.breach`, `meeting.finalized`,
  `decision.created`, `comment.added`).

Anything outside the allowed sets is rejected at install time.

## Hashing

Manifests are hashed with **canonical JSON SHA-256**:

```python
hashlib.sha256(json.dumps(manifest, sort_keys=True,
                          separators=(",", ":")).encode()).hexdigest()
```

The same manifest hashed on any machine produces the same digest. The
operator passes `expected_sha256` on install; mismatches are rejected
with HTTP 400, defending against tampering in transit.

## Lifecycle

| Method | Path | Purpose |
|---|---|---|
| `GET`    | `/api/plugins`               | List installed |
| `POST`   | `/api/plugins`               | Install (admin) |
| `POST`   | `/api/plugins/{name}/enable` | `{"enabled": true|false}` |
| `DELETE` | `/api/plugins/{name}`        | Uninstall (admin) |

Every action is audit-logged (`plugin.install`, `plugin.enable`,
`plugin.disable`, `plugin.uninstall`). New installs default to
`enabled: false` — operators must opt in explicitly. No plugin runs by
surprise.

## Why no execution yet?

Sandboxing arbitrary Python is hard, and doing it badly is a footgun.
Shipping a half-baked sandbox in v0.9 would block the whole release.
We're shipping the catalogue layer now so operators get immediate value
(provenance, scope disclosure, per-team enable/disable) and we can
iterate on manifest design before we tackle execution.

The v1.0 sketch (separate worker process + capability-restricted RPC,
or `wasmtime-py`) is captured in ADR-0010.
