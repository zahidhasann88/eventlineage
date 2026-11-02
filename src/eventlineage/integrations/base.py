"""Optional integration framework.

Integrations are the *only* part of EventLineage that talks to infrastructure, and
they are strictly optional: the core engine never imports them, the CLI only
loads the one you ask for, and every adapter accepts an injected client so it
can be tested without a cluster (see ``tests/unit/test_integrations.py``).

Each adapter returns plain catalog models (:class:`Topic`, :class:`Event`), so
whatever it discovers can be merged into the same pipeline as local files.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from eventlineage.exceptions import IntegrationError, IntegrationUnavailableError
from eventlineage.models.base import EGBaseModel
from eventlineage.models.common import SourceLocation
from eventlineage.models.event import Event
from eventlineage.models.topic import Topic

_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_environment(value: Any) -> Any:
    """Replace ``${NAME}`` in configuration values, so secrets stay out of the file."""
    if isinstance(value, str):

        def lookup(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise IntegrationError(f"environment variable {name!r} is not set")
            return os.environ[name]

        return _ENV_REFERENCE.sub(lookup, value)
    if isinstance(value, Mapping):
        return {key: expand_environment(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [expand_environment(item) for item in value]
    return value


@dataclass(slots=True)
class IntegrationResult:
    """Fragments collected from an integration."""

    topics: list[Topic] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add_topic(self, topic: Topic) -> Topic:
        """Append a discovered topic and return it."""
        self.topics.append(topic)
        return topic

    def add_event(self, event: Event) -> Event:
        """Append a discovered event and return it."""
        self.events.append(event)
        return event

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready representation."""
        return {
            "topics": [topic.model_dump(mode="json") for topic in self.topics],
            "events": [event.model_dump(mode="json") for event in self.events],
            "notes": self.notes,
        }


class IntegrationStatus(EGBaseModel):
    """Whether an integration can be used in this environment."""

    name: str
    available: bool
    requires: str | None = None
    reason: str | None = None
    description: str | None = None


class Integration(ABC):
    """Base class for infrastructure adapters."""

    #: Integration name used in configuration and on the CLI.
    name: ClassVar[str] = ""
    #: Optional dependency required for live access (``None`` means stdlib only).
    requires: ClassVar[str | None] = None
    description: ClassVar[str] = ""

    def __init__(self, **options: Any) -> None:
        self.options = options

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> Integration:
        """Build the adapter from its ``integrations.*`` configuration section."""
        try:
            return cls(**expand_environment(settings))
        except TypeError as exc:
            raise IntegrationError(
                f"invalid settings for the {cls.name!r} integration: {exc}",
                hint="see docs/configuration.md",
            ) from exc

    @classmethod
    def dependency_available(cls) -> bool:
        """Whether the optional dependency is installed."""
        if cls.requires is None:
            return True
        module_name = cls.requires.replace("-", "_")
        return importlib.util.find_spec(module_name) is not None

    @classmethod
    def status(cls) -> IntegrationStatus:
        """Human-readable availability with install guidance."""
        available = cls.dependency_available()
        return IntegrationStatus(
            name=cls.name,
            available=available,
            requires=cls.requires,
            reason=None if available else f"install the optional dependency {cls.requires!r}",
            description=cls.description,
        )

    @classmethod
    def require_dependency(cls) -> Any:
        """Import and return the optional dependency module."""
        if cls.requires is None:
            raise IntegrationError(f"{cls.name} declares no optional dependency")
        try:
            return importlib.import_module(cls.requires.replace("-", "_"))
        except ImportError as exc:
            raise IntegrationUnavailableError(cls.name, cls.requires) from exc

    @abstractmethod
    def collect(self) -> IntegrationResult:
        """Fetch catalog fragments from the external system."""

    def export(self, result: IntegrationResult) -> dict[str, Any]:
        """Render ``result`` as a file ``eventlineage scan`` reads back, sorted and stable."""
        broker = result.topics[0].broker if result.topics else self.name
        return {
            "broker": broker,
            "topics": [
                {
                    "name": topic.name,
                    "partitions": topic.partitions,
                    "replication_factor": topic.replication_factor,
                    "config": dict(sorted(topic.config.items())),
                    "consumers": sorted(topic.consumers),
                    "description": topic.description,
                }
                for topic in sorted(result.topics, key=lambda topic: topic.name)
            ],
        }

    def source(self, path: str) -> SourceLocation:
        """Build a source location for a remote record."""
        return SourceLocation(path=path, plugin=self.name)


__all__ = ["Integration", "IntegrationResult", "IntegrationStatus", "expand_environment"]
