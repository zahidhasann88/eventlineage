"""The event catalog: the normalized, queryable view of a whole project.

Everything the CLI and SDK expose is derived from an :class:`EventCatalog`.
The catalog is a plain Pydantic model, so it serializes to stable JSON, can be
persisted as a scan cache, and can be constructed by hand in tests or by users
of the Python SDK.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import Field, PrivateAttr

from eventlineage.core.merge import SchemaConflict, merge_events
from eventlineage.models.base import EGBaseModel
from eventlineage.models.common import (
    DependencyKind,
    Diagnostic,
    ParticipantKind,
    ServiceKind,
    SourceLocation,
    VersionStatus,
)
from eventlineage.models.dependency import Dependency, EventRelationship
from eventlineage.models.event import Event
from eventlineage.models.participant import Consumer, Service
from eventlineage.models.topic import Topic


class SourceFile(EGBaseModel):
    """A file that contributed to the catalog, with a content digest."""

    path: str
    plugin: str
    digest: str
    events: int = 0
    topics: int = 0
    services: int = 0
    cached: bool = False

    @property
    def contributed(self) -> bool:
        """Whether this source contributed to the definition."""
        return bool(self.events or self.topics or self.services)


class DuplicateDefinition(EGBaseModel):
    """The same key was declared more than once (possibly in different files)."""

    kind: str = Field(description="event | topic | service")
    key: str
    sources: tuple[str, ...] = ()
    occurrences: int = 2


class CatalogStats(EGBaseModel):
    """Aggregate numbers used by reports and the ``analyze`` command."""

    events: int = 0
    services: int = 0
    topics: int = 0
    producers: int = 0
    consumers: int = 0
    relationships: int = 0
    schema_versions: int = 0
    events_without_producer: int = 0
    events_without_consumer: int = 0
    events_without_schema: int = 0
    services_without_events: int = 0
    orphan_topics: int = 0
    isolated_events: int = 0
    average_consumers_per_event: float = 0.0
    schema_formats: dict[str, int] = Field(default_factory=dict)
    top_producers: tuple[tuple[str, int], ...] = ()
    top_consumers: tuple[tuple[str, int], ...] = ()
    most_consumed_events: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class Resolution:
    """Outcome of resolving a user-supplied symbol."""

    kind: str
    key: str
    matches: tuple[str, ...]
    exact: bool

    @property
    def ambiguous(self) -> bool:
        """Whether identification needed a hint."""
        return len(self.matches) > 1

    def __bool__(self) -> bool:
        return bool(self.matches)


def _schema_hash(event: Event) -> str | None:
    """Content hash of the event's active schema, if it has one."""
    active = event.active_schema()
    return active.content_hash if active is not None else None


class ServiceEventIndex:
    """``service -> events`` lookup built from a single pass over the catalog.

    Both mappings keep catalog order (events sorted by id), so results are
    deterministic and identical to repeated ``events_produced_by`` calls.
    """

    __slots__ = ("_consumed", "_produced")

    def __init__(self, produced: dict[str, list[Event]], consumed: dict[str, list[Event]]) -> None:
        self._produced = {name: tuple(events) for name, events in produced.items()}
        self._consumed = {name: tuple(events) for name, events in consumed.items()}

    @classmethod
    def build(cls, events: Iterable[Event]) -> ServiceEventIndex:
        """Build the index for ``events`` in one pass."""
        produced: dict[str, list[Event]] = {}
        consumed: dict[str, list[Event]] = {}
        for event in events:
            for name in event.producer_services:
                produced.setdefault(name, []).append(event)
            for name in event.consumer_services:
                consumed.setdefault(name, []).append(event)
        return cls(produced, consumed)

    def produced_by(self, service: str) -> tuple[Event, ...]:
        """Events this service produces."""
        return self._produced.get(service, ())

    def consumed_by(self, service: str) -> tuple[Event, ...]:
        """Events this service consumes."""
        return self._consumed.get(service, ())

    @property
    def produced(self) -> Mapping[str, tuple[Event, ...]]:
        """Events produced, keyed by service name."""
        return self._produced

    @property
    def consumed(self) -> Mapping[str, tuple[Event, ...]]:
        """Events consumed, keyed by service name."""
        return self._consumed


class EventCatalog(EGBaseModel):
    """All events, topics, services and relationships discovered in a project."""

    project_name: str = "eventlineage"
    root: str = "."
    events: dict[str, Event] = Field(default_factory=dict)
    topics: dict[str, Topic] = Field(default_factory=dict)
    services: dict[str, Service] = Field(default_factory=dict)
    relationships: tuple[EventRelationship, ...] = ()
    dependencies: tuple[Dependency, ...] = ()
    sources: tuple[SourceFile, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    duplicates: tuple[DuplicateDefinition, ...] = ()
    config_fingerprint: str | None = None
    generated_at: str | None = None

    _alias_cache: dict[str, set[str]] | None = PrivateAttr(default=None)
    _relationship_keys: set[tuple[str, str, str]] | None = PrivateAttr(default=None)

    def sorted_events(self) -> tuple[Event, ...]:
        """Events in deterministic id order."""
        return tuple(self.events[key] for key in sorted(self.events))

    def sorted_services(self) -> tuple[Service, ...]:
        """Services in deterministic name order."""
        return tuple(self.services[key] for key in sorted(self.services))

    def sorted_topics(self) -> tuple[Topic, ...]:
        """Topics in deterministic name order."""
        return tuple(self.topics[key] for key in sorted(self.topics))

    def add_event(self, event: Event, *, conflicts: list[SchemaConflict] | None = None) -> None:
        """Insert or merge an event fragment.

        When the incoming fragment disagrees with the definition already stored,
        the first definition is kept and — if ``conflicts`` is provided — the
        rejection is reported so the caller can surface a diagnostic with the
        offending source.
        """
        existing = self.events.get(event.id)
        if existing is None:
            self.events[event.id] = event
        else:
            self._record_duplicate("event", event.id, existing.source, event.source)
            merged, conflicted = merge_events(existing, event, report_conflict=True)
            self.events[event.id] = merged
            if conflicted and conflicts is not None:
                conflicts.append(
                    SchemaConflict(
                        event_id=event.id,
                        kept=existing.source,
                        rejected=event.source,
                    )
                )
        self._alias_cache = None

    def add_topic(self, topic: Topic) -> None:
        """Insert or merge a topic fragment."""
        existing = self.topics.get(topic.name)
        if existing is None:
            self.topics[topic.name] = topic
        else:
            self._record_duplicate("topic", topic.name, existing.source, topic.source)
            self.topics[topic.name] = existing.merge(topic)

    def add_service(self, service: Service) -> None:
        """Insert or merge a service fragment."""
        existing = self.services.get(service.name)
        if existing is None:
            self.services[service.name] = service
        else:
            self._record_duplicate("service", service.name, existing.source, service.source)
            self.services[service.name] = existing.merge(service)

    def _record_duplicate(
        self,
        kind: str,
        key: str,
        existing: SourceLocation | None,
        incoming: SourceLocation | None,
    ) -> None:
        """Track repeated declarations so a rule can report them.

        Merging is the whole point of the discovery pipeline (one event may be
        described by a schema file, an AsyncAPI document and a catalog), so this
        is recorded as information, not as an error.
        """
        if existing is None and incoming is None:
            return
        if existing is not None and incoming is not None and str(existing) == str(incoming):
            return
        new_sources = {str(location) for location in (existing, incoming) if location is not None}
        for position, duplicate in enumerate(self.duplicates):
            if duplicate.kind == kind and duplicate.key == key:
                self.duplicates = (
                    *self.duplicates[:position],
                    duplicate.model_copy(
                        update={
                            "sources": tuple(sorted({*duplicate.sources, *new_sources})),
                            "occurrences": duplicate.occurrences + 1,
                        }
                    ),
                    *self.duplicates[position + 1 :],
                )
                return
        self.duplicates = (
            *self.duplicates,
            DuplicateDefinition(
                kind=kind,
                key=key,
                sources=tuple(sorted(new_sources)),
                occurrences=2,
            ),
        )

    def add_relationship(self, relationship: EventRelationship) -> None:
        """Add a relationship, ignoring duplicates.

        Round-tripping through a key set keeps this O(1) per insert: relation
        inference can generate tens of thousands of candidates and a linear
        scan per insert made that quadratic.
        """
        keys = self._relationship_keys
        if keys is None:
            keys = {known.key() for known in self.relationships}
            self._relationship_keys = keys
        if relationship.key() in keys:
            return
        keys.add(relationship.key())
        self.relationships = (*self.relationships, relationship)

    def set_relationships(self, relationships: Iterable[EventRelationship]) -> None:
        """Replace the relationship list and rebuild the de-duplication index."""
        ordered = tuple(sorted(relationships, key=lambda item: item.key()))
        self.relationships = ordered
        self._relationship_keys = {item.key() for item in ordered}

    def _aliases(self) -> dict[str, set[str]]:
        if self._alias_cache is None:
            aliases: dict[str, set[str]] = {}
            for event in self.events.values():
                for identifier in event.identifiers():
                    aliases.setdefault(identifier, set()).add(event.id)
                    aliases.setdefault(identifier.lower(), set()).add(event.id)
            self._alias_cache = aliases
        return self._alias_cache

    def resolve_event(self, reference: str) -> Event | None:
        """Resolve an event by id, qualified name, short name or ``name@version``.

        Exact matches win. A short name that is unique across namespaces also
        matches; ambiguous short names resolve to ``None`` so the caller can
        report the ambiguity instead of silently picking one.
        """
        if reference in self.events:
            return self.events[reference]
        matches = self._aliases().get(reference) or self._aliases().get(reference.lower())
        if not matches:
            return None
        if len(matches) == 1:
            return self.events[next(iter(matches))]
        # Prefer an exact qualified-name match over an ambiguous short name.
        normalised = reference.lower()
        for event_id in sorted(matches):
            event = self.events[event_id]
            if normalised in {event.qualified_name.lower(), event.ref.lower(), event_id.lower()}:
                return event
        return None

    def resolve_events(self, reference: str) -> tuple[Event, ...]:
        """All events matching ``reference`` (used to explain ambiguity)."""
        matches = self._aliases().get(reference) or self._aliases().get(reference.lower()) or set()
        return tuple(self.events[event_id] for event_id in sorted(matches))

    def resolve_service(self, reference: str) -> Service | None:
        """Resolve a service reference case-insensitively."""
        if reference in self.services:
            return self.services[reference]
        lowered = reference.lower()
        for name, service in self.services.items():
            if name.lower() == lowered:
                return service
        return None

    def service_event_index(self) -> ServiceEventIndex:
        """Build the produced/consumed index for every service in one pass.

        Calling :meth:`events_produced_by` / :meth:`events_consumed_by` once per
        service is ``O(services x events)``, which dominates discovery on large
        catalogs. Everything that needs the mapping for *all* services should
        build it once and query it instead.
        """
        return ServiceEventIndex.build(self.sorted_events())

    def events_produced_by(self, service: str) -> tuple[Event, ...]:
        """Every event produced by ``service``."""
        return tuple(event for event in self.sorted_events() if service in event.producer_services)

    def events_consumed_by(self, service: str) -> tuple[Event, ...]:
        """Every event consumed by ``service``."""
        return tuple(event for event in self.sorted_events() if service in event.consumer_services)

    def events_on_topic(self, topic: str) -> tuple[Event, ...]:
        """Every event published to ``topic``."""
        return tuple(event for event in self.sorted_events() if topic in event.topics)

    def services_for_event(self, event_id: str) -> tuple[str, ...]:
        """Services that produce or consume ``event_id``."""
        event = self.events.get(event_id)
        if event is None:
            return ()
        return event.participant_services()

    def orphan_events(self) -> tuple[Event, ...]:
        """Events with no known producer."""
        return tuple(event for event in self.sorted_events() if event.is_orphaned)

    def unconsumed_events(self) -> tuple[Event, ...]:
        """Events with no known consumer."""
        return tuple(event for event in self.sorted_events() if event.is_unconsumed)

    def dangling_consumers(self) -> tuple[Consumer, ...]:
        """Consumers that reference an event id the project never defines."""
        dangling: list[Consumer] = []
        for event in self.sorted_events():
            for consumer in event.consumers:
                if consumer.event_id and consumer.event_id not in self.events:
                    dangling.append(consumer)
        return tuple(dangling)

    def unknown_participants(self) -> tuple[tuple[str, str, SourceLocation | None], ...]:
        """Services referenced by participants but never declared."""
        unknown: list[tuple[str, str, SourceLocation | None]] = []
        for event in self.sorted_events():
            for participant in [*event.producers, *event.consumers]:
                if participant.service not in self.services:
                    unknown.append((participant.service, event.id, participant.source))
        return tuple(unknown)

    def event_versions(self) -> Iterator[tuple[Event, str, Any]]:
        """Iterate ``(event, version, entry)`` for every version."""
        for event in self.sorted_events():
            for version in event.all_versions():
                yield event, version.version, version

    def iter_dependencies(self) -> Iterator[Dependency]:
        """Iterate the materialised dependency list."""
        yield from self.dependencies

    def add_dependency(self, dependency: Dependency) -> None:
        """Append one dependency edge."""
        self.dependencies = (
            *self.dependencies,
            dependency,
        )

    def stats(self) -> CatalogStats:
        """Return aggregate catalog statistics."""
        events = self.sorted_events()
        formats: dict[str, int] = {}
        for event in events:
            schema = event.active_schema()
            key = schema.format.value if schema else "none"
            formats[key] = formats.get(key, 0) + 1
        consumer_counts = [(event.qualified_name, len(event.consumer_services)) for event in events]
        by_service_producers: dict[str, int] = {}
        by_service_consumers: dict[str, int] = {}
        for event in events:
            for service in event.producer_services:
                by_service_producers[service] = by_service_producers.get(service, 0) + 1
            for service in event.consumer_services:
                by_service_consumers[service] = by_service_consumers.get(service, 0) + 1
        participating = set(by_service_producers) | set(by_service_consumers)
        consumer_total = sum(len(event.consumer_services) for event in events)
        orphan_topics = sum(
            1
            for topic in self.topics.values()
            if not topic.producers and not topic.consumers and not topic.events
        )
        return CatalogStats(
            events=len(events),
            services=len(self.services),
            topics=len(self.topics),
            producers=sum(len(event.producers) for event in events),
            consumers=consumer_total,
            relationships=len(self.relationships),
            schema_versions=sum(event.version_count() for event in events),
            events_without_producer=sum(1 for event in events if event.is_orphaned),
            events_without_consumer=sum(1 for event in events if event.is_unconsumed),
            events_without_schema=sum(1 for event in events if not event.has_schema()),
            services_without_events=sum(1 for name in self.services if name not in participating),
            orphan_topics=orphan_topics,
            isolated_events=sum(
                1 for event in events if not event.producers and not event.consumers
            ),
            average_consumers_per_event=round(consumer_total / len(events), 2) if events else 0.0,
            schema_formats=dict(sorted(formats.items())),
            top_producers=_top(by_service_producers),
            top_consumers=_top(by_service_consumers),
            most_consumed_events=_top(dict(consumer_counts)),
        )

    def fingerprint(self) -> str:
        """Hash of the catalog content — used for cache and change detection."""
        payload = {
            "events": [
                {
                    "id": event.id,
                    "version": event.version,
                    "schema": _schema_hash(event),
                    "producers": sorted(event.producer_services),
                    "consumers": sorted(event.consumer_services),
                    "topics": sorted(event.topics),
                }
                for event in self.sorted_events()
            ],
            "topics": sorted(self.topics),
            "services": sorted(self.services),
            "relationships": sorted(relationship.key() for relationship in self.relationships),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def ensure_services(self) -> None:
        """Register placeholder services for participants that were never declared.

        Keeping the catalog internally consistent means the graph builder and
        the validation rules never have to special-case undeclared services;
        the fact that a service had to be inferred is recorded in metadata and
        reported by the ``service-undeclared`` rule.
        """
        for event in self.events.values():
            for participant in [*event.producers, *event.consumers]:
                if participant.service in self.services:
                    continue
                kind = (
                    ServiceKind.EXTERNAL
                    if participant.kind in {ParticipantKind.EXTERNAL}
                    else ServiceKind.SERVICE
                )
                self.add_service(
                    Service(
                        name=participant.service,
                        kind=kind,
                        metadata={"inferred": True, "declared": False},
                        source=participant.source,
                    )
                )

    def link_topics(self) -> None:
        """Create topics implied by event declarations that were never configured."""
        for event in self.events.values():
            for topic_name in event.topics:
                if topic_name not in self.topics:
                    self.topics[topic_name] = Topic(
                        name=topic_name,
                        metadata={"inferred": True},
                        source=event.source,
                    )
        # One pass over the events instead of one pass per topic, and one
        # assignment per topic instead of one per event: assigning a field on a
        # validated model costs far more than the set arithmetic.
        linked_events = {name: set(topic.events) for name, topic in self.topics.items()}
        linked_producers = {name: set(topic.producers) for name, topic in self.topics.items()}
        linked_consumers = {name: set(topic.consumers) for name, topic in self.topics.items()}
        for event in self.events.values():
            for topic_name in event.topics:
                linked_events[topic_name].add(event.id)
                linked_producers[topic_name].update(
                    producer.service for producer in event.producers
                )
                linked_consumers[topic_name].update(
                    consumer.service for consumer in event.consumers
                )
        for name, topic in self.topics.items():
            events = tuple(sorted(linked_events[name]))
            producers = tuple(sorted(linked_producers[name]))
            consumers = tuple(sorted(linked_consumers[name]))
            if events != tuple(topic.events):
                topic.events = events
            if producers != tuple(topic.producers):
                topic.producers = producers
            if consumers != tuple(topic.consumers):
                topic.consumers = consumers

    def build_dependencies(self) -> None:
        """Materialize the flat dependency list backing every export format."""
        dependencies: list[Dependency] = []
        for event in self.sorted_events():
            for producer in event.producers:
                dependencies.append(
                    Dependency(
                        source=f"service:{producer.service}",
                        target=f"event:{event.id}",
                        kind=DependencyKind.PRODUCES,
                        event_id=event.id,
                        service=producer.service,
                        source_location=producer.source or event.source,
                        inferred=producer.metadata.get("inferred", False),
                    )
                )
            for consumer in event.consumers:
                dependencies.append(
                    Dependency(
                        source=f"event:{event.id}",
                        target=f"service:{consumer.service}",
                        kind=DependencyKind.CONSUMES,
                        event_id=event.id,
                        service=consumer.service,
                        source_location=consumer.source or event.source,
                        inferred=consumer.metadata.get("inferred", False),
                    )
                )
            for topic_name in event.topics:
                dependencies.append(
                    Dependency(
                        source=f"event:{event.id}",
                        target=f"topic:{topic_name}",
                        kind=DependencyKind.ROUTES_VIA,
                        event_id=event.id,
                        topic=topic_name,
                        source_location=event.source,
                    )
                )
        for name in sorted(self.topics):
            topic = self.topics[name]
            for service in topic.producers:
                dependencies.append(
                    Dependency(
                        source=f"service:{service}",
                        target=f"topic:{name}",
                        kind=DependencyKind.PUBLISHES_TO,
                        topic=name,
                        service=service,
                        source_location=topic.source,
                    )
                )
            for service in topic.consumers:
                dependencies.append(
                    Dependency(
                        source=f"topic:{name}",
                        target=f"service:{service}",
                        kind=DependencyKind.SUBSCRIBES_TO,
                        topic=name,
                        service=service,
                        source_location=topic.source,
                    )
                )
        seen: set[tuple[str, str, str]] = set()
        ordered: list[Dependency] = []
        for dependency in dependencies:
            key = dependency.key()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(dependency)
        self.dependencies = tuple(ordered)

    def fold_duplicate_schemas(self) -> tuple[tuple[str, str], ...]:
        """Fold schema-only events into the catalog event that owns the schema.

        A repository usually contains both a catalog entry *and* the schema file
        it points at; when the schema file is discovered on its own it looks like
        an orphaned event. If a schema-only discovery has no participants and its
        schema is byte-identical to one owned by a participating event, the two
        are the same contract: topics and owners are merged into the owner and
        the duplicate is dropped. Events declared on purpose are never folded,
        however generic their schema is.

        Returns the ``(duplicate_id, owner_id)`` pairs that were folded.
        """
        candidates = sorted(
            self.events.values(),
            key=lambda event: (not (event.producers or event.consumers), event.id),
        )
        owners: dict[str, str] = {}
        for event in candidates:
            for entry in event.version_entries():
                schema = entry.schema
                if schema is not None and not schema.is_empty():
                    owners.setdefault(schema.content_hash, event.id)

        folded: list[tuple[str, str]] = []
        for event_id in sorted(self.events):
            event = self.events[event_id]
            if event.producers or event.consumers or "discovered_by" not in event.metadata:
                continue
            schema = event.active_schema()
            if schema is None:
                continue
            owner_id = owners.get(schema.content_hash)
            if owner_id is None or owner_id == event_id:
                continue
            owner = self.events[owner_id]
            if not (owner.producers or owner.consumers):
                continue
            if event.topics:
                owner.topics = tuple(sorted({*owner.topics, *event.topics}))
            if event.owners:
                owner.owners = tuple(sorted({*owner.owners, *event.owners}))
            if event.tags:
                owner.tags = tuple(sorted({*owner.tags, *event.tags}))
            del self.events[event_id]
            folded.append((event_id, owner_id))
        if folded:
            self._alias_cache = None
        return tuple(folded)

    def normalize(self) -> EventCatalog:
        """Run every consistency step and return ``self`` (fluent API)."""
        self.fold_duplicate_schemas()
        self.ensure_services()
        self.link_topics()
        self.build_dependencies()
        self._alias_cache = None
        return self

    def merged(self, other: EventCatalog) -> EventCatalog:
        """Merge another catalog into a copy of this one."""
        merged = self.model_copy(deep=True)
        for event in other.events.values():
            merged.add_event(event)
        for topic in other.topics.values():
            merged.add_topic(topic)
        for service in other.services.values():
            merged.add_service(service)
        for relationship in other.relationships:
            merged.add_relationship(relationship)
        merged.diagnostics = (*merged.diagnostics, *other.diagnostics)
        merged.sources = (*merged.sources, *other.sources)
        return merged.normalize()

    def to_dict(self, *, include_schemas: bool = True) -> dict[str, Any]:
        """Deterministic, JSON-ready representation of the catalog."""
        events: list[dict[str, Any]] = []
        for event in self.sorted_events():
            data: dict[str, Any] = {
                "id": event.id,
                "name": event.qualified_name,
                "namespace": event.namespace,
                "version": event.version,
                "status": event.status.value,
                "producers": sorted(event.producer_services),
                "consumers": sorted(event.consumer_services),
                "topics": sorted(event.topics),
                "versions": [entry.version for entry in event.all_versions()],
                "source": str(event.source) if event.source else None,
                "description": event.description,
                "owners": sorted(event.owners),
                "tags": sorted(event.tags),
            }
            active_schema = event.active_schema()
            if include_schemas and active_schema is not None:
                data["schema"] = {
                    "format": active_schema.format.value,
                    "hash": active_schema.content_hash,
                    "fields": [path for path, _ in active_schema.parsed().walk()],
                }
            events.append(data)
        return {
            "project": self.project_name,
            "events": events,
            "topics": [
                {
                    "name": topic.name,
                    "broker": topic.broker,
                    "partitions": topic.partitions,
                    "producers": list(topic.producers),
                    "consumers": list(topic.consumers),
                    "events": list(topic.events),
                }
                for topic in self.sorted_topics()
            ],
            "services": [
                {"name": service.name, "kind": service.kind.value, "tier": service.tier}
                for service in self.sorted_services()
            ],
            "relationships": [
                {
                    "source": relationship.source_event,
                    "target": relationship.target_event,
                    "kind": relationship.kind.value,
                    "inferred": relationship.inferred,
                }
                for relationship in sorted(self.relationships, key=lambda r: r.key())
            ],
            "stats": self.stats().model_dump(mode="json"),
            "fingerprint": self.fingerprint(),
        }


def _top(counts: dict[str, int], limit: int = 5) -> tuple[tuple[str, int], ...]:
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(ranked[:limit])


def build_catalog(
    *,
    events: Iterable[Event] = (),
    topics: Iterable[Topic] = (),
    services: Iterable[Service] = (),
    relationships: Iterable[EventRelationship] = (),
    project_name: str = "eventlineage",
    root: str = ".",
) -> EventCatalog:
    """Convenience constructor used by the SDK and tests."""
    catalog = EventCatalog(project_name=project_name, root=root)
    for service in services:
        catalog.add_service(service)
    for event in events:
        catalog.add_event(event)
    for topic in topics:
        catalog.add_topic(topic)
    for relationship in relationships:
        catalog.add_relationship(relationship)
    return catalog.normalize()


__all__ = [
    "CatalogStats",
    "DuplicateDefinition",
    "EventCatalog",
    "Resolution",
    "ServiceEventIndex",
    "SourceFile",
    "VersionStatus",
    "build_catalog",
]
