"""Append-only audit log.

Every meaningful state transition is recorded with the actor, action,
entity type/id, and an optional JSON metadata blob. Audit rows are never
mutated — corrections are made by appending a new row.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from . import models


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
    """Append an audit row. Caller is responsible for the surrounding txn."""
    evt = models.AuditEvent(
        team_id=team_id,
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        metadata_json=json.dumps(metadata, sort_keys=True) if metadata else None,
    )
    sess.add(evt)
    sess.flush()
    return evt
