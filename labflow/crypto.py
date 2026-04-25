"""Encryption at rest for sensitive transcript data (v0.5).

When ``LABFLOW_DATA_KEY`` is set (urlsafe base64 ``cryptography.fernet``
key) we transparently encrypt :class:`models.Meeting.transcript` and
:class:`models.Meeting.notes` before they're written, and decrypt on
read via SQLAlchemy's ``TypeDecorator`` mechanism. The DB therefore
holds Fernet ciphertext (``gAAAAA…``) — useful for compliance regimes
that require encryption-at-rest separately from full-disk encryption.

Design notes
------------
* **Fernet** is AES-128-CBC + HMAC-SHA256 with timestamped tokens,
  battle-tested and stdlib-adjacent (we already import ``cryptography``
  transitively via uvicorn for HTTP/2 support; on platforms where it
  isn't present the import gracefully degrades to plaintext).
* We never encrypt small fields like decisions/task titles because we
  need to search them. If the threat model demands those too, swap in
  application-level field crypto with deterministic AES-SIV — out of
  scope for now.
* Key rotation is supported via ``LABFLOW_DATA_KEYS`` (comma-separated):
  the first key encrypts new rows, every key in the list can decrypt
  old rows. This is the standard ``MultiFernet`` pattern.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from .config import get_settings

log = logging.getLogger("labflow.crypto")

_FERNET_PREFIX = "gAAAAA"  # urlsafe-b64 of the Fernet version byte 0x80


def _build_fernet():
    """Return a ``Fernet``/``MultiFernet`` instance, or ``None`` when no
    key is configured. Imported lazily so the package can be used without
    cryptography installed."""
    s = get_settings()
    primary = (getattr(s, "data_key", "") or "").strip()
    if not primary:
        return None
    try:
        from cryptography.fernet import Fernet, MultiFernet  # type: ignore
    except ImportError:  # pragma: no cover — depends on env
        log.warning("cryptography not installed; data_key is ignored")
        return None
    keys = [k.strip() for k in primary.split(",") if k.strip()]
    fernets = []
    for k in keys:
        try:
            fernets.append(Fernet(k.encode() if isinstance(k, str) else k))
        except Exception as exc:  # noqa: BLE001
            log.error("invalid Fernet key (skipped): %s", exc)
    if not fernets:
        return None
    return fernets[0] if len(fernets) == 1 else MultiFernet(fernets)


def encrypt(text: str) -> str:
    """Encrypt ``text`` if a key is configured, else return it verbatim."""
    if not text:
        return text
    f = _build_fernet()
    if f is None:
        return text
    return f.encrypt(text.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    """Decrypt ``value`` if it looks like a Fernet token, else return it.

    The "looks like" check (``gAAAAA`` prefix) lets us seamlessly read
    rows that were written before encryption was enabled — important
    for in-place upgrades.
    """
    if not value or not value.startswith(_FERNET_PREFIX):
        return value
    f = _build_fernet()
    if f is None:
        return value
    try:
        return f.decrypt(value.encode("ascii")).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        log.error("decrypt failed; returning ciphertext placeholder: %s", exc)
        return "[encrypted: decrypt failed]"


class EncryptedText(TypeDecorator):
    """SQLAlchemy column type — transparent encryption when a key is set."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Optional[str], dialect: Any) -> Optional[str]:
        if value is None:
            return None
        return encrypt(value)

    def process_result_value(self, value: Optional[str], dialect: Any) -> Optional[str]:
        if value is None:
            return None
        return decrypt(value)
