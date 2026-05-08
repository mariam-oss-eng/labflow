# ADR-0017: Federated guest invites with scoped ACLs

* **Status**: accepted (v0.13)
* **Context**: Researchers regularly want a single outsider to see
  one task or one wiki page — not the whole team's data, not for
  long. Cutting an API key by hand and remembering to revoke it is
  error-prone.
* **Decision**: introduce one-time `Invite` rows. Acceptance mints a
  fresh API key whose ACL is materialised from the invite's
  declared scope. Tokens are never stored in plaintext.

## Why one-time tokens, not a "share link"?

A share link (v0.8) is a *bearer URL* — anyone who sees it can read.
That's the right primitive for "send the kickoff doc to a vendor".
It is the wrong primitive for "let an external reviewer comment on
this PR's task". An invite differs because:

* it is **bound to an email** (audit trail);
* it can be **revoked** without invalidating other shares;
* acceptance creates a **distinct key** (the guest can be quota-d,
  watched, and traced individually);
* the resulting key carries **the same ACL rows** every other
  scoped key has — no special-case enforcement path.

Share links and invites coexist. Use share links for read-only
artefact distribution; use invites for collaborative access.

## Storage of the token

Only `sha256(token)` is persisted. The plaintext is returned exactly
once, when the admin creates the invite, and once when the guest
accepts (in the form of the minted API key). Rotating the storage
hash if the secret strength assumption changes is a single column
swap — no plaintext ever to re-hash.

## ACL materialisation, not policy interpretation

The invite carries a list of `(entity_type, entity_id, permission)`
entries. On acceptance we *insert* one `ResourceAcl` row per entry
under the new key. This means the runtime ACL check is the same
unionised path every other key uses; there is no second policy
engine to keep in sync.

The downside: changing an invite after acceptance does not
retroactively change the guest's access. We consider this a feature —
a scope that auto-grows is a security smell. To extend a guest's
access, issue a new invite.

## Expiry & revocation

* `expires_at` is enforced both by acceptance (`accept_invite` flips
  to `revoked` on a stale token) and by a periodic sweep that closes
  pending invites whose deadline has passed.
* `revoke_invite` flips status; an admin can revoke before
  acceptance (kills the token) but not after (the guest already has
  a key — revoke the key explicitly).

## Audit & observability

Every invite lifecycle event lands on the v0.10 hash chain:
`invite.created`, `invite.revoked`, `invite.accepted`. The accepted
event records the new `api_key_id` so operators can pivot from
"who's the guest?" to "what did they see?" with one query.

## Consequences

* The same `ResourceAcl` row drives the same enforcement path, so
  invites inherit every existing read/write check — including future
  ones — for free.
* Guest keys are first-class citizens of the quota and notification
  systems (v0.12).
* If we add an OIDC adapter later, it slots in beside this code path
  and uses the same ACL substrate.
