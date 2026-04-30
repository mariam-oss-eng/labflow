"""Plugin marketplace catalogue (v0.9).

A team-scoped install/enable/disable catalogue for plugins. Distinct from
:mod:`labflow.plugins` (which is the *runtime* loader for in-process
extensions); this module is the *operator-facing* lifecycle.

A plugin is described by a *manifest* — a JSON document with a fixed schema.
Operators install a plugin by submitting a manifest plus its SHA-256 hash;
the system verifies the hash and persists the manifest. *Enabling* flips
a flag the runtime (or a feature gate) can consult.

Code execution is intentionally **out of scope** for v0.9 — see ADR-0010.
This release ships the catalogue + lifecycle layer so we can iterate on
manifest design before tackling sandboxing.

Manifest schema
---------------
::

    {
      "name": "my-plugin",
      "version": "1.0.0",
      "author": "Example Co",
      "description": "Adds custom report formats.",
      "labflow_min_version": "0.9.0",
      "permissions": ["read", "webhook:emit"],
      "hooks": ["task.transition", "meeting.finalized"]
    }
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit as audit_mod, models
from .errors import ConflictError, NotFoundError, ValidationError
from .scopes import VALID_SCOPES

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.-]+)?$")
_VALID_HOOKS = frozenset({
    "task.transition", "task.created", "task.closed", "task.sla.breach",
    "meeting.finalized", "decision.created", "comment.added",
})


def validate_manifest(manifest: dict[str, Any]) -> None:
    if not isinstance(manifest, dict):
        raise ValidationError("manifest must be an object")
    name = manifest.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name or ""):
        raise ValidationError("manifest.name must match [a-z0-9][a-z0-9-]{1,62}")
    version = manifest.get("version")
    if not isinstance(version, str) or not _VERSION_RE.match(version or ""):
        raise ValidationError("manifest.version must be semver (e.g. 1.0.0)")
    author = manifest.get("author")
    if not isinstance(author, str) or not author.strip():
        raise ValidationError("manifest.author required")
    perms = manifest.get("permissions", [])
    if not isinstance(perms, list) or any(p not in VALID_SCOPES for p in perms):
        raise ValidationError(
            f"manifest.permissions must be a subset of {sorted(VALID_SCOPES)}"
        )
    hooks = manifest.get("hooks", [])
    if not isinstance(hooks, list) or any(h not in _VALID_HOOKS for h in hooks):
        raise ValidationError(
            f"manifest.hooks must be a subset of {sorted(_VALID_HOOKS)}"
        )


def manifest_hash(manifest: dict[str, Any]) -> str:
    """Canonical JSON SHA-256 — sorted keys, compact separators."""
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def install(
    sess: Session, *, team_id: int, manifest: dict[str, Any],
    expected_sha256: str | None = None, actor: str = "system",
) -> models.Plugin:
    validate_manifest(manifest)
    sha = manifest_hash(manifest)
    if expected_sha256 is not None and expected_sha256.lower() != sha:
        raise ValidationError(
            f"manifest hash mismatch: got {sha}, expected {expected_sha256}"
        )
    name = manifest["name"]
    existing = sess.execute(
        select(models.Plugin).where(
            models.Plugin.team_id == team_id, models.Plugin.name == name,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(f"plugin {name!r} already installed")
    p = models.Plugin(
        team_id=team_id, name=name, version=manifest["version"],
        author=manifest["author"],
        description=manifest.get("description"),
        manifest_json=json.dumps(manifest, sort_keys=True),
        manifest_sha256=sha,
        enabled=False,
    )
    sess.add(p)
    sess.flush()
    audit_mod.record(sess, team_id=team_id, actor=actor,
                     action="plugin.install",
                     entity_type="plugin", entity_id=p.id,
                     metadata={"name": name, "version": p.version,
                               "sha256": sha})
    return p


def set_enabled(sess: Session, *, team_id: int, name: str, enabled: bool,
                actor: str = "system") -> models.Plugin:
    p = _find(sess, team_id=team_id, name=name)
    p.enabled = enabled
    audit_mod.record(sess, team_id=team_id, actor=actor,
                     action="plugin.enable" if enabled else "plugin.disable",
                     entity_type="plugin", entity_id=p.id,
                     metadata={"name": name})
    return p


def uninstall(sess: Session, *, team_id: int, name: str,
              actor: str = "system") -> None:
    p = _find(sess, team_id=team_id, name=name)
    pid = p.id
    sess.delete(p)
    audit_mod.record(sess, team_id=team_id, actor=actor,
                     action="plugin.uninstall",
                     entity_type="plugin", entity_id=pid,
                     metadata={"name": name})


def list_plugins(sess: Session, *, team_id: int) -> list[models.Plugin]:
    return list(sess.execute(
        select(models.Plugin).where(models.Plugin.team_id == team_id)
        .order_by(models.Plugin.name)
    ).scalars())


def is_enabled(sess: Session, *, team_id: int, name: str) -> bool:
    p = sess.execute(
        select(models.Plugin).where(
            models.Plugin.team_id == team_id, models.Plugin.name == name,
        )
    ).scalar_one_or_none()
    return bool(p and p.enabled)


def _find(sess: Session, *, team_id: int, name: str) -> models.Plugin:
    p = sess.execute(
        select(models.Plugin).where(
            models.Plugin.team_id == team_id, models.Plugin.name == name,
        )
    ).scalar_one_or_none()
    if p is None:
        raise NotFoundError(f"plugin {name!r} not installed")
    return p
