"""Broker and registry export plugins.

EventLineage never requires a running broker. Instead it understands the export
formats that teams already keep in their repositories:

* Kafka topic descriptions (``kafka-topics --describe`` JSON exports, Terraform
  output, Confluent Control Center exports),
* Schema Registry subject exports (``{"subjects": [...]}``).

When you *do* have a live cluster, :mod:`eventlineage.integrations` can refresh
these documents for you.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

from eventlineage.discovery.base import (
    DiscoveryContext,
    DiscoveryPlugin,
    DiscoveryResult,
    coerce_service,
    first_json_keys,
    yaml_top_level_keys,
)
from eventlineage.exceptions import DiscoveryError
from eventlineage.models.common import SchemaFormat, SourceLocation
from eventlineage.models.event import Event
from eventlineage.models.schema import EventSchema
from eventlineage.models.topic import Topic
from eventlineage.parsers import detect_schema_format
from eventlineage.utils.versioning import version_key

_SUBJECT_VALUE_SUFFIXES = ("-value", "-key", ".value", ".key")


def _strip_subject_suffix(subject: str) -> str:
    lowered = subject.lower()
    for suffix in _SUBJECT_VALUE_SUFFIXES:
        if lowered.endswith(suffix):
            return subject[: len(subject) - len(suffix)]
    return subject


class KafkaTopicsPlugin(DiscoveryPlugin):
    """Kafka topic configuration exports (offline)."""

    name: ClassVar[str] = "kafka-topics"
    patterns: ClassVar[tuple[str, ...]] = ("*.json", "*.yaml", "*.yml")
    priority: ClassVar[int] = 95
    description: ClassVar[str] = "Kafka topic configuration exports (topics, partitions, producers)"
    extensions: ClassVar[tuple[str, ...]] = (".json", ".yaml", ".yml")

    def detect(self, path: Path, head: str) -> int:
        """Return the detection confidence (0-100) for ``path``."""
        stripped = head.lstrip()
        keys = (
            first_json_keys(head) if stripped.startswith(("{", "[")) else yaml_top_level_keys(head)
        )
        lowered = head.lower()
        if "asyncapi" in lowered[:400] or "openapi" in lowered[:400]:
            return 0
        if "subjects" in keys:
            return 0
        if "topics" not in keys:
            return 0
        if "services" in keys or "events" in keys:
            return 0  # catalog/service-config documents describe topics too
        if (
            '"partitions"' in lowered
            or '"replication_factor"' in lowered
            or "partitions:" in lowered
        ):
            return 90
        if '"events"' in lowered and '"name"' in lowered:
            return 0
        return 0

    def discover(self, path: Path, ctx: DiscoveryContext) -> DiscoveryResult:
        """Parse ``path`` and return the catalog fragments it declares."""
        document = self.load_json_or_yaml(path, ctx)
        result = DiscoveryResult()
        source = self.source(path, ctx)
        broker = "kafka"
        if isinstance(document, Mapping):
            broker = str(document.get("broker") or document.get("cluster_type") or "kafka")
        entries = self._topic_entries(document)
        if not entries:
            raise DiscoveryError("no topics found in Kafka export")
        for name, raw in entries:
            topic = self._build_topic(name, raw, source, broker, result)
            if topic is not None:
                result.add_topic(topic)
        return result

    def _topic_entries(self, document: Any) -> list[tuple[str, Any]]:
        if isinstance(document, Mapping):
            topics = document.get("topics")
            if isinstance(topics, Mapping):
                return [(str(key), value) for key, value in topics.items()]
            if isinstance(topics, Sequence) and not isinstance(topics, (str, bytes)):
                entries: list[tuple[str, Any]] = []
                for item in topics:
                    if isinstance(item, Mapping):
                        name = item.get("name") or item.get("topic")
                        if name:
                            entries.append((str(name), item))
                    elif isinstance(item, str):
                        entries.append((item, {}))
                return entries
            # A dictionary of topic name -> config, without a "topics" wrapper.
            if (
                topics is None
                and document
                and all(
                    isinstance(value, Mapping)
                    and any(key in value for key in ("partitions", "replication_factor", "config"))
                    for value in document.values()
                )
            ):
                return [(str(key), value) for key, value in document.items()]
        return []

    def _build_topic(
        self,
        name: str,
        raw: Any,
        source: SourceLocation,
        broker: str,
        result: DiscoveryResult,
    ) -> Topic | None:
        if not isinstance(raw, Mapping):
            return Topic(name=name, broker=broker, source=source)
        data = dict(raw)
        config = {str(key): value for key, value in (data.pop("config", {}) or {}).items()}
        producers = [str(item) for item in _sequence(data.pop("producers", None))]
        consumers = [str(item) for item in _sequence(data.pop("consumers", None))]
        events = [str(item) for item in _sequence(data.pop("events", None))]
        for participant in producers:
            result.add_service(coerce_service(participant, source=source))
        for participant in consumers:
            result.add_service(coerce_service(participant, source=source))
        retention = data.pop("retention_ms", None) or config.get("retention.ms")
        try:
            return Topic(
                name=name,
                broker=broker,
                partitions=_int_or_none(data.pop("partitions", None)),
                replication_factor=_int_or_none(
                    data.pop("replication_factor", data.pop("replicas", None))
                ),
                retention_ms=_int_or_none(retention),
                cleanup_policy=(
                    str(config.get("cleanup.policy")) if config.get("cleanup.policy") else None
                ),
                config=config,
                producers=tuple(sorted(set(producers))),
                consumers=tuple(sorted(set(consumers))),
                events=tuple(sorted(set(events))),
                description=str(data["description"]) if "description" in data else None,
                source=source,
                metadata={
                    key: value
                    for key, value in data.items()
                    if key not in {"name", "topic", "description"}
                },
            )
        except ValueError:
            result.warn(f"invalid topic declaration {name!r}", source=source)
            return None


class SchemaRegistryExportPlugin(DiscoveryPlugin):
    """Schema Registry subject exports become versioned events."""

    name: ClassVar[str] = "schema-registry-export"
    patterns: ClassVar[tuple[str, ...]] = ("*.json", "*.yaml", "*.yml")
    priority: ClassVar[int] = 110
    description: ClassVar[str] = "Schema Registry subject export (subjects, versions, schemas)"
    extensions: ClassVar[tuple[str, ...]] = (".json", ".yaml", ".yml")

    def detect(self, path: Path, head: str) -> int:
        """Return the detection confidence (0-100) for ``path``."""
        stripped = head.lstrip()
        keys = (
            first_json_keys(head) if stripped.startswith(("{", "[")) else yaml_top_level_keys(head)
        )
        lowered = head.lower()
        if "subjects" in keys:
            return 96
        if '"subject"' in lowered and '"schema"' in lowered and '"version"' in lowered:
            return 92
        return 0

    def discover(self, path: Path, ctx: DiscoveryContext) -> DiscoveryResult:
        """Parse ``path`` and return the catalog fragments it declares."""
        document = self.load_json_or_yaml(path, ctx)
        source = self.source(path, ctx)
        result = DiscoveryResult()
        by_event: dict[str, dict[str, Any]] = {}

        for entry in self._subject_entries(document):
            subject = str(entry.get("subject") or entry.get("name") or "")
            # A key schema is a second contract on the topic, not a version of the value.
            if not subject or subject.lower().endswith(("-key", ".key")):
                continue
            schema_raw = entry.get("schema")
            version = str(entry.get("version", "1"))
            schema_type = str(entry.get("schemaType") or entry.get("schema_type") or "AVRO").upper()
            definition = self._decode_schema(schema_raw)
            if definition is None:
                result.warn(f"subject {subject!r} has no usable schema", source=source)
                continue
            format = {
                "AVRO": SchemaFormat.AVRO,
                "JSON": SchemaFormat.JSON_SCHEMA,
                "JSONSCHEMA": SchemaFormat.JSON_SCHEMA,
                "PROTOBUF": SchemaFormat.PROTOBUF,
            }.get(schema_type, detect_schema_format(definition))
            event_name = str(entry.get("x-event-name") or _strip_subject_suffix(subject))
            record = by_event.setdefault(
                event_name,
                {"versions": [], "subject": subject, "format": format},
            )
            record["versions"].append(
                {
                    "version": version,
                    "schema": EventSchema(
                        format=format,
                        definition=definition,
                        raw=schema_raw if isinstance(schema_raw, str) else None,
                        version=version,
                        schema_id=str(entry["id"]) if "id" in entry else None,
                        source=source,
                    ),
                }
            )

        for event_name, record in sorted(by_event.items()):
            versions = sorted(
                record["versions"], key=lambda item: version_key(str(item["version"]))
            )
            latest = versions[-1]
            from eventlineage.models.schema import EventVersion

            result.add_event(
                Event(
                    id=event_name,
                    name=event_name,
                    version=latest["version"],
                    schema=latest["schema"],
                    versions=tuple(
                        EventVersion(version=item["version"], schema=item["schema"])
                        for item in versions
                    ),
                    metadata={
                        "subject": record["subject"],
                        "registry": "schema-registry",
                        "discovered_by": self.name,
                    },
                    source=source,
                )
            )
        return result

    def _subject_entries(self, document: Any) -> list[Mapping[str, Any]]:
        if isinstance(document, Mapping):
            subjects = document.get("subjects")
            if isinstance(subjects, list):
                entries: list[Mapping[str, Any]] = []
                for item in subjects:
                    if isinstance(item, Mapping):
                        if isinstance(item.get("versions"), list):
                            for version in item["versions"]:
                                if isinstance(version, Mapping):
                                    entries.append({**version, "subject": item.get("subject")})
                        else:
                            entries.append(item)
                return entries
            if isinstance(subjects, Mapping):
                entries = []
                for subject, value in subjects.items():
                    if isinstance(value, Mapping):
                        entries.append({**value, "subject": subject})
                    elif isinstance(value, list):
                        for version in value:
                            if isinstance(version, Mapping):
                                entries.append({**version, "subject": subject})
                return entries
            if "schema" in document:
                return [document]
        if isinstance(document, list):
            return [item for item in document if isinstance(item, Mapping)]
        return []

    @staticmethod
    def _decode_schema(raw: Any) -> Any:
        if isinstance(raw, (Mapping, list)):
            return raw
        if isinstance(raw, str):
            stripped = raw.strip()
            if not stripped:
                return None
            if stripped.startswith(("{", "[")):
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError:
                    return stripped
            return stripped
        return None


def _sequence(node: Any) -> list[Any]:
    if node is None:
        return []
    if isinstance(node, (list, tuple)):
        return list(node)
    return [node]


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["KafkaTopicsPlugin", "SchemaRegistryExportPlugin"]
