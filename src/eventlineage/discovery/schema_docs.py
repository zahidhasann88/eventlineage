"""JSON Schema and Avro document plugins.

Both plug into a common "one document, one or more schemas" pattern: the file
name (or the schema's own name) becomes the event name, so a directory of
``order.created.schema.json`` / ``OrderCreated.avsc`` files is a complete event
catalog with no extra metadata.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from eventlineage.discovery.base import (
    DiscoveryContext,
    DiscoveryPlugin,
    DiscoveryResult,
    first_json_keys,
)
from eventlineage.exceptions import DiscoveryError
from eventlineage.models.common import SchemaFormat
from eventlineage.models.event import Event
from eventlineage.models.schema import EventSchema
from eventlineage.parsers.avro import collect_named_types

_SUFFIX_NOISE = re.compile(r"\.(schema|event|events|avro|record|message|contract)$", re.IGNORECASE)
_VERSION_SUFFIX = re.compile(r"[@._]v?(\d+(?:[._]\d+)*)$", re.IGNORECASE)


def event_name_from_path(path: Path) -> tuple[str, str | None]:
    """Derive ``(name, version)`` from a schema file name.

    ``orders/order.created.schema.json`` -> ``("order.created", None)``
    ``order.created.v2.json`` -> ``("order.created", "2")``
    ``order.created.v1_2.json`` -> ``("order.created", "1.2")``
    """
    stem = path.stem
    version: str | None = None
    match = _VERSION_SUFFIX.search(stem)
    if match:
        version = match.group(1).replace("_", ".")
        stem = stem[: match.start()]
    stem = _SUFFIX_NOISE.sub("", stem)
    name = stem.strip("._-") or path.stem
    return name, version


def _version_from_metadata(document: Mapping[str, Any]) -> str | None:
    for key in ("x-version", "version", "x-event-version"):
        if key in document:
            value = document[key]
            if isinstance(value, (str, int, float)):
                return str(value)
    return None


class JsonSchemaPlugin(DiscoveryPlugin):
    """Standalone JSON Schema documents become events."""

    name: ClassVar[str] = "json-schema"
    patterns: ClassVar[tuple[str, ...]] = ("*.json",)
    priority: ClassVar[int] = 80
    description: ClassVar[str] = "Standalone JSON Schema documents (one event per file)"
    extensions: ClassVar[tuple[str, ...]] = (".json",)

    def detect(self, path: Path, head: str) -> int:
        """Return the detection confidence (0-100) for ``path``."""
        stripped = head.lstrip()
        if not stripped.startswith("{"):
            return 0
        lowered = head.lower()
        if "asyncapi" in lowered[:400] or "openapi" in lowered[:400] or "swagger" in lowered[:400]:
            return 0
        if '"$schema"' in head and "json-schema" in lowered:
            return 92
        if '"type": "record"' in lowered and '"fields"' in lowered:
            return 0
        if first_json_keys(head) & {"events", "subjects"}:
            return 0
        if '"properties"' in lowered and '"type"' in lowered:
            return 55
        return 0

    def discover(self, path: Path, ctx: DiscoveryContext) -> DiscoveryResult:
        """Parse ``path`` and return the catalog fragments it declares."""
        document = self.load_json(path, ctx)
        if not isinstance(document, Mapping):
            raise DiscoveryError("JSON Schema document must be an object")
        name, version = event_name_from_path(path)
        title = document.get("title")
        # A title is an identifier only when it looks like one; "Order Created" is
        # a label, so the file name stays the event name.
        usable_title = isinstance(title, str) and len(title.split()) == 1
        event_name = str(title).strip() if usable_title else name
        namespace = document.get("x-namespace")
        source = self.source(path, ctx)
        event = Event(
            id=str(document.get("x-event-id") or event_name),
            name=event_name,
            namespace=str(namespace) if namespace else "default",
            version=_version_from_metadata(document) or version or "1.0.0",
            description=document.get("description")
            if isinstance(document.get("description"), str)
            else None,
            schema=EventSchema(
                format=SchemaFormat.JSON_SCHEMA,
                definition=dict(document),
                name=event_name,
                source=source,
            ),
            topics=self._topics(document),
            owners=self._owners(document),
            metadata={"discovered_by": self.name},
            source=source,
        )
        result = DiscoveryResult()
        result.add_event(event)
        return result

    @staticmethod
    def _topics(document: Mapping[str, Any]) -> tuple[str, ...]:
        raw = document.get("x-topic") or document.get("x-topics")
        if isinstance(raw, str):
            return (raw,)
        if isinstance(raw, list):
            return tuple(sorted({str(item) for item in raw}))
        return ()

    @staticmethod
    def _owners(document: Mapping[str, Any]) -> tuple[str, ...]:
        raw = document.get("x-owners") or document.get("x-owner")
        if isinstance(raw, str):
            return (raw,)
        if isinstance(raw, list):
            return tuple(sorted({str(item) for item in raw}))
        return ()


def _inline_type_names(node: Any) -> set[str]:
    """Names of named types declared inside ``node`` (records, enums, fixed)."""
    names: set[str] = set()
    stack: list[Any] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            kind = current.get("type")
            if (
                isinstance(kind, str)
                and kind in {"record", "error", "enum", "fixed"}
                and isinstance(current.get("name"), str)
            ):
                names.add(current["name"])
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return names


class AvroPlugin(DiscoveryPlugin):
    """Avro ``.avsc`` documents (single record or a list of named types)."""

    name: ClassVar[str] = "avro"
    patterns: ClassVar[tuple[str, ...]] = ("*.avsc", "*.json", "*.avro")
    priority: ClassVar[int] = 85
    description: ClassVar[str] = "Avro schema documents (.avsc)"
    extensions: ClassVar[tuple[str, ...]] = (".avsc", ".avro")

    def detect(self, path: Path, head: str) -> int:
        """Return the detection confidence (0-100) for ``path``."""
        if path.suffix.lower() in {".avsc", ".avro"}:
            return 100
        stripped = head.lstrip()
        if not stripped.startswith(("{", "[")):
            return 0
        lowered = head.lower()
        if '"asyncapi"' in lowered[:400] or '"openapi"' in lowered[:400]:
            return 0
        if first_json_keys(head) & {"subjects", "topics"}:
            return 0
        if '"type"' in lowered and '"record"' in lowered and '"fields"' in lowered:
            return 80
        return 0

    def discover(self, path: Path, ctx: DiscoveryContext) -> DiscoveryResult:
        """Parse ``path`` and return the catalog fragments it declares."""
        document = self.load_json(path, ctx)
        records = self._records(document)
        if not records:
            raise DiscoveryError("no Avro record found in document")
        result = DiscoveryResult()
        source = self.source(path, ctx)
        registry = collect_named_types(document if isinstance(document, (list, Mapping)) else {})
        # Named types that are declared inline in a record are already part of its
        # definition; keeping them in ``_registry`` as well would make the schema
        # hash differ from the same file referenced directly by a catalog.
        default_name, default_version = event_name_from_path(path)
        for record in records:
            name = str(record.get("name") or default_name)
            namespace = record.get("namespace")
            event_name = str(namespace) + "." + name if namespace else name
            version = record.get("x-version") or record.get("version") or default_version or "1.0.0"
            inline = _inline_type_names(record)
            external = {
                key: value
                for key, value in registry.items()
                if key not in inline and key.rsplit(".", 1)[-1] not in inline
            }
            definition = {**record, "_registry": external} if external else dict(record)
            result.add_event(
                Event(
                    id=str(record.get("x-event-id") or event_name),
                    name=event_name,
                    version=str(version),
                    description=record.get("doc") if isinstance(record.get("doc"), str) else None,
                    schema=EventSchema(
                        format=SchemaFormat.AVRO,
                        definition=definition,
                        name=name,
                        version=str(version),
                        source=source,
                    ),
                    topics=self._topics(record),
                    metadata={"discovered_by": self.name},
                    source=source,
                )
            )
        return result

    def _records(self, document: Any) -> list[Mapping[str, Any]]:
        if isinstance(document, Mapping):
            if isinstance(document.get("schema"), Mapping) and "type" in document["schema"]:
                return [document["schema"]]
            if document.get("type") in {"record", "error"}:
                return [document]
            if isinstance(document.get("types"), list):
                return [item for item in document["types"] if isinstance(item, Mapping)]
            return []
        if isinstance(document, list):
            return [
                item
                for item in document
                if isinstance(item, Mapping) and item.get("type") in {"record", "error"}
            ] or [item for item in document if isinstance(item, Mapping)]
        return []

    @staticmethod
    def _topics(record: Mapping[str, Any]) -> tuple[str, ...]:
        raw = record.get("x-topic") or record.get("x-topics") or record.get("topic")
        if isinstance(raw, str):
            return (raw,)
        if isinstance(raw, list):
            return tuple(sorted({str(item) for item in raw}))
        return ()


__all__ = ["AvroPlugin", "JsonSchemaPlugin", "event_name_from_path"]
