"""Plugin registry: lookup, filtering and third-party loading."""

from __future__ import annotations

import logging
from importlib import metadata
from typing import Any

from eventlineage.discovery.base import DiscoveryPlugin

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "eventlineage.discovery"


class PluginRegistry:
    """An ordered collection of discovery plugin classes."""

    def __init__(self) -> None:
        self._plugins: dict[str, type[DiscoveryPlugin]] = {}
        self._entry_points_loaded = False

    def register(
        self, plugin: type[DiscoveryPlugin], *, replace: bool = False
    ) -> type[DiscoveryPlugin]:
        """Register ``plugin`` (usable as a class decorator too)."""
        name = getattr(plugin, "name", "")
        if not name:
            raise ValueError(f"{plugin.__name__} must define a non-empty 'name'")
        if name in self._plugins and not replace:
            existing = self._plugins[name]
            if existing is plugin:
                return plugin
            raise ValueError(f"a discovery plugin named {name!r} is already registered")
        if not getattr(plugin, "patterns", ()) and plugin.detect is DiscoveryPlugin.detect:
            logger.debug("plugin %s declares no patterns; it will be asked about every file", name)
        self._plugins[name] = plugin
        return plugin

    def unregister(self, name: str) -> None:
        """Remove a plugin by name (no-op when absent)."""
        self._plugins.pop(name, None)

    def get(self, name: str) -> type[DiscoveryPlugin]:
        """Return a registered plugin class by name."""
        if name not in self._plugins:
            raise KeyError(name)
        return self._plugins[name]

    def names(self) -> tuple[str, ...]:
        """Registered plugin names, sorted."""
        return tuple(sorted(self._plugins))

    def classes(self) -> tuple[type[DiscoveryPlugin], ...]:
        """Registered plugin classes, ordered by name."""
        return tuple(self._plugins[name] for name in sorted(self._plugins))

    def __len__(self) -> int:
        return len(self._plugins)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._plugins

    def active(
        self, *, enabled: tuple[str, ...] = (), disabled: tuple[str, ...] = ()
    ) -> list[DiscoveryPlugin]:
        """Instantiate the plugins selected by configuration.

        ``enabled`` acts as an allow-list when non-empty. Unknown names are
        logged rather than raising: a stale config entry should not make a scan
        impossible.
        """
        self.load_entry_points()
        unknown = {*enabled, *disabled} - set(self._plugins)
        for name in sorted(unknown):
            logger.warning("configured discovery plugin %r is not installed; ignoring", name)
        selected: list[DiscoveryPlugin] = []
        for name in self.names():
            if enabled and name not in enabled:
                continue
            if name in disabled:
                continue
            selected.append(self._plugins[name]())
        selected.sort(key=lambda plugin: (-plugin.priority, plugin.name))
        return selected

    def load_entry_points(self) -> None:
        """Load plugins advertised by installed packages (once)."""
        if self._entry_points_loaded:
            return
        self._entry_points_loaded = True
        try:
            entry_points = metadata.entry_points(group=ENTRY_POINT_GROUP)
        except Exception:  # pragma: no cover - metadata backend failure
            logger.debug("could not read entry points", exc_info=True)
            return
        for entry_point in entry_points:
            try:
                loaded: Any = entry_point.load()
            except Exception:  # pragma: no cover - broken third-party package
                logger.warning(
                    "failed to load discovery plugin %r", entry_point.name, exc_info=True
                )
                continue
            candidates = loaded if isinstance(loaded, (list, tuple)) else [loaded]
            for candidate in candidates:
                if not (isinstance(candidate, type) and issubclass(candidate, DiscoveryPlugin)):
                    logger.warning(
                        "entry point %r did not resolve to a DiscoveryPlugin subclass",
                        entry_point.name,
                    )
                    continue
                if candidate.name in self._plugins:
                    continue
                self.register(candidate)
                logger.debug("registered discovery plugin %r from entry point", candidate.name)


_DEFAULT_REGISTRY = PluginRegistry()


def default_registry() -> PluginRegistry:
    """The registry used by the CLI and the SDK unless one is passed explicitly."""
    return _DEFAULT_REGISTRY


def register_plugin(
    plugin: type[DiscoveryPlugin], *, replace: bool = False
) -> type[DiscoveryPlugin]:
    """Register a plugin on the default registry (decorator-friendly)."""
    return _DEFAULT_REGISTRY.register(plugin, replace=replace)


def available_plugins() -> tuple[str, ...]:
    """Names of all plugins registered on the default registry."""
    _DEFAULT_REGISTRY.load_entry_points()
    return _DEFAULT_REGISTRY.names()


def get_plugin(name: str) -> type[DiscoveryPlugin]:
    """Return a plugin class from the default registry."""
    _DEFAULT_REGISTRY.load_entry_points()
    return _DEFAULT_REGISTRY.get(name)


__all__ = [
    "ENTRY_POINT_GROUP",
    "PluginRegistry",
    "available_plugins",
    "default_registry",
    "get_plugin",
    "register_plugin",
]
