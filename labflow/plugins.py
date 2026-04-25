"""Plugin loader for custom extractors and verifiers (v0.5).

Two paths are supported, in priority order:

  1. **Entry-point** discovery — install your plugin as a Python package
     declaring entry-points in the ``labflow.plugins`` group:

     .. code:: toml

        [project.entry-points."labflow.plugins"]
        my_extractor = "my_pkg.module:Extractor"

  2. **Dotted path** — ``LABFLOW_PLUGINS=pkg.mod:obj,pkg2.mod:other`` on
     the env. This is convenient for one-off scripts and tests where
     full package metadata is overkill.

A plugin is any object exposing ``register(api)`` where ``api`` is a
:class:`PluginRegistry` — call ``api.add_extractor(name, fn)``,
``api.add_verifier(name, fn)``, or ``api.on_event(event, handler)`` to
extend the system. Plugin loading is best-effort: a failure logs and
continues so a single bad plugin can't crash the API.
"""
from __future__ import annotations

import importlib
import importlib.metadata as md
import logging
import os
from collections import defaultdict
from typing import Any, Callable

log = logging.getLogger("labflow.plugins")


class PluginRegistry:
    """Mutable registry passed to every plugin's ``register`` hook."""

    def __init__(self):
        self.extractors: dict[str, Callable[[str], Any]] = {}
        self.verifiers: dict[str, Callable[..., float]] = {}
        self.event_handlers: dict[str, list[Callable[..., None]]] = defaultdict(list)
        self.metadata: dict[str, dict] = {}

    # --- Registration API ------------------------------------------------
    def add_extractor(self, name: str, fn: Callable[[str], Any]) -> None:
        if name in self.extractors:
            raise ValueError(f"extractor {name!r} already registered")
        self.extractors[name] = fn

    def add_verifier(self, name: str, fn: Callable[..., float]) -> None:
        if name in self.verifiers:
            raise ValueError(f"verifier {name!r} already registered")
        self.verifiers[name] = fn

    def on_event(self, event: str, handler: Callable[..., None]) -> None:
        self.event_handlers[event].append(handler)

    def declare(self, plugin_name: str, **meta) -> None:
        self.metadata[plugin_name] = dict(meta)

    # --- Dispatch API ----------------------------------------------------
    def emit(self, event: str, **kwargs) -> None:
        for h in self.event_handlers.get(event, []):
            try:
                h(**kwargs)
            except Exception:  # noqa: BLE001
                log.exception("plugin handler for %r failed", event)


_registry: PluginRegistry | None = None


def get_registry() -> PluginRegistry:
    """Return the process-wide registry, loading plugins on first call."""
    global _registry
    if _registry is None:
        _registry = PluginRegistry()
        _load_into(_registry)
    return _registry


def reset_registry_for_tests() -> None:
    """Drop the cached registry. Tests should call this between cases."""
    global _registry
    _registry = None


def _load_into(reg: PluginRegistry) -> None:
    # Entry points first.
    try:
        eps = md.entry_points(group="labflow.plugins")
    except Exception:  # pragma: no cover — older importlib_metadata fallback
        eps = md.entry_points().get("labflow.plugins", [])  # type: ignore
    for ep in eps:
        try:
            obj = ep.load()
            _register_plugin(reg, obj, source=f"entry_point:{ep.name}")
        except Exception:  # noqa: BLE001
            log.exception("failed loading entry-point plugin %r", ep.name)

    # Dotted-path overrides via env.
    raw = os.environ.get("LABFLOW_PLUGINS", "").strip()
    for spec in [s.strip() for s in raw.split(",") if s.strip()]:
        try:
            mod_name, attr = spec.split(":", 1)
            obj = getattr(importlib.import_module(mod_name), attr)
            _register_plugin(reg, obj, source=f"env:{spec}")
        except Exception:  # noqa: BLE001
            log.exception("failed loading dotted-path plugin %r", spec)


def _register_plugin(reg: PluginRegistry, obj: Any, *, source: str) -> None:
    register = getattr(obj, "register", None)
    if not callable(register):
        log.warning("plugin %s has no register(api) callable", source)
        return
    register(reg)
    log.info("plugin loaded: %s", source)
