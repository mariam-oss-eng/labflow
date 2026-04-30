# ADR-0010 — Plugin marketplace v0 (catalogue without sandbox)

**Status:** Accepted (v0.9, 2026-04-30)

## Context

LabFlow shipped a *runtime* plugin loader in v0.5 (`labflow/plugins.py`)
that imports Python entry-points and dotted paths and registers
extractors / verifiers / event handlers. That works for operators who
control the deploy, but it doesn't address the operator-facing question:
**which plugins are installed in this team, who installed them, what
permissions do they want, and how do I disable one without restarting
the API?**

A full marketplace also needs to answer: **how do we run untrusted
plugin code safely?** Sandboxing arbitrary Python is hard. Doing it
poorly is a footgun.

## Decision

Ship the **catalogue + lifecycle** layer in v0.9 and explicitly **defer
in-process code execution** to v1.0 (or later, behind a feature flag).

The v0.9 marketplace:

* Persists installed plugins in a new `plugins` table, scoped per team.
* Defines a strict **manifest schema** (name, semver version, author,
  description, `permissions ⊆ scopes`, `hooks ⊆ valid_hooks`).
* Validates that the manifest's **canonical-JSON SHA-256** matches an
  expected hash supplied by the operator on install — defending against
  manifest tampering in transit.
* Tracks `enabled` separately from `installed`, so an operator can flip
  a plugin off without uninstalling.
* Audit-logs every install / enable / disable / uninstall.

What it intentionally does **not** do in v0.9:

* Download or execute plugin code.
* Process the `permissions` field at runtime (it's recorded, not
  enforced — there's no code running to enforce it against).
* Process the `hooks` field at runtime (same reason).

The runtime extension loader from v0.5 is still the way to actually
*execute* plugin code today, deployed by the operator the old way.

## Consequences

* **Discoverability today, safety later.** Operators can browse and
  manage their plugin catalogue right now without us shipping a half-
  baked sandbox. The hash check + manifest schema gives the catalogue
  immediate value (provenance, scope-of-permission disclosure).
* **Forward path is well-defined.** When we ship v1.0 sandboxed
  execution, the catalogue is the natural authority for "is this plugin
  installed and enabled?" The runtime will read from `plugins.enabled`
  and apply `permissions` against API-key scopes.
* **No surprises for users.** The marketplace API explicitly returns
  `enabled: false` after install — operators have to opt in. No plugin
  silently runs after a fresh install.
* **Manifest is canonical-JSON-hashed.** `manifest_hash()` uses
  `sort_keys=True, separators=(",", ":")`, so the same manifest hashed
  on two different machines always produces the same SHA-256.

## v1.0 sandbox sketch (not in this release)

When we tackle execution, we'll evaluate (in order of preference):

1. **Run plugin code in a separate worker process** with a
   capabilities-restricted RPC surface. Permissions become the only
   API the plugin can call.
2. **Wasm runtime** (e.g. `wasmtime-py`) for non-Python plugins. Higher
   ceiling on isolation, lower ceiling on ergonomics.
3. **Subinterpreters** (PEP 684) once stable. Avoids the IPC overhead
   of option 1 with similar isolation properties.

## Alternatives considered

| Option | Why we passed |
|---|---|
| **Ship execution alongside catalogue** | Sandboxing is a multi-month design problem; we'd block the whole release |
| **Skip the catalogue, rely on entry-points** | No way for operators to enable/disable per team; no permission disclosure |
| **Signed binaries (PGP)** | Adds key-management burden disproportionate to v0.9 risk profile; SHA-256 + canonical JSON is enough provenance for the catalogue layer |

## See also
* `labflow/plugin_marketplace.py`
* `labflow/plugins.py` (v0.5 runtime loader)
* `tests/test_v09.py::test_plugin_*`
