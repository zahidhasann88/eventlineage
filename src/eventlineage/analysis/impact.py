"""Change impact analysis.

Answers the question teams actually ask before merging a change: *if I touch
this event, what else moves?* The answer combines the dependency graph (who is
downstream) with the catalog (which services, topics and schemas those nodes
represent) and classifies the result into a risk level that can gate a deploy.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field

from eventlineage.core.catalog import EventCatalog
from eventlineage.exceptions import SymbolNotFoundError
from eventlineage.graph.algorithms import bfs, cycle_through, impact_propagation
from eventlineage.graph.builder import build_graph
from eventlineage.graph.model import Graph, event_node_id, service_node_id, topic_node_id
from eventlineage.models.base import EGBaseModel
from eventlineage.models.common import DependencyKind, NodeKind
from eventlineage.models.event import Event
from eventlineage.models.participant import Service


class RiskLevel(StrEnum):
    """How risky a change to the target is."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        """Ordering index of this risk level."""
        return {"low": 0, "medium": 1, "high": 2, "critical": 3}[self.value]

    @classmethod
    def from_score(cls, score: float) -> RiskLevel:
        """Risk level implied by a 0-100 score."""
        if score >= 70:
            return cls.CRITICAL
        if score >= 40:
            return cls.HIGH
        if score >= 15:
            return cls.MEDIUM
        return cls.LOW


class ImpactedService(EGBaseModel):
    """A service affected by the change."""

    name: str
    depth: int
    direct: bool
    score: float
    role: str = Field(default="consumer", description="consumer | producer | intermediate")
    tier: str | None = None
    path: tuple[str, ...] = ()

    @property
    def is_critical_tier(self) -> bool:
        """Whether a service tier counts as business critical."""
        if not self.tier:
            return False
        return self.tier.lower().replace("-", "") in {"tier1", "p0", "critical"}


class ImpactedEvent(EGBaseModel):
    """An event affected by the change (including the target itself)."""

    id: str
    name: str
    depth: int
    version: str
    schema_format: str | None = None
    schema_hash: str | None = None
    producers: tuple[str, ...] = ()
    consumers: tuple[str, ...] = ()


class SchemaReference(EGBaseModel):
    """A schema a downstream participant may need to update."""

    event_id: str
    version: str
    format: str | None = None
    hash: str | None = None
    source: str | None = None


class ImpactReport(EGBaseModel):
    """Result of :func:`analyze_impact`."""

    target: str
    target_kind: str
    resolved: str | None = None
    description: str | None = None
    risk: RiskLevel = RiskLevel.LOW
    risk_score: float = 0.0
    max_depth: int = 0
    direct_consumers: tuple[str, ...] = ()
    transitive_consumers: tuple[str, ...] = ()
    producers: tuple[str, ...] = ()
    affected_services: tuple[ImpactedService, ...] = ()
    affected_events: tuple[ImpactedEvent, ...] = ()
    related_topics: tuple[str, ...] = ()
    affected_schemas: tuple[SchemaReference, ...] = ()
    cycles_detected: tuple[tuple[str, ...], ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def affected_service_count(self) -> int:
        """Number of distinct services in the blast radius."""
        return len(self.affected_services)

    def top_services(self, limit: int = 5) -> tuple[ImpactedService, ...]:
        """Return the most affected services, highest score first."""
        return tuple(self.affected_services[:limit])

    def summary(self) -> str:
        """Return a one-line human summary."""
        if not self.affected_services and not self.affected_events:
            return f"{self.target}: no downstream impact detected"
        return (
            f"{self.target}: {len(self.affected_services)} service(s) affected, "
            f"{len(self.affected_events)} event(s), depth {self.max_depth}, "
            f"risk {self.risk.value.upper()} ({self.risk_score:.0f}/100)"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready representation."""
        return {
            "target": self.target,
            "target_kind": self.target_kind,
            "resolved": self.resolved,
            "risk": self.risk.value,
            "risk_score": round(self.risk_score, 1),
            "max_depth": self.max_depth,
            "direct_consumers": list(self.direct_consumers),
            "transitive_consumers": list(self.transitive_consumers),
            "producers": list(self.producers),
            "affected_services": [
                {
                    "name": service.name,
                    "depth": service.depth,
                    "direct": service.direct,
                    "score": round(service.score, 3),
                    "tier": service.tier,
                    "path": list(service.path),
                }
                for service in self.affected_services
            ],
            "affected_events": [
                {
                    "id": event.id,
                    "name": event.name,
                    "depth": event.depth,
                    "version": event.version,
                    "schema_format": event.schema_format,
                    "schema_hash": event.schema_hash,
                }
                for event in self.affected_events
            ],
            "related_topics": list(self.related_topics),
            "affected_schemas": [
                {
                    "event": schema.event_id,
                    "version": schema.version,
                    "format": schema.format,
                    "hash": schema.hash,
                    "source": schema.source,
                }
                for schema in self.affected_schemas
            ],
            "cycles_detected": [list(cycle) for cycle in self.cycles_detected],
            "notes": list(self.notes),
            "summary": self.summary(),
        }


def _resolve_target(catalog: EventCatalog, target: str) -> tuple[str, str, Event | Service | None]:
    """Resolve a user-supplied symbol to ``(kind, node_id, entity)``."""
    raw = target.strip()
    if raw.startswith(("event:", "service:", "topic:")):
        kind, _, reference = raw.partition(":")
        raw = reference
        if kind == "event":
            event = catalog.resolve_event(raw)
            if event is None:
                raise SymbolNotFoundError(raw, "event", candidates=tuple(catalog.events))
            return "event", event_node_id(event.id), event
        if kind == "service":
            service = catalog.resolve_service(raw)
            if service is None:
                raise SymbolNotFoundError(raw, "service", candidates=tuple(catalog.services))
            return "service", service_node_id(service.name), service
        if raw not in catalog.topics:
            raise SymbolNotFoundError(raw, "topic", candidates=tuple(catalog.topics))
        return "topic", topic_node_id(raw), None

    event = catalog.resolve_event(raw)
    if event is not None:
        return "event", event_node_id(event.id), event
    service = catalog.resolve_service(raw)
    if service is not None:
        return "service", service_node_id(service.name), service
    if raw in catalog.topics:
        return "topic", topic_node_id(raw), None
    raise SymbolNotFoundError(
        target,
        "event or service",
        candidates=tuple(catalog.events) + tuple(catalog.services) + tuple(catalog.topics),
    )


def analyze_impact(
    catalog: EventCatalog,
    target: str,
    *,
    graph: Graph | None = None,
    max_depth: int | None = None,
    risk_high_services: int = 5,
    risk_critical_services: int = 15,
    include_consumed: bool = True,
) -> ImpactReport:
    """Compute the blast radius of changing an event, service or topic.

    Traversal follows edge direction: ``service -> event`` (produces),
    ``event -> service`` (consumes), ``event -> topic`` and ``event -> event``
    (triggers). Depth and score come from :func:`impact_propagation`, which
    decays the score per hop and takes the maximum over multiple routes instead
    of summing them.
    """
    kind, node_id, entity = _resolve_target(catalog, target)
    graph = graph or build_graph(catalog)
    start = graph.require(node_id)
    step_kinds = (
        DependencyKind.PRODUCES,
        DependencyKind.CONSUMES,
        DependencyKind.ROUTES_VIA,
        DependencyKind.PUBLISHES_TO,
        DependencyKind.SUBSCRIBES_TO,
        DependencyKind.TRIGGERS,
        DependencyKind.DERIVED_FROM,
    )
    if not include_consumed:
        step_kinds = tuple(  # type: ignore[assignment]
            kind_ for kind_ in step_kinds if kind_ is not DependencyKind.CONSUMES
        )

    scores = impact_propagation(graph, start, kinds=step_kinds, max_depth=max_depth)
    traversal = bfs(graph, start, kinds=step_kinds, max_depth=max_depth)

    direct_consumers: set[str] = set()
    if kind == "event" and isinstance(entity, Event):
        direct_consumers.update(entity.consumer_services)
    for node_index in graph.successors(
        start, (DependencyKind.CONSUMES, DependencyKind.SUBSCRIBES_TO)
    ):
        node = graph.node(node_index)
        if node.kind == NodeKind.SERVICE:
            direct_consumers.add(node.ref)

    producers: list[str] = []
    if isinstance(entity, Event):
        producers = list(entity.producer_services)

    affected_services: list[ImpactedService] = []
    affected_events: list[ImpactedEvent] = []
    topics: set[str] = set()
    event_ids: set[str] = set()
    if kind == "event" and isinstance(entity, Event):
        event_ids.add(entity.id)
        topics.update(entity.topics)
    if kind == "topic":
        topics.add(node_id.split(":", 1)[1])
        for event in catalog.events_on_topic(topics.copy().pop()):
            event_ids.add(event.id)

    for node_index, depth in traversal.depths.items():
        if node_index == start:
            continue
        node = graph.node(node_index)
        score = scores.get(node_index, 0.0)
        path = tuple(graph.node(index).ref for index in traversal.path_to(graph, node_index))
        if node.kind == NodeKind.SERVICE:
            service = catalog.services.get(node.ref)
            affected_services.append(
                ImpactedService(
                    name=node.ref,
                    depth=depth,
                    direct=node.ref in direct_consumers,
                    score=round(score, 3),
                    role="consumer",
                    tier=service.tier if service else None,
                    path=path,
                )
            )
        elif node.kind == NodeKind.EVENT:
            affected = catalog.events.get(node.ref)
            if affected is None:  # pragma: no cover - graph is built from the catalog
                continue
            event_ids.add(affected.id)
            topics.update(affected.topics)
            schema = affected.active_schema()
            affected_events.append(
                ImpactedEvent(
                    id=affected.id,
                    name=affected.qualified_name,
                    depth=depth,
                    version=affected.version,
                    schema_format=schema.format.value if schema else None,
                    schema_hash=schema.content_hash if schema else None,
                    producers=affected.producer_services,
                    consumers=affected.consumer_services,
                )
            )
        elif node.kind == NodeKind.TOPIC:
            topics.add(node.ref)

    affected_services.sort(key=lambda service: (service.depth, service.name))
    affected_events.sort(key=lambda event: (event.depth, event.id))

    schemas: list[SchemaReference] = []
    for event_id in sorted(event_ids):
        owner = catalog.events.get(event_id)
        if owner is None:
            continue
        schema = owner.active_schema()
        if schema is None:
            continue
        schemas.append(
            SchemaReference(
                event_id=owner.id,
                version=owner.version,
                format=schema.format.value,
                hash=schema.content_hash,
                source=str(schema.source) if schema.source else None,
            )
        )

    cycles = _detect_cycles(graph, start, step_kinds)
    score, risk, notes = _classify_risk(
        affected_services,
        max_depth=max((service.depth for service in affected_services), default=0),
        high=risk_high_services,
        critical=risk_critical_services,
    )

    resolved = None
    description = None
    if isinstance(entity, Event):
        resolved = entity.ref
        description = entity.description or (
            f"{len(entity.producer_services)} producer(s), "
            f"{len(entity.consumer_services)} consumer(s)"
        )
    elif isinstance(entity, Service):
        resolved = entity.name
        description = entity.description

    return ImpactReport(
        target=target,
        target_kind=kind,
        resolved=resolved,
        description=description,
        risk=risk,
        risk_score=score,
        max_depth=max((service.depth for service in affected_services), default=0),
        direct_consumers=tuple(sorted(direct_consumers)),
        transitive_consumers=tuple(
            sorted(service.name for service in affected_services if not service.direct)
        ),
        producers=tuple(sorted(producers)),
        affected_services=tuple(affected_services),
        affected_events=tuple(affected_events),
        related_topics=tuple(sorted(topics)),
        affected_schemas=tuple(schemas),
        cycles_detected=cycles,
        notes=notes,
    )


def _detect_cycles(
    graph: Graph,
    start: int,
    kinds: tuple[DependencyKind, ...],
) -> tuple[tuple[str, ...], ...]:
    """A cycle through the target, if any (it makes impact analysis open-ended)."""
    cycle = cycle_through(graph, start, kinds=kinds)
    return (tuple(graph.node(index).ref for index in cycle),) if cycle else ()


def _classify_risk(
    services: list[ImpactedService],
    *,
    max_depth: int,
    high: int,
    critical: int,
) -> tuple[float, RiskLevel, tuple[str, ...]]:
    """Turn the affected set into a 0-100 score and a risk level."""
    notes: list[str] = []
    count = len(services)
    direct = sum(1 for service in services if service.direct)
    critical_tier = [service for service in services if service.is_critical_tier]

    score = 0.0
    score += min(count, critical) / max(critical, 1) * 55.0
    score += min(direct, 10) / 10 * 20.0
    score += min(max_depth, 6) / 6 * 15.0
    if critical_tier:
        score += 10.0
        notes.append(
            "affects critical-tier service(s): "
            + ", ".join(sorted(service.name for service in critical_tier))
        )
    if count >= critical:
        notes.append(f"wide blast radius: {count} services downstream")
    elif count >= high:
        notes.append(f"moderate blast radius: {count} services downstream")
    if max_depth >= 4:
        notes.append(f"deep dependency chain ({max_depth} hops)")
    if not services:
        notes.append("no downstream consumers found; impact is contained")
    score = round(min(score, 100.0), 1)
    return score, RiskLevel.from_score(score), tuple(notes)


__all__ = [
    "ImpactReport",
    "ImpactedEvent",
    "ImpactedService",
    "RiskLevel",
    "SchemaReference",
    "analyze_impact",
]
