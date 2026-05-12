# Webhook DLQ, terminal UI, heatmap & key rotation (v0.17)

v0.17 is the **operations release**. The features don't add user-facing
surface — they make running LabFlow at 3am a more pleasant experience.

## At a glance

| Feature | Module | Where |
|---|---|---|
| Webhook dead-letter queue | `labflow.webhook_dlq` | `/api/webhook-dlq{,/stats,/{id}/replay,/{id}/discard}` |
| Terminal UI | `labflow.tui` | `labflow tui [--once] [--team SLUG]` |
| Activity heatmap | `labflow.heatmap` | `/api/heatmap{,.svg}` |
| API key rotation | `labflow.key_rotation` | `/api/keys/{id}/rotate{,/cancel}`, `/api/keys/_sweep-expired` |

## Webhook DLQ

Outbound webhooks retry up to 5 times. After the final attempt without
success, the row is **dead-lettered** (`dead_lettered_at` stamped).

```bash
curl -s localhost:8000/api/webhook-dlq/stats
{"dead": 2, "pending": 0, "success": 47}

curl -s localhost:8000/api/webhook-dlq | jq '.items[].event'
"task.created"
"task.completed"

# Reanimate a delivery — clears dead_lettered_at and resets attempts.
curl -X POST localhost:8000/api/webhook-dlq/123/replay   # admin only

# Acknowledge a permanent failure (audit-only; the row stays).
curl -X POST localhost:8000/api/webhook-dlq/123/discard  # admin only
```

Every `replay` and `discard` emits an audit row; the chain
([ADR-0011](adr/0011-tamper-evident-audit-chain.md)) makes silent
edits detectable. See [ADR-0027](adr/0027-webhook-dlq.md).

## `labflow tui` — ops dashboard in one terminal

```bash
labflow tui                # live, refresh every 1s, press q to quit
labflow tui --once         # one snapshot, exit (great in CI / cron)
labflow tui --team alpha   # specific team
```

The renderer is hand-rolled ANSI — no `curses`, no `rich`, nothing
to install. Layout:

```
LabFlow 0.17.0 — team default

Tasks  open:    8  in-progress:    3  blocked:    1  done:    42

Top owners (open + in_progress)
  alice                  ▇▇▇▇▇▇▇ 7
  bob                    ▇▇▇ 3
  carol                  ▇ 1

Webhook DLQ  dead: 0  pending: 0  success: 47

Activity (last 12 weeks)  (· empty  █ busy)
  ··░░▒▒▓▓████▓▓▒▒░░··················
```

See [ADR-0028](adr/0028-tui.md).

## Activity heatmap

Daily counts derived from `audit_events.created_at` — works
retroactively on any team.

```bash
# JSON
curl -s 'localhost:8000/api/heatmap?days=90' | jq '.counts | length'
90

# Standalone SVG, embeddable in a README via <img>
curl -s 'localhost:8000/api/heatmap.svg?days=180' > heatmap.svg
```

The SVG is self-contained, GitHub-style 5-bucket palette, no JS
required. Embed it anywhere `<img>` works. See
[ADR-0029](adr/0029-activity-heatmap.md).

## API key rotation with grace

Rotate without downtime:

```bash
# 1. Mint a successor; old key valid for 24h by default.
curl -X POST localhost:8000/api/keys/7/rotate -d '{"grace_hours": 48}'
{
  "new_key_id": 8, "old_key_id": 7,
  "token": "lfk_xxxxx",                # plaintext — stored ONCE
  "grace_until": "2026-05-14T09:00:00+00:00"
}

# 2. Redeploy clients with the new token. Both keys work.

# 3. Sweep — runs the cron-friendly sweeper to revoke any keys
#    whose grace window has elapsed. Idempotent.
curl -X POST localhost:8000/api/keys/_sweep-expired
{"revoked": 1}

# Aborting an in-progress rotation:
curl -X POST localhost:8000/api/keys/7/rotate/cancel
```

* `grace_hours` is bounded `[1, 720]`. Beyond 30 days you should
  treat the old key as compromised and mint fresh.
* Plaintext is shown **exactly once** — same SHA-256 storage as
  bootstrap keys, see `auth.hash_api_key` for the rationale.

See [ADR-0030](adr/0030-api-key-rotation.md).
