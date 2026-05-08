# Federation: guest invites & Markdown bundle export

Two complementary v0.13 features for letting data flow *across* team
boundaries without losing tamper-evidence.

## Guest invites

Invite a single outsider to see one task or wiki page, with an
expiring token, without making them a team member.

### Create

```bash
curl -X POST http://localhost:8000/api/invites \
  -H 'Content-Type: application/json' \
  -d '{
    "email": "reviewer@example.com",
    "role": "viewer",
    "ttl_hours": 168,
    "acl_entries": [
      {"entity_type": "task", "entity_id": 42, "permission": "read"},
      {"entity_type": "wiki", "entity_id": 7,  "permission": "read"}
    ]
  }'
```

The response carries the **token exactly once**. Send the token to
the invitee out of band; LabFlow only stores its SHA-256 hash.

### Accept

```bash
curl -X POST http://localhost:8000/api/invites/accept \
  -d '{"token": "<the token>"}'
```

The response carries a **fresh API key** (also returned exactly
once). Use it like any other LabFlow API key — but its `ResourceAcl`
rows confine it to the entities the invite specified.

### Lifecycle

* `revoke_invite` flips an unaccepted token to `revoked` (the token
  immediately stops working).
* Accepted invites cannot be revoked through this endpoint — revoke
  the resulting API key instead.
* Expired pending invites close on the next sweep.

Every state change emits an audit event
(`invite.created`/`invite.revoked`/`invite.accepted`) on the v0.10
hash chain, so a reviewer's actions can always be traced back to the
admin who invited them.

See [ADR-0017](adr/0017-federated-invites.md) for the design choices.

## Markdown bundle export

Compile a team's full execution history into a single zip:

```bash
curl -OJ http://localhost:8000/api/admin/export/bundle.zip
```

Contents:

```
manifest.json              # version, generated_at, per-collection counts
audit.jsonl                # one JSON object per audit row, in chain order
meetings/000001-kickoff.md
decisions/000001.md
tasks/000042-deploy-staging.md
wiki/onboarding.md
```

The bundle is self-contained — no DB needed to read it — and
unsigned. Pipe it through the v0.10 backup signer if you need
detached-signature integrity.

### What it's good for

* **Archival** — tar the zip alongside your regular backups.
* **Demos** — show a colleague the actual content without granting
  them DB access.
* **Migration** — diff two bundles to see what changed between
  releases.
* **Compliance** — the embedded `audit.jsonl` carries the full hash
  chain; an auditor can verify it offline.
