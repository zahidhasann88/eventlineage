"""OpenAPI discovery for documents that carry event metadata.

OpenAPI is a request/response specification, so events only appear where a team
documents them explicitly:

``x-events`` / ``x-eventlineage-events``
    An explicit list of event declarations (the recommended extension).
``webhooks`` (OpenAPI 3.1)
    Requests the described application *receives* — consumers.
``callbacks`` (OpenAPI 3.0+)
    Requests the described application *sends* — producers.
``components.schemas`` entries named ``*Event``/``*Command``
    Used only to provide a schema for an event that is otherwise declared, never
    to invent an event on its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, cast

from eventlineage.discovery.asyncapi import slugify
from eventlineage.discovery.base import (
    DiscoveryContext,
    DiscoveryPlugin,
    DiscoveryResult,
    coerce_participant,
    load_referenced_document,
)
from eventlineage.discovery.catalog import _resolve_pointer
from eventlineage.exceptions import DiscoveryError
from eventlineage.models.common import SchemaFormat
from eventlineage.models.event import Event
from eventlineage.models.participant import Consumer, Producer, Service
from eventlineage.models.schema import EventSchema

_EVENT_EXTENSIONS = ("x-events", "x-eventlineage-events", "x-events-catalog")


class OpenApiPlugin(DiscoveryPlugin):
    """OpenAPI 3.x / Swagger 2.0 documents with event metadata."""

    name: ClassVar[str] = "openapi"
    patterns: ClassVar[tuple[str, ...]] = ("*.json", "*.yaml", "*.yml")
    priority: ClassVar[int] = 125
    description: ClassVar[str] = "OpenAPI documents with x-events, webhooks or callbacks"
    extensions: ClassVar[tuple[str, ...]] = (".json", ".yaml", ".yml")

    def detect(self, path: Path, head: str) -> int:
        """Return the detection confidence (0-100) for ``path``."""
        lowered = head.lower()[:2000]
        if '"asyncapi"' in lowered or "asyncapi:" in lowered:
            return 0
        if '"openapi"' in lowered or "openapi:" in lowered:
            return 95
        if '"swagger"' in lowered or "swagger:" in lowered:
            return 90
        return 0

    def discover(self, path: Path, ctx: DiscoveryContext) -> DiscoveryResult:
        """Parse ``path`` and return the catalog fragments it declares."""
        document = self.load_json_or_yaml(path, ctx)
        if not isinstance(document, Mapping):
            raise DiscoveryError("OpenAPI document root must be a mapping")
        if not any(key in document for key in ("openapi", "swagger")):
            raise DiscoveryError("not an OpenAPI document")
        result = DiscoveryResult()
        source = self.source(path, ctx)
        info = document.get("info") or {}
        title = info.get("title") if isinstance(info, Mapping) else None
        service_name = str(
            document.get("x-service-name") or (slugify(str(title)) if title else slugify(path.stem))
        )
        namespace = str(document.get("x-namespace") or service_name)
        result.add_service(
            Service(
                name=service_name,
                description=str(title) if title else None,
                metadata={
                    "openapi_version": str(document.get("openapi") or document.get("swagger"))
                },
                source=source,
            )
        )

        events: dict[str, Event] = {}

        external: dict[str, Any] = {}

        def resolve(node: Any) -> Any:
            seen = 0
            while isinstance(node, Mapping) and "$ref" in node and seen < 8:
                reference = str(node["$ref"])
                if reference.startswith("#"):
                    target = _resolve_pointer(document, reference)
                else:
                    try:
                        target = load_referenced_document(
                            path.parent, reference, ctx=ctx, cache=external
                        )
                    except Exception as exc:
                        result.warn(
                            f"unresolved OpenAPI reference {reference!r}: {exc}", source=source
                        )
                        return None
                if target is None:
                    result.warn(f"unresolved OpenAPI reference {reference!r}", source=source)
                    return None
                node = target
                seen += 1
            return node

        def qualified(name: str) -> str:
            """Dotted names are already fully qualified event ids."""
            return name if "." in name else f"{namespace}.{name}"

        def register(
            name: str,
            *,
            schema_node: Any,
            role: str,
            version: str | None = None,
            description: str | None = None,
            topic: str | None = None,
        ) -> None:
            event_id = qualified(name)
            payload = resolve(schema_node)
            schema = (
                EventSchema(
                    format=SchemaFormat.JSON_SCHEMA,
                    definition=payload,
                    name=name,
                    source=source,
                )
                if payload is not None
                else None
            )
            event = events.get(event_id)
            if event is None:
                event = Event(
                    id=event_id,
                    name=name,
                    namespace=None if "." in name else namespace,
                    version=version or "1.0.0",
                    description=description,
                    schema=schema,
                    topics=(topic,) if topic else (),
                    metadata={"openapi_operation": role},
                    source=source,
                )
                events[event_id] = event
            elif event.schema is None and schema is not None:
                event.schema = schema
            participant = coerce_participant(
                {"service": service_name, "topics": [topic] if topic else []},
                consumer=role == "consumer",
                source=source,
            )
            if role == "producer":
                event.producers = (*event.producers, participant)  # type: ignore[arg-type]
            else:
                event.consumers = (*event.consumers, participant)  # type: ignore[arg-type]

        for extension in _EVENT_EXTENSIONS:
            for raw in _sequence(document.get(extension)):
                if not isinstance(raw, Mapping):
                    continue
                name = raw.get("name") or raw.get("event") or raw.get("id")
                if not name:
                    result.warn(f"entry in {extension} is missing a name", source=source)
                    continue
                schema_node = raw.get("schema") or raw.get("payload")
                if isinstance(schema_node, Mapping) and "schema" in schema_node:
                    schema_node = schema_node["schema"]
                event_name = str(name)
                event_id = str(raw.get("id") or qualified(event_name))
                payload = resolve(schema_node)
                if payload is None:
                    payload = self._component_schema(document, event_name)
                event = events.get(event_id)
                schema = (
                    EventSchema(
                        format=SchemaFormat.JSON_SCHEMA,
                        definition=payload,
                        name=event_name,
                        source=source,
                    )
                    if payload is not None
                    else None
                )
                if event is None:
                    event = Event(
                        id=event_id,
                        name=event_name,
                        namespace=None if "." in event_name else namespace,
                        version=str(raw.get("version") or "1.0.0"),
                        description=(
                            str(raw["description"])
                            if isinstance(raw.get("description"), str)
                            else None
                        ),
                        schema=schema,
                        topics=tuple(
                            sorted(
                                {
                                    str(item)
                                    for item in _sequence(raw.get("topics") or raw.get("topic"))
                                }
                            )
                        ),
                        owners=tuple(sorted({str(item) for item in _sequence(raw.get("owners"))})),
                        source=source,
                    )
                    events[event_id] = event
                elif schema is not None:
                    event.schema = schema
                for participant in _sequence(raw.get("producers")):
                    producer = coerce_participant(participant, consumer=False, source=source)
                    event.producers = (*event.producers, cast(Producer, producer))
                for participant in _sequence(raw.get("consumers")):
                    consumer = coerce_participant(participant, consumer=True, source=source)
                    event.consumers = (*event.consumers, cast(Consumer, consumer))

        webhooks = document.get("webhooks")
        if isinstance(webhooks, Mapping):
            for name, item in webhooks.items():
                schema_node = self._request_schema(item, resolve)
                register(
                    str(name),
                    schema_node=schema_node,
                    role="consumer",
                    description=self._operation_description(item),
                )

        paths = document.get("paths")
        if isinstance(paths, Mapping):
            for path_item in paths.values():
                if not isinstance(path_item, Mapping):
                    continue
                for operation in path_item.values():
                    if not isinstance(operation, Mapping):
                        continue
                    callbacks = operation.get("callbacks")
                    if not isinstance(callbacks, Mapping):
                        continue
                    for callback_name, callback in callbacks.items():
                        if not isinstance(callback, Mapping):
                            continue
                        for expression, callback_operation in callback.items():
                            schema_node = self._request_schema(callback_operation, resolve)
                            register(
                                str(callback_name),
                                schema_node=schema_node,
                                role="producer",
                                description=str(expression),
                            )

        for event in sorted(events.values(), key=lambda item: item.id):
            result.add_event(event)
        return result

    def _component_schema(self, document: Mapping[str, Any], name: str) -> Any:
        components = document.get("components") or {}
        schemas = components.get("schemas") if isinstance(components, Mapping) else {}
        if isinstance(schemas, Mapping):
            for key, value in schemas.items():
                if str(key).lower() == name.lower() or str(key).lower() == f"{name.lower()}event":
                    return value
        return None

    def _request_schema(self, operation: Any, resolve: Any) -> Any:
        if not isinstance(operation, Mapping):
            return None
        # Webhooks and callbacks hold Path Item Objects: the body is one level down.
        if "requestBody" not in operation:
            for method in ("post", "put", "patch", "delete", "get"):
                if isinstance(operation.get(method), Mapping):
                    operation = operation[method]
                    break
        request_body = resolve(operation.get("requestBody"))
        if not isinstance(request_body, Mapping):
            return None
        content = request_body.get("content")
        if not isinstance(content, Mapping) or not content:
            return None
        first = next(iter(content.values()))
        if not isinstance(first, Mapping):
            return None
        return resolve(first.get("schema"))

    @staticmethod
    def _operation_description(operation: Any) -> str | None:
        if isinstance(operation, Mapping):
            candidates = [operation, *(v for v in operation.values() if isinstance(v, Mapping))]
            for candidate in candidates:
                for key in ("description", "summary"):
                    value = candidate.get(key)
                    if isinstance(value, str):
                        return value
        return None


def _sequence(node: Any) -> list[Any]:
    if node is None:
        return []
    if isinstance(node, (list, tuple)):
        return list(node)
    if isinstance(node, Mapping):
        return [node]
    return [node]


__all__ = ["OpenApiPlugin"]
