"""Protobuf discovery.

Event messages are selected by annotation (``// @event`` or a message option) or
by name suffix, both configurable. Producers and consumers come from gRPC
service definitions, which is the only place a ``.proto`` file states direction:

* ``rpc Foo(Req) returns (EventMessage)`` — the service produces the event,
* ``rpc Foo(EventMessage) returns (Resp)`` — the service consumes the event.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from eventlineage.discovery.base import DiscoveryContext, DiscoveryPlugin, DiscoveryResult
from eventlineage.exceptions import DiscoveryError
from eventlineage.models.common import SchemaFormat, ServiceKind
from eventlineage.models.event import Event
from eventlineage.models.participant import Consumer, Producer, Service
from eventlineage.models.schema import EventSchema
from eventlineage.parsers.protobuf_parser import (
    ProtoFile,
    ProtoMessage,
    definition_from_file,
    event_messages,
    parse_proto_file,
)


class ProtobufPlugin(DiscoveryPlugin):
    """``.proto`` files (proto2 and proto3)."""

    name: ClassVar[str] = "protobuf"
    patterns: ClassVar[tuple[str, ...]] = ("*.proto",)
    priority: ClassVar[int] = 100
    description: ClassVar[str] = "Protocol Buffers definitions (.proto)"
    extensions: ClassVar[tuple[str, ...]] = (".proto",)

    def detect(self, path: Path, head: str) -> int:
        """Return the detection confidence (0-100) for ``path``."""
        return 100 if path.suffix.lower() == ".proto" else 0

    def discover(self, path: Path, ctx: DiscoveryContext) -> DiscoveryResult:
        """Parse ``path`` and return the catalog fragments it declares."""
        text = self.read_text(path, ctx)
        try:
            proto_file = parse_proto_file(text, path=ctx.relative(path))
        except DiscoveryError:
            raise
        source = self.source(path, ctx)
        config = ctx.config.discovery.protobuf
        messages = event_messages(
            proto_file,
            suffixes=tuple(config.event_suffixes),
            annotations=config.use_annotations,
        )
        if not messages:
            return DiscoveryResult()

        result = DiscoveryResult()
        event_ids: dict[str, str] = {}
        for message in messages:
            event_name = message.name
            namespace = proto_file.package or path.parent.name or "default"
            event_id = f"{namespace}.{event_name}"
            event_ids[message.full_name or event_name] = event_id
            event_ids.setdefault(event_name, event_id)
            result.add_event(
                Event(
                    id=event_id,
                    name=event_name,
                    namespace=namespace,
                    version="1.0.0",
                    description=message.doc,
                    schema=EventSchema(
                        format=SchemaFormat.PROTOBUF,
                        definition=definition_from_file(proto_file, message),
                        raw=text,
                        name=message.full_name or message.name,
                        source=source,
                    ),
                    metadata={
                        "proto_message": message.full_name or message.name,
                        "proto_package": proto_file.package,
                        "proto_syntax": proto_file.syntax,
                        "discovered_by": self.name,
                    },
                    source=source,
                )
            )

        if config.include_services:
            self._link_services(proto_file, event_ids, result, source)
        return result

    def _link_services(
        self,
        proto_file: ProtoFile,
        event_ids: dict[str, str],
        result: DiscoveryResult,
        source: object,
    ) -> None:
        for service in proto_file.services:
            service_name = self._service_name(service.full_name or service.name)
            result.add_service(
                Service(
                    name=service_name,
                    kind=ServiceKind.SERVICE,
                    description=service.doc,
                    metadata={"proto_service": service.full_name or service.name},
                    source=source,
                )
            )
            productions: set[str] = set()
            consumptions: set[str] = set()
            for rpc in service.rpcs:
                produced = self._resolve(rpc.response, event_ids, proto_file)
                consumed = self._resolve(rpc.request, event_ids, proto_file)
                if produced:
                    productions.add(produced)
                if consumed:
                    consumptions.add(consumed)
            for event_id in sorted(productions):
                event = self._find(result, event_id)
                if event is not None:
                    event.producers = (
                        *event.producers,
                        Producer(service=service_name, source=source),
                    )
            for event_id in sorted(consumptions):
                event = self._find(result, event_id)
                if event is not None:
                    event.consumers = (
                        *event.consumers,
                        Consumer(service=service_name, source=source),
                    )

    @staticmethod
    def _service_name(full_name: str) -> str:
        """``shop.orders.OrderService`` -> ``order-service``."""
        cleaned = full_name.rsplit(".", 1)[-1]
        output: list[str] = []
        for index, character in enumerate(cleaned):
            if character.isupper() and index and not cleaned[index - 1].isupper():
                output.append("-")
            output.append(character.lower())
        return "".join(output)

    @staticmethod
    def _resolve(type_name: str, event_ids: dict[str, str], proto_file: ProtoFile) -> str | None:
        if type_name in event_ids:
            return event_ids[type_name]
        message: ProtoMessage | None = proto_file.find_message(type_name)
        if message is not None:
            return event_ids.get(message.full_name or message.name)
        return None

    @staticmethod
    def _find(result: DiscoveryResult, event_id: str) -> Event | None:
        for event in result.events:
            if event.id == event_id:
                return event
        return None


__all__ = ["ProtobufPlugin"]
