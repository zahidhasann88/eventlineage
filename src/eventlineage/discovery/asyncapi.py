"""AsyncAPI 2.x/3.x discovery.

AsyncAPI 2.x names operations from the *client's* point of view: ``subscribe``
defines the messages the application produces, ``publish`` the messages it
consumes (the specification's Channel Item Object says so explicitly, and the
inversion is the reason 3.0 replaced both with ``send``/``receive``). EventLineage
maps that onto events with a producer/consumer for the application described by
the document, and channel addresses become topics.

Supported: AsyncAPI 2.0-2.6 (``channels[*].publish``/``subscribe``) and a
pragmatic subset of 3.0 (``channels[*].messages`` plus root-level
``operations``). ``$ref`` pointers into ``components`` are resolved locally;
remote references are reported as warnings instead of failing the scan.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar, cast

from eventlineage.discovery.base import (
    DiscoveryContext,
    DiscoveryPlugin,
    DiscoveryResult,
    coerce_participant,
    load_referenced_document,
)
from eventlineage.discovery.catalog import _resolve_pointer
from eventlineage.exceptions import DiscoveryError
from eventlineage.models.common import SchemaFormat, SourceLocation
from eventlineage.models.event import Event
from eventlineage.models.participant import Consumer, Producer, Service
from eventlineage.models.schema import EventSchema
from eventlineage.models.topic import Topic

_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")
AVRO_SCHEMA_HINT = "avro"
PROTO_SCHEMA_HINT = "protobuf"


def slugify(value: str) -> str:
    """``"Order Service"`` -> ``"order-service"``."""
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return slug or "app"


class AsyncApiPlugin(DiscoveryPlugin):
    """AsyncAPI documents (JSON or YAML)."""

    name: ClassVar[str] = "asyncapi"
    patterns: ClassVar[tuple[str, ...]] = ("*.json", "*.yaml", "*.yml")
    priority: ClassVar[int] = 130
    description: ClassVar[str] = "AsyncAPI 2.x/3.x specifications"
    extensions: ClassVar[tuple[str, ...]] = (".json", ".yaml", ".yml")

    def detect(self, path: Path, head: str) -> int:
        """Return the detection confidence (0-100) for ``path``."""
        lowered = head.lower()
        window = lowered[:600]
        if re.search(r'"asyncapi"\s*:', lowered) or re.search(
            r"^asyncapi\s*:", lowered, re.MULTILINE
        ):
            return 100
        if "asyncapi" in window:
            return 95
        return 0

    def discover(self, path: Path, ctx: DiscoveryContext) -> DiscoveryResult:
        """Parse ``path`` and return the catalog fragments it declares."""
        document = self.load_json_or_yaml(path, ctx)
        if not isinstance(document, Mapping):
            raise DiscoveryError("AsyncAPI document root must be a mapping")
        if "asyncapi" not in document:
            raise DiscoveryError("not an AsyncAPI document (missing 'asyncapi' version)")
        result = DiscoveryResult()
        source = self.source(path, ctx)
        info = document.get("info") or {}
        title = info.get("title") if isinstance(info, Mapping) else None
        service_name = str(
            document.get("x-service-name")
            or document.get("x-eventlineage-service")
            or (slugify(str(title)) if title else slugify(path.stem))
        )
        namespace = str(document.get("x-namespace") or service_name)
        servers = document.get("servers") or {}
        broker = self._broker(servers)

        result.add_service(
            Service(
                name=service_name,
                description=str(title) if title else None,
                metadata={"asyncapi_version": str(document.get("asyncapi"))},
                source=source,
            )
        )

        events: dict[str, Event] = {}
        external: dict[str, Any] = {}

        def resolve(node: Any) -> Any:
            if not (isinstance(node, Mapping) and "$ref" in node):
                return node
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
                        f"unresolved AsyncAPI reference {reference!r}: {exc}", source=source
                    )
                    return None
            if target is None:
                result.warn(f"unresolved AsyncAPI reference {reference!r}", source=source)
                return None
            return target

        def register(
            message: Any,
            *,
            channel: str,
            role: str,
            fallback_name: str | None = None,
        ) -> None:
            message_data = resolve(message)
            if not isinstance(message_data, Mapping):
                return
            if isinstance(message, Mapping) and "$ref" in message:
                # An unnamed component message is known by its component key.
                fallback_name = fallback_name or str(message["$ref"]).rsplit("/", 1)[-1]
            name = str(
                message_data.get("name")
                or message_data.get("x-event-name")
                or fallback_name
                or channel
            )
            # A dotted message name is already fully qualified; otherwise the
            # document's namespace qualifies it.
            default_id = name if "." in name else f"{namespace}.{name}"
            event_id = str(message_data.get("x-event-id") or default_id)
            payload = resolve(message_data.get("payload"))
            schema = self._schema_from_payload(payload, message_data, source, name)
            event = events.get(event_id)
            if event is None:
                event = Event(
                    id=event_id,
                    name=name,
                    namespace=None if "." in name else namespace,
                    version=str(message_data.get("x-version") or "1.0.0"),
                    description=(
                        str(message_data["description"])
                        if isinstance(message_data.get("description"), str)
                        else None
                    ),
                    schema=schema,
                    topics=(channel,),
                    metadata={"asyncapi_channel": channel},
                    source=source,
                )
                events[event_id] = event
            else:
                event.topics = tuple(sorted({*event.topics, channel}))
                if event.schema is None:
                    event.schema = schema
            declared = {"service": service_name, "topics": [channel]}
            if role == "producer":
                event.producers = (
                    *event.producers,
                    cast(Producer, coerce_participant(declared, consumer=False, source=source)),
                )
            else:
                event.consumers = (
                    *event.consumers,
                    cast(Consumer, coerce_participant(declared, consumer=True, source=source)),
                )

        channels = document.get("channels") or {}
        if isinstance(channels, Mapping):
            for channel_name, channel_raw in channels.items():
                channel = resolve(channel_raw)
                if not isinstance(channel, Mapping):
                    continue
                address = str(channel.get("address") or channel_name)
                self._register_v2_operations(channel, address, register)
                self._register_v3_messages(channel, address, document, register, result, source)
                topic_meta = {
                    str(key): value for key, value in channel.items() if str(key).startswith("x-")
                }
                result.add_topic(
                    Topic(
                        name=address,
                        broker=broker,
                        description=(
                            str(channel["description"])
                            if isinstance(channel.get("description"), str)
                            else None
                        ),
                        source=source,
                        metadata={key: value for key, value in topic_meta.items() if value},
                    )
                )

        operations = document.get("operations")
        if isinstance(operations, Mapping):
            for operation_raw in operations.values():
                operation = resolve(operation_raw)
                if not isinstance(operation, Mapping):
                    continue
                action = str(operation.get("action") or "send")
                role = "producer" if action in {"send", "publish"} else "consumer"
                channel_ref = operation.get("channel")
                channel = resolve(channel_ref) if channel_ref is not None else None
                address = ""
                if isinstance(channel, Mapping):
                    address = str(channel.get("address") or "")
                elif isinstance(channel_ref, Mapping) and "$ref" in channel_ref:
                    address = str(channel_ref["$ref"]).rsplit("/", 1)[-1]
                messages = operation.get("messages") or (
                    list((channel or {}).get("messages", {}).values())
                    if isinstance(channel, Mapping)
                    else []
                )
                for message in messages if isinstance(messages, Sequence) else []:
                    register(message, channel=address, role=role)

        for event in sorted(events.values(), key=lambda item: item.id):
            result.add_event(event)
        for server_name, server in servers.items() if isinstance(servers, Mapping) else []:
            if isinstance(server, Mapping) and server.get("protocol"):
                result.warn(
                    f"server {server_name!r} uses protocol {server.get('protocol')!r}; "
                    "EventLineage only uses it as topic metadata",
                    source=source,
                )
        return result

    def _register_v2_operations(
        self, channel: Mapping[str, Any], address: str, register: Any
    ) -> None:
        for operation_name, role in (("subscribe", "producer"), ("publish", "consumer")):
            operation = channel.get(operation_name)
            if not isinstance(operation, Mapping):
                continue
            messages = operation.get("messages")
            if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
                for message in messages:
                    register(message, channel=address, role=role)
                continue
            message = operation.get("message")
            if message is None:
                continue
            if isinstance(message, Mapping) and "oneOf" in message:
                for option in message.get("oneOf") or []:
                    register(option, channel=address, role=role)
                continue
            register(message, channel=address, role=role)

    def _register_v3_messages(
        self,
        channel: Mapping[str, Any],
        address: str,
        document: Mapping[str, Any],
        register: Any,
        result: DiscoveryResult,
        source: SourceLocation,
    ) -> None:
        messages = channel.get("messages")
        if not isinstance(messages, Mapping):
            return
        has_operations = isinstance(document.get("operations"), Mapping)
        if has_operations:
            return  # directions come from the operations section
        for key, message in messages.items():
            register(message, channel=address, role="producer", fallback_name=str(key))

    def _schema_from_payload(
        self,
        payload: Any,
        message: Mapping[str, Any],
        source: SourceLocation,
        name: str,
    ) -> EventSchema | None:
        if payload is None:
            return None
        schema_format = str(message.get("schemaFormat") or "").lower()
        if AVRO_SCHEMA_HINT in schema_format:
            fmt = SchemaFormat.AVRO
        elif PROTO_SCHEMA_HINT in schema_format:
            fmt = SchemaFormat.PROTOBUF
        else:
            fmt = SchemaFormat.JSON_SCHEMA
        return EventSchema(format=fmt, definition=payload, name=name, source=source)

    @staticmethod
    def _broker(servers: Any) -> str | None:
        if not isinstance(servers, Mapping):
            return None
        protocols = {
            str(server.get("protocol")).lower()
            for server in servers.values()
            if isinstance(server, Mapping) and server.get("protocol")
        }
        for protocol, broker in (
            ("kafka", "kafka"),
            ("kafka-secure", "kafka"),
            ("amqp", "rabbitmq"),
            ("nats", "nats"),
        ):
            if protocol in protocols:
                return broker
        return sorted(protocols)[0] if protocols else None


__all__ = ["AsyncApiPlugin", "slugify"]
