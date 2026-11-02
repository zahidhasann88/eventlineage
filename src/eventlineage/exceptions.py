"""Exception hierarchy.

Every error EventLineage raises deliberately derives from :class:`EventLineageError`
so SDK users can write ``except EventLineageError`` and get everything, while the
CLI can map each subclass to a meaningful exit code.
"""

from __future__ import annotations

from pathlib import Path


class EventLineageError(Exception):
    """Base class for all EventLineage errors."""

    exit_code: int = 3

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message}\nHint: {self.hint}"
        return self.message


class UsageError(EventLineageError):
    """The user asked for something impossible (bad flag combination, ...)."""

    exit_code = 2


class ConfigError(EventLineageError):
    """``eventlineage.yaml`` (or environment overrides) is invalid."""

    exit_code = 2

    def __init__(
        self, message: str, *, path: Path | str | None = None, hint: str | None = None
    ) -> None:
        super().__init__(f"{path}: {message}" if path else message, hint=hint)
        self.path = path


class ProjectNotFoundError(ConfigError):
    """No project root could be located."""

    def __init__(self, start: str) -> None:
        super().__init__(
            f"no EventLineage project found from {start!r}: eventlineage.yaml is missing",
            hint="run 'eventlineage init' to create a configuration file",
        )


class DiscoveryError(EventLineageError):
    """A discovery plugin failed irrecoverably."""

    def __init__(
        self, message: str, *, plugin: str | None = None, path: Path | str | None = None
    ) -> None:
        prefix = "/".join(part for part in (plugin, str(path) if path else None) if part)
        super().__init__(f"{prefix}: {message}" if prefix else message)
        self.plugin = plugin
        self.path = path


class ParseError(EventLineageError):
    """A source file could not be parsed."""

    def __init__(
        self, message: str, *, path: Path | str | None = None, line: int | None = None
    ) -> None:
        location = ""
        if path is not None:
            location = f"{path}:{line}: " if line else f"{path}: "
        super().__init__(f"{location}{message}")
        self.path = path
        self.line = line


class SchemaError(EventLineageError):
    """A schema is structurally invalid or uses unsupported constructs."""


class CompatibilityError(EventLineageError):
    """Two schemas could not be compared (e.g. different dialects)."""

    exit_code = 2


class GraphError(EventLineageError):
    """Graph construction or traversal failure."""


class IntegrationError(EventLineageError):
    """An optional integration failed."""


class IntegrationUnavailableError(IntegrationError):
    """An optional dependency for an integration is not installed."""

    def __init__(self, integration: str, package: str) -> None:
        super().__init__(
            f"the '{integration}' integration requires the optional dependency {package!r}",
            hint=f"pip install 'eventlineage[{integration}]'",
        )
        self.integration = integration
        self.package = package


class SymbolNotFoundError(EventLineageError):
    """A symbol could not be resolved in the catalog."""

    exit_code = 2

    def __init__(self, symbol: str, kind: str, *, candidates: tuple[str, ...] = ()) -> None:
        message = f"unknown {kind}: {symbol!r}"
        if candidates:
            message += f" (did you mean: {', '.join(sorted(candidates)[:5])}?)"
        commands = " or ".join(f"'eventlineage {part}s'" for part in kind.split(" or "))
        super().__init__(message, hint=f"run {commands} to list what is available")
        self.symbol = symbol
        self.kind = kind


__all__ = [
    "CompatibilityError",
    "ConfigError",
    "DiscoveryError",
    "EventLineageError",
    "GraphError",
    "IntegrationError",
    "IntegrationUnavailableError",
    "ParseError",
    "ProjectNotFoundError",
    "SchemaError",
    "SymbolNotFoundError",
    "UsageError",
]
