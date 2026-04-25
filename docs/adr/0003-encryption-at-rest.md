# ADR-0003 · Encryption at rest with Fernet

* Status: Accepted
* Date: 2026-04-25 (v0.5)

## Context

Customers in regulated industries (biotech, finance, EU-only) need an
encryption-at-rest story that is **independent** of full-disk
encryption on the host. Auditors specifically want to know: *"if
someone obtained a copy of your database, would the transcript
columns be plaintext or ciphertext?"*

The threat model we *do* address: a copy of the SQL database leaks
(backup mishandled, S3 bucket misconfigured, compromised analytics
replica). The threat model we explicitly *don't* address: a full
process compromise — by definition, an attacker with code execution
inside the API has access to the key.

## Decision

We add a SQLAlchemy `TypeDecorator` called `EncryptedText` that wraps
`Text` and runs values through `cryptography.Fernet` on bind/result.
The key is read from `LABFLOW_DATA_KEY`; multiple keys can be
configured (comma-separated) to enable `MultiFernet`-style rotation.

We apply `EncryptedText` to the two columns that genuinely contain
free-form sensitive content: `meetings.transcript` and
`meetings.notes`. We **do not** encrypt small structured fields like
decision statements or task titles, because:

* The existing search index relies on a `LIKE` over those columns —
  encrypting them would force us to ship application-side
  deterministic AES-SIV (or a CLOB sidecar) for every query.
* The risk profile is different: a leaked decision title carries
  meaningfully less harm than a leaked transcript chunk.

When no key is configured, the decorator is a no-op — existing
deployments continue to read/write plaintext, with no migration step.
Reads transparently handle plaintext (`gAAAAA…` prefix detection)
which makes in-place upgrades safe.

## Consequences

* **Positive** — single env var enables encryption; `MultiFernet`
  gives us key rotation without downtime; the `cryptography` library
  is widely audited and stdlib-adjacent. Tests prove that raw rows in
  SQLite are ciphertext and that round-trips preserve the value.
* **Negative** — search and full-text features cannot operate on the
  encrypted columns directly. We have not (and will not) encrypt
  `decisions.statement` or `tasks.title` for that reason. Customers
  who need *every* field encrypted must front the API with a separate
  field-level encryption proxy.
* **Follow-on** — when we ship envelope encryption with KMS-managed
  data keys, this decorator stays — it'll just call into a KMS-backed
  Fernet provider rather than reading from the env directly.
