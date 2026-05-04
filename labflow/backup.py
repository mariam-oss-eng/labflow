"""Encrypted, signed full-team backup & restore (v0.10).

A backup is a JSON document of every row owned by a single team plus a
header describing the schema version, and an HMAC-SHA256 signature
computed with the operator's signing secret. The signed envelope is::

    {"header": {...}, "payload": {...}, "signature": "<hex>"}

Where ``signature = hmac_sha256(secret, canonical_json(header)+canonical_json(payload))``.

Design notes
------------
* The backup is **opaque text** by default, but is *integrity-protected*
  via HMAC. We don't add an encryption layer here because the operator
  can pipe the signed JSON through any file-level encryption tool they
  already trust (age, gpg, kms-encrypt). Mixing in a half-baked AEAD
  would be worse than letting them compose proper tooling.
* Restore is **two-phase**: a preview returns counts and conflicts
  without writing; an apply takes an explicit ``mode`` (``new_team`` or
  ``merge``). ``new_team`` is the safe default — it creates a fresh
  team and inserts everything fresh.
* We restore only data tables — system tables (``alembic_version``,
  ``api_keys``, ``audit_events.entry_hash``) are deliberately *not*
  brought across. The new team's audit chain re-anchors at restore.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .errors import ConflictError, NotFoundError, ValidationError
from .time_utils import now_utc

SCHEMA_VERSION = "1"

# Tables exported in the backup envelope. Order matters on restore so
# foreign keys can be satisfied — owners and meetings before tasks, etc.
EXPORT_TABLES: list[tuple[str, type]] = [
    ("owners", models.Owner),
    ("meetings", models.Meeting),
    ("decisions", models.Decision),
    ("tasks", models.Task),
    ("experiments", models.Experiment),
    ("assumptions", models.Assumption),
    ("blockers", models.Blocker),
    ("evidence", models.Evidence),
    ("workflows", models.Workflow),
    ("sprints", models.Sprint),
    ("comments", models.Comment),
    ("wiki_pages", models.WikiPage),
]


def _row_to_dict(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in row.__table__.columns:
        val = getattr(row, col.name)
        if isinstance(val, datetime):
            val = val.isoformat()
        out[col.name] = val
    return out


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      default=str)


def _sign(secret: str, header: dict, payload: dict) -> str:
    body = (_canonical(header) + _canonical(payload)).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def make_backup(sess: Session, *, team_id: int, secret: str) -> dict:
    """Build a signed backup envelope for ``team_id``.

    Raises :class:`NotFoundError` if the team is missing.
    """
    if not secret:
        raise ValidationError("backup signing secret must be non-empty")
    team = sess.get(models.Team, team_id)
    if team is None:
        raise NotFoundError(f"team {team_id} not found")
    payload: dict[str, Any] = {"team": _row_to_dict(team), "tables": {}}
    for name, model in EXPORT_TABLES:
        rows = sess.execute(
            select(model).where(model.team_id == team_id)
        ).scalars().all()
        payload["tables"][name] = [_row_to_dict(r) for r in rows]
    header = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_utc().isoformat(),
        "team_slug": team.slug,
        "row_count": sum(len(v) for v in payload["tables"].values()),
    }
    signature = _sign(secret, header, payload)
    return {"header": header, "payload": payload, "signature": signature}


def verify_backup(envelope: dict, *, secret: str) -> bool:
    """Constant-time signature check.

    Returns ``True`` iff the envelope's ``signature`` matches a fresh
    HMAC over its ``header`` and ``payload`` with the supplied secret.
    """
    if not isinstance(envelope, dict):
        return False
    header = envelope.get("header")
    payload = envelope.get("payload")
    sig = envelope.get("signature")
    if not isinstance(header, dict) or not isinstance(payload, dict):
        return False
    if not isinstance(sig, str):
        return False
    expected = _sign(secret, header, payload)
    return hmac.compare_digest(expected, sig)


def preview_restore(envelope: dict, *, secret: str) -> dict:
    """Return a structural summary of a backup envelope, no DB writes."""
    if not verify_backup(envelope, secret=secret):
        raise ValidationError("backup signature did not verify")
    payload = envelope["payload"]
    counts = {name: len(rows) for name, rows in payload["tables"].items()}
    return {
        "header": envelope["header"],
        "counts": counts,
        "total_rows": sum(counts.values()),
    }


def restore_into_new_team(
    sess: Session, *, envelope: dict, secret: str, new_slug: str,
) -> dict:
    """Apply ``envelope`` into a freshly-created team.

    Returns ``{"team_id": int, "team_slug": str, "inserted": dict[name, count]}``.
    Raises :class:`ConflictError` if ``new_slug`` already exists.
    """
    if not verify_backup(envelope, secret=secret):
        raise ValidationError("backup signature did not verify")
    if not new_slug:
        raise ValidationError("new_slug is required")
    existing = sess.execute(
        select(models.Team).where(models.Team.slug == new_slug)
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(f"team slug '{new_slug}' already exists")

    payload = envelope["payload"]
    src_team = payload.get("team", {})
    new_team = models.Team(slug=new_slug, name=src_team.get("name", new_slug))
    sess.add(new_team)
    sess.flush()

    inserted: dict[str, int] = {}
    # Map old PK -> new PK per table so FK references can be rewritten.
    pk_map: dict[str, dict[int, int]] = {name: {} for name, _ in EXPORT_TABLES}

    for name, model in EXPORT_TABLES:
        rows = payload["tables"].get(name, [])
        for row in rows:
            new_row_data = _remap_row(row, name, pk_map, new_team.id)
            obj = model(**new_row_data)
            sess.add(obj)
            sess.flush()
            old_id = row.get("id")
            if old_id is not None:
                pk_map[name][int(old_id)] = obj.id
        inserted[name] = len(rows)

    sess.flush()
    return {
        "team_id": new_team.id,
        "team_slug": new_team.slug,
        "inserted": inserted,
    }


def _remap_row(
    row: dict, table_name: str, pk_map: dict[str, dict[int, int]],
    new_team_id: int,
) -> dict:
    """Rewrite FK columns + scrub fields that must not be carried across.

    Datetime columns were JSON-serialized to ISO-8601 strings on backup;
    SQLAlchemy's SQLite driver only accepts ``datetime`` objects, so we
    parse known datetime columns back here.
    """
    out = dict(row)
    out.pop("id", None)
    # Always rebind to the new team.
    if "team_id" in out:
        out["team_id"] = new_team_id
    # Rewrite well-known FK columns using the pk_map we've built so far.
    fk_columns = {
        "meeting_id": "meetings",
        "owner_id": "owners",
        "task_id": "tasks",
        "decision_id": "decisions",
        "page_id": "wiki_pages",
        "sprint_id": "sprints",
        "workflow_id": "workflows",
    }
    for col, src_table in fk_columns.items():
        if col in out and out[col] is not None:
            mapped = pk_map.get(src_table, {}).get(int(out[col]))
            # If we don't have a mapping (the referenced table wasn't in
            # the export, or the row was missing), set to None so the
            # row stays valid where the column is nullable.
            out[col] = mapped
    # Datetime round-trip: JSON cannot carry datetime objects so the
    # backup stored them as ISO strings; SQLAlchemy needs real datetimes.
    DT_COLS = {
        "created_at", "updated_at", "occurred_at", "due_date", "closed_at",
        "starts_at", "ends_at", "installed_at", "last_used_at",
        "last_fired_at", "sla_breach_at", "revoked_at",
    }
    for col in list(out.keys()):
        if col in DT_COLS and isinstance(out[col], str):
            try:
                # ``fromisoformat`` accepts both naive and tz-aware ISO
                # strings produced by ``datetime.isoformat``.
                out[col] = datetime.fromisoformat(out[col])
            except ValueError:
                out[col] = None
    return out
