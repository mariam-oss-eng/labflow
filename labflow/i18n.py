"""Lightweight i18n (v0.9).

A tiny dict-based message catalog with locale negotiation. The rest of the
codebase calls :func:`t` to translate UI strings; ``Accept-Language`` is
honoured at the FastAPI dependency boundary.

We avoid Babel/gettext to keep zero-dep guarantees. The catalogue lives
in this file so contributors can add languages with a single PR.
"""
from __future__ import annotations

import re
from typing import Mapping

from .config import get_settings


# Catalogue: locale -> message_id -> string. Missing keys fall back to en.
_CATALOGUES: Mapping[str, Mapping[str, str]] = {
    "en": {
        "app.title": "LabFlow",
        "app.tagline": "Meeting-to-execution OS for research teams",
        "nav.dashboard": "Dashboard",
        "nav.meetings": "Meetings",
        "nav.tasks": "Tasks",
        "nav.search": "Search",
        "task.status.open": "Open",
        "task.status.closed": "Closed",
        "digest.weekly.title": "Your weekly digest",
        "digest.weekly.empty": "No activity this week.",
        "share.expired": "This share link has expired.",
        "share.passcode_required": "A passcode is required to view this resource.",
    },
    "es": {
        "app.title": "LabFlow",
        "app.tagline": "Sistema operativo de ejecución para equipos de investigación",
        "nav.dashboard": "Panel",
        "nav.meetings": "Reuniones",
        "nav.tasks": "Tareas",
        "nav.search": "Buscar",
        "task.status.open": "Abierta",
        "task.status.closed": "Cerrada",
        "digest.weekly.title": "Tu resumen semanal",
        "digest.weekly.empty": "Sin actividad esta semana.",
        "share.expired": "Este enlace ha caducado.",
        "share.passcode_required": "Se requiere un código de acceso.",
    },
    "fr": {
        "app.title": "LabFlow",
        "app.tagline": "Système d'exécution des décisions pour équipes de recherche",
        "nav.dashboard": "Tableau de bord",
        "nav.meetings": "Réunions",
        "nav.tasks": "Tâches",
        "nav.search": "Recherche",
        "task.status.open": "Ouverte",
        "task.status.closed": "Fermée",
        "digest.weekly.title": "Votre résumé hebdomadaire",
        "digest.weekly.empty": "Aucune activité cette semaine.",
        "share.expired": "Ce lien de partage a expiré.",
        "share.passcode_required": "Un code d'accès est requis.",
    },
}


_HEADER_RE = re.compile(r"\s*([a-zA-Z-]+)(?:\s*;\s*q\s*=\s*([0-9.]+))?\s*")


def supported_locales() -> list[str]:
    settings = get_settings()
    return [loc for loc in (settings.locales or ["en"]) if loc in _CATALOGUES] or ["en"]


def negotiate(accept_language: str | None) -> str:
    """Pick the best supported locale from an ``Accept-Language`` header.

    Implements RFC 7231 §5.3.5: parse ``q=`` weights, sort, pick the highest
    that we support; fall back to the first configured locale.
    """
    sup = supported_locales()
    if not accept_language:
        return sup[0]
    cands: list[tuple[float, str]] = []
    for chunk in accept_language.split(","):
        m = _HEADER_RE.fullmatch(chunk)
        if not m:
            continue
        tag, q = m.group(1), m.group(2)
        try:
            qv = float(q) if q is not None else 1.0
        except ValueError:
            qv = 1.0
        # Try full tag, then primary subtag.
        for variant in (tag.lower(), tag.split("-")[0].lower()):
            if variant in sup:
                cands.append((qv, variant))
                break
    if not cands:
        return sup[0]
    cands.sort(key=lambda x: -x[0])
    return cands[0][1]


def t(key: str, *, locale: str = "en", **fmt) -> str:
    """Translate ``key`` into ``locale`` with optional ``str.format`` args."""
    cat = _CATALOGUES.get(locale) or _CATALOGUES["en"]
    msg = cat.get(key) or _CATALOGUES["en"].get(key, key)
    if fmt:
        try:
            return msg.format(**fmt)
        except (KeyError, IndexError):
            return msg
    return msg


def all_messages(locale: str) -> dict[str, str]:
    """Return the entire catalogue for a locale (English fallback merged)."""
    base = dict(_CATALOGUES["en"])
    base.update(_CATALOGUES.get(locale, {}))
    return base
