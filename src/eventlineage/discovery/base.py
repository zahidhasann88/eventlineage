"""Discovery plugin API.

A discovery plugin turns one file into catalog fragments. Plugins are selected
per file by :meth:`DiscoveryPlugin.detect`, which returns a confidence score:
several plugins can claim a ``.json`` file, and the best-scoring one wins. This
keeps format detection in the plugins themselves instead of in the core engine,
which is what makes third-party formats a drop-in extension.

Writing a plugin
----------------
Subclass :class:`DiscoveryPlugin`, declare ``name``, ``patterns`` and
``detect``, implement :meth:`discover`, and register it::

    from eventlineage.discovery import DiscoveryPlugin, DiscoveryResult, register_plugin

    class MyFormatPlugin(DiscoveryPlugin):
        name = "my-format"
        patterns = ("*.evt",)

        def discover(self, path, ctx):
            result = DiscoveryResult()
            result.add_event(Event(id="x", name="x"))
            return result

    register_plugin(MyFormatPlugin)

Third-party packages can also expose the ``eventlineage.discovery`` entry-point
group; those plugins are loaded automatically.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import yaml

from eventlineage.config import Config
from eventlineage.exceptions import DiscoveryError, ParseError
from eventlineage.models.common import Diagnostic, Severity, SourceLocation
from eventlineage.models.dependency import EventRelationship
from eventlineage.models.event import Event
from eventlineage.models.participant import Consumer, Producer, Service
from eventlineage.models.topic import Topic
from eventlineage.utils.yaml_io import load_yaml

#: Maximum number of characters passed to :meth:`DiscoveryPlugin.detect`.
HEAD_SIZE = 8192

_JSON_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[{}\[\]]')


@dataclass(slots=True)
class DiscoveryContext:
    """Everything a plugin may need while reading a file."""

    root: Path
    config: Config
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("eventlineage.discovery")
    )
    #: Other files the current file was built from; the scan cache hashes them too.
    references: set[Path] = field(default_factory=set)

    def reference(self, path: Path) -> None:
        """Record that the file being discovered depends on ``path``."""
        self.references.add(path.resolve())

    def relative(self, path: Path) -> str:
        """Path relative to the project root, using forward slashes."""
        try:
            return path.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()


@dataclass(slots=True)
class DiscoveryResult:
    """Catalog fragments produced by one plugin run."""

    events: list[Event] = field(default_factory=list)
    topics: list[Topic] = field(default_factory=list)
    services: list[Service] = field(default_factory=list)
    relationships: list[EventRelationship] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def add_event(self, event: Event) -> Event:
        """Append an event fragment and return it."""
        self.events.append(event)
        return event

    def add_topic(self, topic: Topic) -> Topic:
        """Append a topic fragment and return it."""
        self.topics.append(topic)
        return topic

    def add_service(self, service: Service) -> Service:
        """Append a service fragment and return it."""
        self.services.append(service)
        return service

    def add_relationship(self, relationship: EventRelationship) -> EventRelationship:
        """Append an explicit relationship and return it."""
        self.relationships.append(relationship)
        return relationship

    def warn(
        self,
        message: str,
        *,
        source: SourceLocation | None = None,
        code: str | None = None,
    ) -> None:
        """Record a warning diagnostic."""
        self.diagnostics.append(
            Diagnostic(severity=Severity.WARNING, message=message, source=source, code=code)
        )

    def error(
        self,
        message: str,
        *,
        source: SourceLocation | None = None,
        code: str | None = None,
    ) -> None:
        """Record an error diagnostic."""
        self.diagnostics.append(
            Diagnostic(severity=Severity.ERROR, message=message, source=source, code=code)
        )

    @property
    def empty(self) -> bool:
        """Whether the plugin produced no fragments at all."""
        return not (self.events or self.topics or self.services or self.relationships)

    def counts(self) -> dict[str, int]:
        """Return fragment counts per entity kind."""
        return {
            "events": len(self.events),
            "topics": len(self.topics),
            "services": len(self.services),
            "relationships": len(self.relationships),
        }

    def extend(self, other: DiscoveryResult) -> None:
        """Merge another result into this one."""
        self.events.extend(other.events)
        self.topics.extend(other.topics)
        self.services.extend(other.services)
        self.relationships.extend(other.relationships)
        self.diagnostics.extend(other.diagnostics)

    def to_dict(self) -> dict[str, Any]:
        """Serializable form used by the scan cache."""
        return {
            "events": [event.model_dump(mode="json") for event in self.events],
            "topics": [topic.model_dump(mode="json") for topic in self.topics],
            "services": [service.model_dump(mode="json") for service in self.services],
            "relationships": [
                relationship.model_dump(mode="json") for relationship in self.relationships
            ],
            "diagnostics": [item.model_dump(mode="json") for item in self.diagnostics],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiscoveryResult:
        """Rebuild a result from a cached payload."""
        return cls(
            events=[Event.model_validate(item) for item in data.get("events", [])],
            topics=[Topic.model_validate(item) for item in data.get("topics", [])],
            services=[Service.model_validate(item) for item in data.get("services", [])],
            relationships=[
                EventRelationship.model_validate(item) for item in data.get("relationships", [])
            ],
            diagnostics=[Diagnostic.model_validate(item) for item in data.get("diagnostics", [])],
        )


class DiscoveryPlugin(ABC):
    """Base class for discovery plugins."""

    #: Stable plugin identifier, used by ``discovery.plugins`` config and logs.
    name: ClassVar[str] = ""
    #: Filename globs the plugin can handle, e.g. ``("*.proto",)``.
    patterns: ClassVar[tuple[str, ...]] = ()
    #: Higher priority wins ties between equally confident plugins.
    priority: ClassVar[int] = 100
    #: One-line description shown by ``eventlineage plugins``.
    description: ClassVar[str] = ""
    #: File extensions this plugin reads, for documentation purposes.
    extensions: ClassVar[tuple[str, ...]] = ()

    def supports(self, path: Path) -> bool:
        """Whether the plugin can be asked about ``path`` at all."""
        if not self.patterns:
            return True
        name = path.name
        relative = path.as_posix()
        return any(
            fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relative, pattern)
            for pattern in self.patterns
        )

    def detect(self, path: Path, head: str) -> int:
        """Confidence (0-100) that this plugin should handle ``path``.

        The default implementation scores any supported file at 50, which is
        the right behaviour for single-extension formats such as ``.proto``.
        Content sniffing plugins override this.
        """
        return 50 if self.supports(path) else 0

    @abstractmethod
    def discover(self, path: Path, ctx: DiscoveryContext) -> DiscoveryResult:
        """Parse ``path`` and return catalog fragments."""

    def read_text(self, path: Path, ctx: DiscoveryContext) -> str:
        """Read a file as UTF-8, raising :class:`ParseError` on failure."""
        try:
            return path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            return path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:  # pragma: no cover - filesystem failure
            raise ParseError(f"cannot read file: {exc}", path=ctx.relative(path)) from exc

    def load_json(self, path: Path, ctx: DiscoveryContext, text: str | None = None) -> Any:
        """Parse ``path`` as JSON, raising :class:`ParseError` on failure."""
        raw = text if text is not None else self.read_text(path, ctx)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ParseError(
                f"invalid JSON: {exc.msg}", path=ctx.relative(path), line=exc.lineno
            ) from exc

    def load_yaml(self, path: Path, ctx: DiscoveryContext, text: str | None = None) -> Any:
        """Parse ``path`` as YAML, raising :class:`ParseError` on failure."""
        raw = text if text is not None else self.read_text(path, ctx)
        try:
            return load_yaml(raw)
        except yaml.YAMLError as exc:
            raise ParseError(f"invalid YAML: {exc}", path=ctx.relative(path)) from exc

    def load_json_or_yaml(self, path: Path, ctx: DiscoveryContext, text: str | None = None) -> Any:
        """Load JSON or YAML based on the extension, falling back to sniffing."""
        suffix = path.suffix.lower()
        if suffix in {".json", ".avsc"}:
            return self.load_json(path, ctx, text)
        if suffix in {".yaml", ".yml"}:
            return self.load_yaml(path, ctx, text)
        raw = text if text is not None else self.read_text(path, ctx)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return self.load_yaml(path, ctx, raw)

    def source(self, path: Path, ctx: DiscoveryContext, **kwargs: Any) -> SourceLocation:
        """Build a :class:`SourceLocation` for ``path``."""
        return SourceLocation(path=ctx.relative(path), plugin=self.name, **kwargs)


def looks_like_json(text: str) -> bool:
    """Cheap check for a JSON document without parsing it."""
    stripped = text.lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


def first_json_keys(text: str, limit: int = 64) -> set[str]:
    """Cheap top-level key sniffing that never raises on malformed input.

    Nesting is tracked because a JSON Schema property called ``events`` must not
    make the document look like a catalog.
    """
    stripped = text.lstrip()[:HEAD_SIZE]
    if not stripped.startswith("{"):
        return set()
    keys: set[str] = set()
    depth = 0
    for match in _JSON_TOKEN.finditer(stripped):
        token = match.group()
        if token in "{[":
            depth += 1
        elif token in "}]":
            depth -= 1
        elif depth == 1 and stripped[match.end() :].lstrip().startswith(":"):
            keys.add(token[1:-1])
            if len(keys) >= limit:
                break
    return keys


def yaml_top_level_keys(text: str) -> set[str]:
    """Top-level keys of a YAML document discovered with a line scan."""
    keys: set[str] = set()
    for line in text.splitlines():
        if not line or line[0] in " \t#-":
            continue
        if ":" in line:
            keys.add(line.split(":", 1)[0].strip().strip("\"'"))
        if len(keys) >= 40:
            break
    return keys


def load_referenced_document(
    base_dir: Path,
    reference: str,
    *,
    ctx: DiscoveryContext | None = None,
    cache: dict[str, Any] | None = None,
) -> Any:
    """Load a document referenced from another file (relative ``$ref``).

    Only local files are supported: EventLineage never fetches remote schemas,
    which keeps scans hermetic and reproducible. The reference may carry a JSON
    pointer fragment (``schemas/order.json#/definitions/Order``). ``cache`` holds
    whole documents, so several pointers into one file parse it once.
    """
    file_part, _, pointer = reference.partition("#")
    candidate = Path(file_part)
    if not candidate.is_absolute():
        candidate = (base_dir / candidate).resolve()
    if ctx is not None:
        ctx.reference(candidate)
    if not candidate.is_file():
        raise DiscoveryError(f"referenced file not found: {file_part!r}")
    document: Any
    if cache is not None and file_part in cache:
        document = cache[file_part]
    else:
        suffix = candidate.suffix.lower()
        text = candidate.read_text(encoding="utf-8-sig", errors="replace")
        if suffix in {".yaml", ".yml"}:
            document = load_yaml(text)
        elif suffix in {".proto", ".avdl"}:
            document = text
        else:
            try:
                document = json.loads(text)
            except json.JSONDecodeError:
                document = text
        if cache is not None:
            cache[file_part] = document
    if pointer:
        from eventlineage.discovery.catalog import _resolve_pointer

        resolved = _resolve_pointer(document, "#" + pointer)
        if resolved is None:
            raise DiscoveryError(f"reference {reference!r} does not resolve")
        return resolved
    return document


def coerce_service(
    value: Any, *, kind: Any = None, source: SourceLocation | None = None
) -> Service:
    """Accept ``"name"`` or ``{"name": ..., "tier": ...}`` service declarations."""
    if isinstance(value, str):
        return Service(name=value, source=source)
    if isinstance(value, dict):
        data = dict(value)
        if "name" not in data and "service" in data:
            data["name"] = data.pop("service")
        if kind is not None and "kind" not in data:
            data["kind"] = kind
        data.setdefault("source", source)
        return Service.model_validate(data)
    raise DiscoveryError(f"invalid service declaration: {value!r}")


def coerce_participant(
    value: Any,
    *,
    consumer: bool,
    default_service: str | None = None,
    source: SourceLocation | None = None,
) -> Producer | Consumer:
    """Accept ``"service"``, ``{"service": ...}`` or a full participant dict."""
    cls: type[Producer] | type[Consumer] = Consumer if consumer else Producer
    if isinstance(value, str):
        return cls(service=value, source=source)
    if isinstance(value, dict):
        data = dict(value)
        service = data.pop("service", None) or data.pop("name", None) or default_service
        if service is None:
            raise DiscoveryError(f"participant is missing a service name: {value!r}")
        data.setdefault("source", source)
        return cls.model_validate({"service": service, **data})
    if default_service is not None:
        return cls(service=default_service, source=source)
    raise DiscoveryError(f"invalid participant declaration: {value!r}")


__all__ = [
    "HEAD_SIZE",
    "DiscoveryContext",
    "DiscoveryPlugin",
    "DiscoveryResult",
    "coerce_participant",
    "coerce_service",
    "first_json_keys",
    "load_referenced_document",
    "looks_like_json",
    "yaml_top_level_keys",
]
