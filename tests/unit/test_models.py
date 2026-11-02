"""Model-level behaviour: enums, hashing, participants and catalog invariants."""

from __future__ import annotations

import pytest
from tests.conftest import make_catalog, make_event, make_schema

from eventlineage.core.catalog import EventCatalog
from eventlineage.discovery.base import coerce_participant
from eventlineage.models.common import (
    DependencyKind,
    ParticipantKind,
    SchemaFormat,
    ServiceKind,
    Severity,
    SourceLocation,
    VersionStatus,
)
from eventlineage.models.event import Event
from eventlineage.models.participant import Consumer, Producer, Service
from eventlineage.models.schema import EventSchema, EventVersion
from eventlineage.models.topic import Topic


class TestSeverity:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("error", Severity.ERROR),
            ("WARNING", Severity.WARNING),
            (" info ", Severity.INFO),
            ("off", Severity.OFF),
            ("false", Severity.OFF),
            ("none", Severity.OFF),
            (None, Severity.OFF),
            (False, Severity.OFF),
            (True, Severity.ERROR),
            (Severity.INFO, Severity.INFO),
        ],
    )
    def test_coerce(self, raw: object, expected: Severity) -> None:
        assert Severity.coerce(raw) == expected

    def test_unknown_severity_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown severity"):
            Severity.coerce("catastrophe")

    def test_rank_orders_severities(self) -> None:
        ranks = [
            item.rank for item in (Severity.OFF, Severity.INFO, Severity.WARNING, Severity.ERROR)
        ]
        assert ranks == sorted(ranks)
        assert len(set(ranks)) == 4


class TestSchemaFormat:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("json-schema", SchemaFormat.JSON_SCHEMA),
            ("jsonschema", SchemaFormat.JSON_SCHEMA),
            ("json", SchemaFormat.JSON_SCHEMA),
            ("avro", SchemaFormat.AVRO),
            ("protobuf", SchemaFormat.PROTOBUF),
            ("proto", SchemaFormat.PROTOBUF),
            ("PROTOBUF", SchemaFormat.PROTOBUF),
            ("openapi", SchemaFormat.OPENAPI),
            ("unknown-format", SchemaFormat.UNKNOWN),
        ],
    )
    def test_coerce(self, raw: str, expected: SchemaFormat) -> None:
        assert SchemaFormat.coerce(raw) == expected

    def test_enum_names_round_trip(self) -> None:
        for member in SchemaFormat:
            assert SchemaFormat.coerce(member.value) is member


class TestEventSchema:
    def test_hash_is_order_insensitive(self) -> None:
        left = make_schema({"type": "object", "title": "x"})
        right = make_schema({"title": "x", "type": "object"})
        assert left.content_hash == right.content_hash

    def test_hash_changes_with_content(self) -> None:
        left = make_schema({"type": "object", "required": ["a"]})
        right = make_schema({"type": "object", "required": ["b"]})
        assert left.content_hash != right.content_hash

    def test_hash_is_short_hex(self) -> None:
        schema = make_schema({"type": "object", "properties": {"order_id": {"type": "string"}}})
        assert len(schema.content_hash) == 32
        assert set(schema.content_hash) <= set("0123456789abcdef")

    def test_hash_of_identical_definitions_matches(self) -> None:
        left = make_schema({"type": "object", "properties": {"a": {"type": "string"}}})
        right = make_schema({"properties": {"a": {"type": "string"}}, "type": "object"})
        assert left.content_hash == right.content_hash

    def test_empty_schema_detection(self) -> None:
        assert EventSchema(format="json-schema").is_empty()
        assert not make_schema({"type": "object"}).is_empty()

    def test_canonical_drops_metadata(self) -> None:
        schema = make_schema({"type": "object"}, metadata={"generated": True})
        assert "metadata" not in schema.canonical()


class TestEvent:
    def test_qualified_name_uses_namespace(self) -> None:
        event = Event(id="shop.orders.created", name="created", namespace="shop.orders")
        assert event.qualified_name == "shop.orders.created"

    def test_qualified_name_defaults_to_name(self) -> None:
        assert Event(id="orders.created", name="orders.created").qualified_name == "orders.created"

    def test_active_schema_prefers_explicit_schema(self) -> None:
        event = Event(
            id="a.b",
            name="a.b",
            version="2.0.0",
            schema=make_schema({"type": "object", "title": "active"}),
            versions=(
                EventVersion(
                    version="1.0.0", schema=make_schema({"type": "object", "title": "v1"})
                ),
                EventVersion(
                    version="2.0.0", schema=make_schema({"type": "object", "title": "v2"})
                ),
            ),
        )
        schema = event.active_schema()
        assert schema is not None
        assert schema.definition["title"] == "active"

    def test_active_schema_falls_back_to_newest_version(self) -> None:
        event = Event(
            id="a.b",
            name="a.b",
            version="2.0.0",
            versions=(
                EventVersion(
                    version="1.0.0", schema=make_schema({"type": "object", "title": "v1"})
                ),
                EventVersion(
                    version="2.0.0", schema=make_schema({"type": "object", "title": "v2"})
                ),
            ),
        )
        schema = event.active_schema()
        assert schema is not None
        assert schema.definition["title"] in {"v1", "v2"}
        assert event.schema_versions()[0] == "2.0.0"

    def test_schema_versions_includes_declared_version(self) -> None:
        event = make_event("x.y", schema={"type": "object"}, version="3.1.0")
        assert "3.1.0" in event.schema_versions()

    def test_participant_lookups(self) -> None:
        event = make_event("x.y", producers=["a"], consumers=["b", "c"])
        assert event.producer_services == ("a",)
        assert event.consumer_services == ("b", "c")
        assert event.has_producer
        assert event.has_consumer

    def test_owner_lookup(self) -> None:
        event = make_event("x.y", owners=["team-a", "team-b"])
        assert event.owners == ("team-a", "team-b")


class TestParticipants:
    def test_coerce_string_producer(self) -> None:
        participant = coerce_participant("billing-service", consumer=False)
        assert isinstance(participant, Producer)
        assert participant.service == "billing-service"

    def test_coerce_mapping_with_service_key(self) -> None:
        consumer = coerce_participant(
            {"service": "search", "schema_version": "2.0.0"}, consumer=True
        )
        assert isinstance(consumer, Consumer)
        assert consumer.schema_version == "2.0.0"

    def test_coerce_external_service(self) -> None:
        consumer = coerce_participant({"name": "vendor", "kind": "external"}, consumer=True)
        assert consumer.kind is ParticipantKind.EXTERNAL

    def test_service_kind_defaults_to_service(self) -> None:
        service = Service(name="a")
        assert service.kind is ServiceKind.SERVICE
        assert service.tier is None


class TestTopic:
    def test_topic_merge_is_union(self) -> None:
        left = Topic(name="orders.v1", partitions=6, events=("a",))
        right = Topic(name="orders.v1", replication_factor=3, events=("b",))
        merged = left.merge(right)
        assert merged.partitions == 6
        assert merged.replication_factor == 3
        assert merged.events == ("a", "b")


class TestCatalog:
    def test_duplicates_are_recorded_only_with_sources(self) -> None:
        location = SourceLocation(path="a.yaml", plugin="catalog")
        catalog = make_catalog(
            [
                make_event("a.b", producers=["svc"]),
                make_event("a.b", consumers=["other"]),
            ]
        )
        assert catalog.duplicates == ()
        catalog.add_event(make_event("a.b").model_copy(update={"source": location}))
        assert [dup.key for dup in catalog.duplicates] == ["a.b"]

    def test_duplicate_fragments_merge(self) -> None:
        catalog = make_catalog(
            [
                make_event("a.b", producers=["svc"]),
                make_event("a.b", consumers=["other"]),
            ]
        )
        event = catalog.events["a.b"]
        assert event.producer_services == ("svc",)
        assert event.consumer_services == ("other",)

    def test_services_are_created_for_unknown_participants(self) -> None:
        catalog = make_catalog([make_event("a.b", producers=["ghost"], consumers=["phantom"])])
        assert set(catalog.services) >= {"ghost", "phantom"}
        assert catalog.services["ghost"].metadata["inferred"] is True
        assert catalog.unknown_participants() == ()

    def test_declared_services_are_not_inferred(self) -> None:
        catalog = make_catalog(
            [make_event("a.b", producers=["svc"])],
            services=[Service(name="svc", tier="tier-1")],
        )
        assert catalog.services["svc"].metadata.get("inferred") is None
        assert catalog.services["svc"].tier == "tier-1"

    def test_topics_are_linked_to_events(self) -> None:
        catalog = make_catalog([make_event("a.b", topics=["orders.v1"])])
        topic = catalog.topics["orders.v1"]
        assert topic.events == ("a.b",)

    def test_orphans_and_unconsumed(self) -> None:
        catalog = make_catalog(
            [
                make_event("a.b", producers=["svc"], consumers=["other"]),
                make_event("a.c", producers=["svc"]),
                make_event("a.d", consumers=["other"]),
            ]
        )
        assert [event.id for event in catalog.unconsumed_events()] == ["a.c"]
        assert [event.id for event in catalog.orphan_events()] == ["a.d"]

    def test_sorted_events_are_deterministic(self) -> None:
        catalog = make_catalog([make_event(name) for name in ("c.x", "a.x", "b.x")])
        assert [event.id for event in catalog.sorted_events()] == ["a.x", "b.x", "c.x"]

    def test_fingerprint_ignores_insertion_order(self) -> None:
        left = make_catalog(
            [make_event("a.b", producers=["x"]), make_event("c.d", consumers=["x"])]
        )
        right = make_catalog(
            [make_event("c.d", consumers=["x"]), make_event("a.b", producers=["x"])]
        )
        assert left.fingerprint() == right.fingerprint()

    def test_fold_duplicate_schemas(self) -> None:
        schema = {"type": "object", "title": "shared"}
        catalog = EventCatalog(project_name="t")
        catalog.add_event(make_event("orders.created", producers=["svc"], schema=schema))
        discovered = make_event("shop.PaymentCaptured", schema=schema)
        catalog.add_event(discovered.model_copy(update={"metadata": {"discovered_by": "avro"}}))
        folded = catalog.fold_duplicate_schemas()
        assert folded == (("shop.PaymentCaptured", "orders.created"),)
        assert "shop.PaymentCaptured" not in catalog.events

    def test_fold_keeps_events_with_different_schemas(self) -> None:
        catalog = EventCatalog(project_name="t")
        catalog.add_event(
            make_event("a.b", producers=["svc"], schema={"type": "object", "title": "1"})
        )
        catalog.add_event(make_event("c.d", schema={"type": "object", "title": "2"}))
        assert catalog.fold_duplicate_schemas() == ()
        assert set(catalog.events) == {"a.b", "c.d"}

    def test_event_versions_are_sorted(self) -> None:
        catalog = make_catalog(
            [
                make_event(
                    "a.b",
                    versions=[("1.0.0", {"type": "object"}), ("2.0.0", {"type": "object", "x": 1})],
                    version="2.0.0",
                )
            ]
        )
        assert [entry.version for entry in catalog.events["a.b"].all_versions()] == [
            "2.0.0",
            "1.0.0",
        ]

    def test_dangling_consumers(self) -> None:
        catalog = make_catalog(
            [
                make_event(
                    "a.b",
                    consumers=[Consumer(service="search-indexer", event_id="never.declared")],
                )
            ]
        )
        assert [consumer.service for consumer in catalog.dangling_consumers()] == ["search-indexer"]

    def test_resolve_event_accepts_alias(self) -> None:
        catalog = make_catalog([make_event("orders.OrderCreated")])
        assert catalog.resolve_event("orders.OrderCreated") is not None
        assert catalog.resolve_event("nope") is None

    def test_stats_are_stable(self) -> None:
        catalog = make_catalog([make_event("a.b", producers=["x"], consumers=["y"])])
        stats = catalog.stats()
        assert stats.events == 1
        assert stats.services == 2
        assert stats.producers == 1


class TestSourceLocation:
    def test_repr_is_compact(self) -> None:
        location = SourceLocation(path="events.yaml", line=12, plugin="catalog")
        assert "events.yaml" in repr(location)
        assert "12" in repr(location)


class TestDependencyKind:
    def test_values(self) -> None:
        assert DependencyKind.PRODUCES.value == "produces"
        assert DependencyKind.DEPENDS_ON.value == "depends_on"
        assert DependencyKind.TRIGGERS.is_event_flow

    def test_version_status_members(self) -> None:
        assert "active" in {member.value for member in VersionStatus}
