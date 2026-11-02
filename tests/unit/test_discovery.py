"""Discovery plugins and the merge pipeline, exercised on written files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.conftest import make_event, make_schema

from eventlineage.core.merge import merge_events, merge_topics
from eventlineage.discovery.base import DiscoveryContext
from eventlineage.discovery.broker_exports import KafkaTopicsPlugin, SchemaRegistryExportPlugin
from eventlineage.discovery.catalog import CatalogPlugin
from eventlineage.discovery.service_config import ServiceConfigPlugin
from eventlineage.models.topic import Topic

CATALOG_YAML = """
version: 1
services:
  order-service:
    tier: tier-1
    produces: [order.created]
    consumes: [payment.captured]
events:
  - name: order.created
    version: 2.0.0
    topics: [orders.v1]
    schema:
      definition:
        type: object
        properties:
          order_id: {type: string}
    producers:
      - service: order-service
    consumers:
      - service: payment-service
  - name: payment.captured
    version: 1.0.0
"""


def write(root: Path, name: str, content: object) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content, indent=2), encoding="utf-8")
    return path


def context(root: Path) -> DiscoveryContext:
    from eventlineage.config import Config

    return DiscoveryContext(root=root, config=Config.load(root=root))


class TestCatalogPlugin:
    def test_discovers_events_services_topics(self, tmp_path: Path) -> None:
        path = write(tmp_path, "events/catalog.yaml", CATALOG_YAML)
        result = CatalogPlugin().discover(path, context(tmp_path))
        events = {event.id for event in result.events}
        assert events == {"order.created", "payment.captured"}
        assert {service.name for service in result.services} >= {"order-service"}
        declared = next(event for event in result.events if event.id == "order.created")
        assert declared.topics == ("orders.v1",)
        assert declared.version == "2.0.0"
        assert declared.producer_services == ("order-service",)
        assert declared.consumer_services == ("payment-service",)

    def test_inline_schema_is_parsed(self, tmp_path: Path) -> None:
        path = write(tmp_path, "events/catalog.yaml", CATALOG_YAML)
        result = CatalogPlugin().discover(path, context(tmp_path))
        event = next(entry for entry in result.events if entry.id == "order.created")
        assert event.schema is not None
        assert "order_id" in event.schema.field_paths()

    def test_missing_schema_file_is_a_diagnostic(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "events/broken.yaml",
            "version: 1\nevents:\n  - name: a.b\n    schema:\n      file: ../nope.json\n",
        )
        result = CatalogPlugin().discover(path, context(tmp_path))
        assert result.diagnostics
        assert result.events == []

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        from eventlineage.exceptions import ParseError

        path = write(tmp_path, "events/broken.yaml", "version: 1\nevents: [\n")
        with pytest.raises(ParseError):
            CatalogPlugin().discover(path, context(tmp_path))


class TestBrokerPlugins:
    def test_kafka_topics_export(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "kafka/topics.json",
            {
                "cluster": "prod",
                "topics": [
                    {"name": "orders.v1", "partitions": 12, "replication_factor": 3},
                    {"name": "payments.v1", "partitions": 6},
                ],
            },
        )
        result = KafkaTopicsPlugin().discover(path, context(tmp_path))
        assert [topic.name for topic in result.topics] == ["orders.v1", "payments.v1"]
        assert result.topics[0].partitions == 12
        assert result.topics[0].broker == "kafka"

    def test_schema_registry_export(self, tmp_path: Path) -> None:
        schema = json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
            }
        )
        path = write(
            tmp_path,
            "registry/subjects.json",
            {
                "subjects": [
                    {
                        "subject": "orders.created-value",
                        "versions": [
                            {"version": 1, "id": 1, "schemaType": "JSON", "schema": schema}
                        ],
                    }
                ]
            },
        )
        result = SchemaRegistryExportPlugin().discover(path, context(tmp_path))
        assert result.events
        event = result.events[0]
        assert event.id.startswith("orders.created")
        assert event.schema is not None
        assert event.schema.schema_id == "1"

    def test_service_config_yaml(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "services/platform.yaml",
            {
                "services": {
                    "billing-service": {
                        "tier": "tier-1",
                        "produces": ["payment.captured"],
                        "consumes": ["order.created"],
                    }
                }
            },
        )
        result = ServiceConfigPlugin().discover(path, context(tmp_path))
        assert {service.name for service in result.services} == {"billing-service"}
        assert {event.id for event in result.events} == {"order.created", "payment.captured"}
        produced = next(event for event in result.events if event.id == "payment.captured")
        assert produced.producer_services == ("billing-service",)
        assert produced.inferred is True


class TestMerging:
    def test_merge_is_commutative_for_disjoint_fields(self) -> None:
        left = make_event("a.b", producers=["x"], topics=["t1"], schema={"type": "object"})
        right = make_event("a.b", consumers=["y"], topics=["t2"], description="hello")
        merged = merge_events(left, right)
        assert merged.producer_services == ("x",)
        assert merged.consumer_services == ("y",)
        assert merged.topics == ("t1", "t2")
        assert merged.description == "hello"

    def test_merge_prefers_the_newest_version(self) -> None:
        left = make_event("a.b", version="1.0.0")
        right = make_event("a.b", version="9.9.9")
        assert merge_events(left, right).version == "9.9.9"
        assert merge_events(right, left).version == "9.9.9"

    def test_merge_conflict_keeps_first_schema(self) -> None:
        left = make_event("a.b", producers=["x"], schema={"type": "object", "title": "left"})
        right = make_event("a.b", consumers=["y"], schema={"type": "object", "title": "right"})
        merged, conflicted = merge_events(left, right, report_conflict=True)
        assert conflicted is True
        assert merged.active_schema().definition["title"] == "left"
        assert merged.consumer_services == ("y",)

    def test_merge_accepts_a_known_version_schema(self) -> None:

        left = make_event(
            "a.b",
            producers=["x"],
            version="2.0.0",
            schema={"type": "object", "title": "v2"},
            versions=[("1.0.0", {"type": "object", "title": "v1"})],
        )
        right = make_event("a.b", consumers=["y"], schema={"type": "object", "title": "v1"})
        merged, conflicted = merge_events(left, right, report_conflict=True)
        assert conflicted is False
        assert merged.consumer_services == ("y",)

    def test_merge_requires_matching_ids(self) -> None:
        with pytest.raises(ValueError):
            merge_events(make_event("a.b"), make_event("c.d"))

    def test_merge_topics_unions_sources(self) -> None:
        left = Topic(name="orders.v1", partitions=3, config={"retention.ms": "1000"})
        right = Topic(name="orders.v1", replication_factor=3, producers=("a",))
        merged = merge_topics(left, right)
        assert merged.partitions == 3
        assert merged.replication_factor == 3
        assert merged.config["retention.ms"] == "1000"

    def test_fingerprint_is_stable_across_merge_order(self) -> None:
        from eventlineage.core.catalog import EventCatalog

        fragments = [
            make_event("a.b", producers=["x"], schema=make_schema({"type": "object"})),
            make_event("a.b", consumers=["y"]),
            make_event("c.d", producers=["y"]),
        ]
        first, second = EventCatalog(project_name="t"), EventCatalog(project_name="t")
        for fragment in fragments:
            first.add_event(fragment)
        for fragment in reversed(fragments):
            second.add_event(fragment)
        assert first.normalize().fingerprint() == second.normalize().fingerprint()
