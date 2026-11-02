"""Native EventLineage catalog plugin (JSON or YAML).

This is the "manual catalog" source and the richest one: it can declare events,
versions, schemas (inline, by file reference or by JSON pointer), services,
topics and explicit relationships. Everything the engine knows can be expressed
here, which makes it the recommended place to record architectural decisions
that cannot be inferred from code.

Minimal shape::

    version: 1
    events:
      - name: orders.created
        version: 1.0.0
        schema:
          format: json-schema
          file: ../schemas/order_created.json
        producers: [order-service]
        consumers: [billing-service, search-service]
        topics: [orders.v1]

Full reference: ``docs/configuration.md``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar, cast

from eventlineage.discovery.base import (
    DiscoveryContext,
    DiscoveryPlugin,
    DiscoveryResult,
    coerce_participant,
    coerce_service,
    first_json_keys,
    yaml_top_level_keys,
)
from eventlineage.exceptions import DiscoveryError
from eventlineage.models.common import (
    RelationshipKind,
    SchemaFormat,
    SourceLocation,
    VersionStatus,
)
from eventlineage.models.dependency import EventRelationship
from eventlineage.models.event import Event
from eventlineage.models.participant import Consumer, Producer
from eventlineage.models.schema import EventSchema, EventVersion
from eventlineage.models.topic import Topic
from eventlineage.parsers import definition_from_file, detect_schema_format, parse_proto_file
from eventlineage.utils.versioning import latest_version, version_key
from eventlineage.utils.yaml_io import load_yaml

SCHEMA_MARKERS = ("eventlineage", "eventlineage.dev")
_PROTO_SUFFIXES = (".proto",)
_SCHEMA_SUFFIXES = (".json", ".avsc", ".yaml", ".yml", ".json5")


def _as_sequence(node: Any) -> list[Any]:
    if node is None:
        return []
    if isinstance(node, (list, tuple)):
        return list(node)
    return [node]


def _load_structured(path: Path, ctx: DiscoveryContext) -> Any:
    """Load a referenced schema file, keeping proto sources as text."""
    ctx.reference(path)
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() in _PROTO_SUFFIXES:
        return text
    if path.suffix.lower() in {".yaml", ".yml"}:
        return load_yaml(text)
    return json.loads(text)


def _resolve_pointer(document: Any, pointer: str) -> Any:
    """Resolve a JSON pointer (``#/components/schemas/Order``) inside ``document``."""
    if not pointer.startswith("#"):
        return None
    node = document
    for token in pointer.lstrip("#").strip("/").split("/"):
        if not token:
            continue
        segment = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping) and segment in node:
            node = node[segment]
        elif isinstance(node, Sequence) and not isinstance(node, str):
            try:
                node = node[int(segment)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return node


def coerce_schema(
    raw: Any,
    *,
    base_dir: Path,
    document: Any,
    source: SourceLocation,
    ctx: DiscoveryContext,
) -> EventSchema | None:
    """Build an :class:`EventSchema` from an inline document or a reference."""
    if raw is None:
        return None
    if isinstance(raw, EventSchema):
        return raw
    if isinstance(raw, str):
        candidate = (base_dir / raw) if not Path(raw).is_absolute() else Path(raw)
        if candidate.is_file():
            definition = _load_structured(candidate, ctx)
            return EventSchema(
                format=detect_schema_format(definition),
                definition=definition,
                name=raw,
                source=source,
            )
        # A bare string schema: treat obvious proto source as protobuf text and
        # anything else as an unresolved reference.
        if "message " in raw or raw.lstrip().startswith("syntax"):
            return EventSchema(format=SchemaFormat.PROTOBUF, definition=raw, raw=raw, source=source)
        raise DiscoveryError(f"schema reference not found: {raw!r}")

    if isinstance(raw, Mapping):
        data = dict(raw)
        if "file" in data:
            relative = str(data["file"])
            candidate = Path(relative)
            if not candidate.is_absolute():
                candidate = base_dir / candidate
            if not candidate.is_file():
                # Remembered even when missing: adding the file must invalidate this entry.
                ctx.reference(candidate)
                raise DiscoveryError(f"schema file not found: {relative!r}")
            loaded: Any = _load_structured(candidate, ctx)
            fmt_raw = data.get("format")
            if fmt_raw is not None:
                fmt = SchemaFormat.coerce(str(fmt_raw))
            else:
                fmt = detect_schema_format(loaded)
            if fmt == SchemaFormat.PROTOBUF and isinstance(loaded, str):
                proto_file = parse_proto_file(loaded, path=str(candidate))
                message = (
                    proto_file.find_message(str(data["message"])) if data.get("message") else None
                )
                if message is not None:
                    loaded = definition_from_file(proto_file, message)
            return EventSchema(
                format=fmt,
                definition=loaded,
                name=str(data.get("name") or relative),
                version=str(data["version"]) if "version" in data else None,
                schema_id=str(data["id"]) if "id" in data else None,
                source=source,
                metadata={
                    k: v
                    for k, v in data.items()
                    if k not in {"file", "format", "name", "version", "id"}
                },
            )

        if "ref" in data:
            resolved = _resolve_pointer(document, str(data["ref"]))
            if resolved is None:
                raise DiscoveryError(f"schema reference could not be resolved: {data['ref']!r}")
            fmt_raw = data.get("format")
            fmt = SchemaFormat.coerce(str(fmt_raw)) if fmt_raw else detect_schema_format(resolved)
            return EventSchema(
                format=fmt,
                definition=resolved,
                name=str(data.get("name") or data["ref"]),
                source=source,
            )

        if "definition" in data or "schema" in data:
            definition = data.get("definition", data.get("schema"))
            fmt_raw = data.get("format")
            fmt = SchemaFormat.coerce(str(fmt_raw)) if fmt_raw else detect_schema_format(definition)
            return EventSchema(
                format=fmt,
                definition=definition,
                raw=definition if isinstance(definition, str) else None,
                name=str(data["name"]) if "name" in data else None,
                version=str(data["version"]) if "version" in data else None,
                schema_id=str(data["id"]) if "id" in data else None,
                source=source,
                metadata={
                    key: value
                    for key, value in data.items()
                    if key
                    not in {"definition", "schema", "format", "name", "version", "id", "message"}
                },
            )

        return EventSchema(
            format=detect_schema_format(data),
            definition=data,
            name=str(data["title"]) if "title" in data else None,
            source=source,
        )
    if isinstance(raw, (list, tuple)):
        return EventSchema(format=SchemaFormat.AVRO, definition=list(raw), source=source)
    raise DiscoveryError(f"unsupported schema declaration: {raw!r}")


def _coerce_versions(
    event_data: Mapping[str, Any],
    *,
    base_dir: Path,
    document: Any,
    source: SourceLocation,
    ctx: DiscoveryContext,
) -> tuple[EventVersion, ...]:
    versions: list[EventVersion] = []
    for raw in _as_sequence(event_data.get("versions")):
        if isinstance(raw, (str, int, float)):  # the shape ``export --format catalog`` writes
            versions.append(EventVersion(version=str(raw)))
            continue
        if not isinstance(raw, Mapping):
            raise DiscoveryError(f"invalid version entry for event {event_data.get('name')!r}")
        version_value = raw.get("version") or raw.get("id")
        if version_value is None:
            raise DiscoveryError("event version entry is missing 'version'")
        schema = coerce_schema(
            raw.get("schema"), base_dir=base_dir, document=document, source=source, ctx=ctx
        )
        versions.append(
            EventVersion(
                version=str(version_value),
                status=VersionStatus(str(raw.get("status", "active"))),
                schema=schema,
                released_at=(
                    str(raw["released_at"])
                    if "released_at" in raw
                    else str(raw["released"])
                    if "released" in raw
                    else None
                ),
                notes=str(raw["notes"]) if "notes" in raw else None,
                metadata=dict(raw.get("metadata") or {}),
            )
        )
    return tuple(versions)


def build_event(
    spec: Mapping[str, Any],
    *,
    name_hint: str | None,
    base_dir: Path,
    document: Any,
    source: SourceLocation,
    ctx: DiscoveryContext,
) -> Event:
    """Turn one catalog entry into an :class:`Event`."""
    data = dict(spec)
    name = str(data.pop("name", None) or data.pop("event", None) or name_hint or "").strip()
    if not name:
        raise DiscoveryError("event declaration is missing a 'name'")

    versions = _coerce_versions(data, base_dir=base_dir, document=document, source=source, ctx=ctx)
    versions = tuple(sorted(versions, key=lambda entry: version_key(entry.version)))
    schema = coerce_schema(
        data.get("schema"), base_dir=base_dir, document=document, source=source, ctx=ctx
    )
    if schema is None and versions:
        newest = versions[-1]
        if newest.schema is not None:
            schema = newest.schema

    version = str(data.get("version") or (latest_version([e.version for e in versions]) or "1.0.0"))
    status = VersionStatus(str(data.get("status", "active")))

    producers: list[Producer] = [
        cast(Producer, coerce_participant(raw, consumer=False, source=source))
        for raw in _as_sequence(data.get("producers") or data.get("produced_by"))
    ]
    consumers: list[Consumer] = [
        cast(Consumer, coerce_participant(raw, consumer=True, source=source))
        for raw in _as_sequence(data.get("consumers") or data.get("consumed_by"))
    ]

    topics_raw = data.get("topics", data.get("topic"))
    topics = tuple(sorted({str(item) for item in _as_sequence(topics_raw)}))

    owners_raw = data.get("owners", data.get("owner"))
    tags_raw = data.get("tags")

    return Event(
        id=str(data.get("id") or name),
        name=name,
        namespace=str(data.get("namespace") or "default"),
        version=version,
        status=status,
        schema=schema,
        versions=versions,
        producers=tuple(producers),
        consumers=tuple(consumers),
        topics=topics,
        description=str(data["description"]) if "description" in data else None,
        owners=tuple(sorted({str(item) for item in _as_sequence(owners_raw)})),
        tags=tuple(sorted({str(item) for item in _as_sequence(tags_raw)})),
        metadata=dict(data.get("metadata") or {}),
        source=source,
        inferred=bool(data.get("inferred", False)),
    )


def parse_service_specs(
    services_node: Any,
    *,
    source_for: Any,
    default_kind: Any = None,
) -> tuple[list[Any], list[Event], list[tuple[str, str, bool]]]:
    """Parse service declarations into services, implied events and links.

    Returns ``(services, implied_events, (service, event_ref, consumes) links)``.
    Service-centric descriptions (``produces``/``consumes`` lists) are the most
    common way teams document a platform, so they are supported everywhere a
    service can be declared.
    """
    services: list[Any] = []
    events: list[Event] = []
    links: list[tuple[str, str, bool]] = []

    entries: list[tuple[str | None, Any]] = []
    if isinstance(services_node, Mapping):
        entries = [(str(key), value) for key, value in services_node.items()]
    else:
        entries = [(None, value) for value in _as_sequence(services_node)]

    for key, raw in entries:
        if isinstance(raw, str):
            services.append(coerce_service(raw, kind=default_kind, source=source_for(raw)))
            continue
        if not isinstance(raw, Mapping):
            continue
        data = dict(raw)
        name = str(data.get("name") or data.get("service") or key or "").strip()
        if not name:
            continue
        produces = [
            item.get("event") if isinstance(item, Mapping) else item
            for item in _as_sequence(data.pop("produces", None))
        ]
        consumes = [
            item.get("event") if isinstance(item, Mapping) else item
            for item in _as_sequence(data.pop("consumes", None))
        ]
        data.pop("name", None)
        data.pop("service", None)
        data.setdefault("source", source_for(name))
        services.append(coerce_service({"name": name, **data}, kind=default_kind))
        for reference in produces:
            if reference is None:
                continue
            links.append((name, str(reference), False))
            events.append(
                Event(
                    id=str(reference),
                    name=str(reference),
                    inferred=True,
                    producers=(Producer(service=name),),
                    source=source_for(name),
                )
            )
        for reference in consumes:
            if reference is None:
                continue
            links.append((name, str(reference), True))
            events.append(
                Event(
                    id=str(reference),
                    name=str(reference),
                    inferred=True,
                    consumers=(Consumer(service=name),),
                    source=source_for(name),
                )
            )
    return services, events, links


class CatalogPlugin(DiscoveryPlugin):
    """EventLineage native catalog and service-centric configuration files."""

    name: ClassVar[str] = "catalog"
    patterns: ClassVar[tuple[str, ...]] = ("*.json", "*.yaml", "*.yml")
    priority: ClassVar[int] = 120
    description: ClassVar[str] = (
        "EventLineage native event catalog (events, services, topics, relationships)"
    )
    extensions: ClassVar[tuple[str, ...]] = (".json", ".yaml", ".yml")

    def detect(self, path: Path, head: str) -> int:
        """Return the detection confidence (0-100) for ``path``."""
        keys = (
            first_json_keys(head)
            if head.lstrip().startswith(("{", "["))
            else yaml_top_level_keys(head)
        )
        lowered = head.lower()
        marked = any(marker in lowered for marker in SCHEMA_MARKERS)
        if "asyncapi" in head[:200] or "openapi" in head[:200]:
            return 0
        if "events" in keys:
            return 100 if marked else 95
        if "services" in keys:
            if "topics" in keys or "relationships" in keys:
                return 92 if marked else 88
            if "produces" in lowered or "consumes" in lowered or "publishes" in lowered:
                return 90 if marked else 85
        if marked and "$schema" in keys:
            return 95
        return 0

    def discover(self, path: Path, ctx: DiscoveryContext) -> DiscoveryResult:
        """Parse ``path`` and return the catalog fragments it declares."""
        document = self.load_json_or_yaml(path, ctx)
        result = DiscoveryResult()
        if document is None:
            return result
        if not isinstance(document, Mapping):
            raise DiscoveryError("catalog root must be a mapping with an 'events' key")
        source = self.source(path, ctx)
        base_dir = path.parent

        services, implied_events, _links = parse_service_specs(
            document.get("services"), source_for=lambda _name: source
        )
        for service in services:
            result.add_service(service)

        events_node = document.get("events")
        if isinstance(events_node, Mapping):
            entries: Iterable[tuple[str | None, Any]] = [
                (str(key), value) for key, value in events_node.items()
            ]
        else:
            entries = [(None, value) for value in _as_sequence(events_node)]
        for key, raw in entries:
            if not isinstance(raw, Mapping):
                result.warn(f"skipping malformed event entry {key or '<unnamed>'!r}", source=source)
                continue
            try:
                result.add_event(
                    build_event(
                        raw,
                        name_hint=key,
                        base_dir=base_dir,
                        document=document,
                        source=source,
                        ctx=ctx,
                    )
                )
            except (DiscoveryError, ValueError) as exc:
                result.warn(
                    f"invalid event {key or raw.get('name') or '<unnamed>'}: {exc}", source=source
                )
        for event in implied_events:
            result.add_event(event)

        topics_node = document.get("topics")
        topic_entries: list[tuple[str | None, Any]] = (
            [(str(key), value) for key, value in topics_node.items()]
            if isinstance(topics_node, Mapping)
            else [(None, value) for value in _as_sequence(topics_node)]
        )
        for key, raw in topic_entries:
            topic = self._build_topic(raw, key, source)
            if topic is not None:
                result.add_topic(topic)

        for raw in _as_sequence(document.get("relationships")):
            relationship = self._build_relationship(raw, source)
            if relationship is not None:
                result.add_relationship(relationship)
        return result

    def _build_topic(self, raw: Any, key: str | None, source: SourceLocation) -> Topic | None:
        if isinstance(raw, str):
            return Topic(name=raw, source=source)
        if not isinstance(raw, Mapping):
            return None
        data = dict(raw)
        name = str(data.pop("name", None) or key or "")
        if not name:
            return None
        producers = tuple(sorted({str(item) for item in _as_sequence(data.pop("producers", None))}))
        consumers = tuple(sorted({str(item) for item in _as_sequence(data.pop("consumers", None))}))
        events = tuple(sorted({str(item) for item in _as_sequence(data.pop("events", None))}))
        data.setdefault("source", source)
        try:
            return Topic.model_validate(
                {
                    "name": name,
                    "producers": producers,
                    "consumers": consumers,
                    "events": events,
                    **data,
                }
            )
        except ValueError:
            return None

    def _build_relationship(self, raw: Any, source: SourceLocation) -> EventRelationship | None:
        if not isinstance(raw, Mapping):
            return None
        data = dict(raw)
        source_event = (
            data.pop("from", None) or data.pop("source", None) or data.pop("source_event", None)
        )
        target_event = (
            data.pop("to", None) or data.pop("target", None) or data.pop("target_event", None)
        )
        if not source_event or not target_event:
            return None
        kind_raw = str(data.pop("kind", "triggers"))
        try:
            kind = RelationshipKind(kind_raw)
        except ValueError:
            kind = RelationshipKind.TRIGGERS
        data.pop("inferred", None)
        return EventRelationship(
            source_event=str(source_event),
            target_event=str(target_event),
            kind=kind,
            inferred=bool(raw.get("inferred", False)),
            confidence=float(raw.get("confidence", 1.0)),
            source_location=source,
            metadata=dict(data),
        )


__all__ = ["CatalogPlugin", "build_event", "coerce_schema", "parse_service_specs"]
