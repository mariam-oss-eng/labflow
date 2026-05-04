"""Append-only audit log with tamper-evident hash chain (v0.10).

Every meaningful state transition is recorded with the actor, action,
entity type/id, and an optional JSON metadata blob. Audit rows are never
mutated — corrections are made by appending a new row.

Since v0.10 each row also carries a hash chain pointer:

    entry_hash = sha256(prev_hash || canonical_payload)

where ``prev_hash`` is the previous row's ``entry_hash`` for the same
team (or 64 zeros for the genesis row). This lets ``audit.verify_chain``
detect *silent rewrites* of historical rows: any in-place edit breaks
the chain at the edited row and every row after it.

The chain is *per-team* so one tenant's tampering can't silently
invalidate another's history; each team's chain stands or falls alone.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models

GENESIS_HASH = "0" * 64


def _canonical_payload(
    *,
    team_id: int,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: int | None,
    metadata_json: str | None,
    created_at_iso: str,
) -> str:
    """Stable string form of an audit row's hashable fields.

    JSON with ``sort_keys`` and no whitespace makes the payload
    deterministic across Python versions and platforms — critical for a
    hash-chain whose verification must be reproducible.

    The ``created_at_iso`` argument is normalised to its naive form
    (``YYYY-MM-DDTHH:MM:SS[.ffffff]`` with no timezone suffix) because
    SQLite's ``DateTime`` column drops timezone information on read; we
    must hash the *post-roundtrip* representation so signing and
    verification produce identical bytes.
    """
    if "+" in created_at_iso:
        created_at_iso = created_at_iso.split("+", 1)[0]
    if created_at_iso.endswith("Z"):
        created_at_iso = created_at_iso[:-1]
    return json.dumps(
        {
            "team_id": team_id,
            "actor": actor,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "metadata_json": metadata_json,
            "created_at": created_at_iso,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _compute_entry_hash(prev_hash: str, payload: str) -> str:
    h = hashlib.sha256()
    h.update(prev_hash.encode("ascii"))
    h.update(b"\n")
    h.update(payload.encode("utf-8"))
    return h.hexdigest()


def _last_hash_for_team(sess: Session, team_id: int) -> str:
    row = sess.execute(
        select(models.AuditEvent.entry_hash, models.AuditEvent.id)
        .where(models.AuditEvent.team_id == team_id)
        .order_by(models.AuditEvent.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return GENESIS_HASH
    last_hash, _ = row
    # Pre-v0.10 rows have NULL hashes — treat as genesis for forward chain.
    return last_hash or GENESIS_HASH


def record(
    sess: Session,
    *,
    team_id: int,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    actor: str = "system",
    metadata: dict[str, Any] | None = None,
) -> models.AuditEvent:
    """Append an audit row. Caller is responsible for the surrounding txn.

    Computes ``prev_hash`` and ``entry_hash`` so the row participates in
    the team's tamper-evident chain.
    """
    metadata_json = json.dumps(metadata, sort_keys=True) if metadata else None
    prev_hash = _last_hash_for_team(sess, team_id)
    evt = models.AuditEvent(
        team_id=team_id,
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        metadata_json=metadata_json,
        prev_hash=prev_hash,
    )
    sess.add(evt)
    sess.flush()  # populates created_at via default
    payload = _canonical_payload(
        team_id=evt.team_id,
        actor=evt.actor,
        action=evt.action,
        entity_type=evt.entity_type,
        entity_id=evt.entity_id,
        metadata_json=evt.metadata_json,
        created_at_iso=evt.created_at.isoformat(),
    )
    evt.entry_hash = _compute_entry_hash(prev_hash, payload)
    sess.flush()
    # Try to dispatch automation rules. We import lazily to avoid a
    # circular import at module-load time and we *swallow* errors so a
    # broken rule never blocks the underlying state change.
    try:
        from . import automation as _auto
        _auto.dispatch(sess, team_id=team_id, event=action, entity_type=entity_type,
                       entity_id=entity_id, metadata=metadata or {})
    except Exception:  # pragma: no cover - defensive
        pass
    return evt


def verify_chain(sess: Session, *, team_id: int) -> dict[str, Any]:
    """Walk the team's audit chain and report integrity.

    Returns a dict::

        {"ok": bool, "checked": int, "first_break_id": int|None,
         "reason": str|None, "skipped_legacy": int}

    A row with NULL ``entry_hash`` is counted as ``skipped_legacy`` and
    treated as a genesis pivot: the next row's chain is verified against
    the most recent computed hash (or genesis if none exists yet). This
    keeps verification meaningful for upgraded databases.
    """
    rows = (
        sess.execute(
            select(models.AuditEvent)
            .where(models.AuditEvent.team_id == team_id)
            .order_by(models.AuditEvent.id.asc())
        )
        .scalars()
        .all()
    )
    expected_prev = GENESIS_HASH
    checked = 0
    skipped_legacy = 0
    for row in rows:
        if row.entry_hash is None:
            skipped_legacy += 1
            # Re-anchor: pre-v0.10 rows are unverifiable; reset to genesis
            # so the first hashed row is the new chain start.
            expected_prev = GENESIS_HASH
            continue
        if row.prev_hash != expected_prev:
            return {
                "ok": False, "checked": checked, "first_break_id": row.id,
                "reason": f"prev_hash mismatch at id={row.id}: "
                          f"expected {expected_prev[:8]}, got "
                          f"{(row.prev_hash or '')[:8]}",
                "skipped_legacy": skipped_legacy,
            }
        payload = _canonical_payload(
            team_id=row.team_id, actor=row.actor, action=row.action,
            entity_type=row.entity_type, entity_id=row.entity_id,
            metadata_json=row.metadata_json,
            created_at_iso=row.created_at.isoformat(),
        )
        recomputed = _compute_entry_hash(row.prev_hash or GENESIS_HASH, payload)
        if recomputed != row.entry_hash:
            return {
                "ok": False, "checked": checked, "first_break_id": row.id,
                "reason": f"entry_hash mismatch at id={row.id}",
                "skipped_legacy": skipped_legacy,
            }
        expected_prev = row.entry_hash
        checked += 1
    return {
        "ok": True, "checked": checked, "first_break_id": None,
        "reason": None, "skipped_legacy": skipped_legacy,
    }
