"""Translate a normalized catalog into an immutable dependency graph."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping

from eventlineage.core.catalog import EventCatalog
from eventlineage.graph.model import (
    Edge,
    Graph,
    Node,
    event_node_id,
    service_node_id,
    topic_node_id,
)
from eventlineage.models.common import DependencyKind, NodeKind, RelationshipKind
from eventlineage.models.dependency import EventRelationship

logger = logging.getLogger(__name__)

DESCENDANT_FLOW_KINDS = (
    DependencyKind.TRIGGERS,
    DependencyKind.DERIVED_FROM,
)


#: Services touching more than this many event pairs are skipped: with N events
#: consumed and M produced the naive inference is O(N*M), which explodes for
#: "god" services (a data warehouse consuming 500 events and emitting 50).
MAX_INFERENCE_PAIRS = 2_000


def infer_relationships(
    catalog: EventCatalog, *, confidence: float = 0.5, max_pairs: int = MAX_INFERENCE_PAIRS
) -> tuple[EventRelationship, ...]:
    """Infer event-to-event relationships from service behaviour.

    A service that consumes ``order.created`` and produces ``order.paid``
    implies a causal link between the two events. Explicitly declared
    relationships always win; inferred ones are marked as such and carry a
    lower confidence so reports can present them differently.

    Inference is intentionally shallow (one hop through a service): transitive
    inference would infer causality between events in the same domain that are
    merely handled by the same worker. Services whose consumed x produced pair
    count exceeds ``max_pairs`` are skipped, which keeps the pass linear in
    practice; ``docs/performance.md`` explains the trade-off.
    """
    known = {relationship.key() for relationship in catalog.relationships}
    inferred: list[EventRelationship] = []
    skipped = 0
    index = catalog.service_event_index()
    for service in catalog.sorted_services():
        consumed = index.consumed_by(service.name)
        produced = index.produced_by(service.name)
        if len(consumed) * len(produced) > max_pairs:
            skipped += 1
            logger.debug(
                "skipping relationship inference for %s: %d consumed x %d produced pairs",
                service.name,
                len(consumed),
                len(produced),
            )
            continue
        for upstream in consumed:
            for downstream in produced:
                if upstream.id == downstream.id:
                    continue
                candidate = EventRelationship(
                    source_event=upstream.id,
                    target_event=downstream.id,
                    kind=RelationshipKind.TRIGGERS,
                    inferred=True,
                    confidence=confidence,
                    via_service=service.name,
                )
                if candidate.key() in known:
                    continue
                known.add(candidate.key())
                inferred.append(candidate)
    if skipped:
        logger.info(
            "relationship inference skipped %d service(s) with more than %d event pairs",
            skipped,
            max_pairs,
        )
    return tuple(sorted(inferred, key=lambda relationship: relationship.key()))


def apply_inferred_relationships(catalog: EventCatalog) -> int:
    """Add inferred relationships to ``catalog`` in place; returns the count.

    ``infer_relationships`` already de-duplicates against the catalog, so the
    result is installed in one assignment instead of an insert per candidate.
    """
    inferred = infer_relationships(catalog)
    if not inferred:
        return 0
    catalog.set_relationships((*catalog.relationships, *inferred))
    return len(inferred)


def build_graph(
    catalog: EventCatalog,
    *,
    include_topics: bool = True,
    include_services: bool = True,
    edge_kinds: Iterable[DependencyKind] | None = None,
) -> Graph:
    """Build the dependency graph for ``catalog``.

    Nodes are services, events and topics; edges carry their semantic kind, so
    callers can query a single graph as several logical views.
    """
    allowed = None if edge_kinds is None else set(edge_kinds)
    nodes: list[Node] = []
    ids: dict[str, Node] = {}

    def add(node: Node) -> None:
        if node.id not in ids:
            ids[node.id] = node
            nodes.append(node)

    if include_services:
        for service in catalog.sorted_services():
            add(
                Node(
                    id=service_node_id(service.name),
                    kind=NodeKind.SERVICE,
                    label=service.name,
                    ref=service.name,
                    metadata={"tier": service.tier, "service_kind": service.kind.value},
                )
            )
    for event in catalog.sorted_events():
        add(
            Node(
                id=event_node_id(event.id),
                kind=NodeKind.EVENT,
                label=event.qualified_name,
                ref=event.id,
                metadata={
                    "version": event.version,
                    "namespace": event.namespace,
                    "status": event.status.value,
                },
            )
        )
    if include_topics:
        for topic in catalog.sorted_topics():
            add(
                Node(
                    id=topic_node_id(topic.name),
                    kind=NodeKind.TOPIC,
                    label=topic.name,
                    ref=topic.name,
                    metadata={"broker": topic.broker, "partitions": topic.partitions},
                )
            )

    # Sort once: node ids are the graph's canonical order, and edge endpoints
    # are expressed as positions in this list.
    nodes.sort(key=lambda node: node.id)
    position = {node.id: index for index, node in enumerate(nodes)}

    def node_index(node_id: str) -> int | None:
        return position.get(node_id)

    edges: list[Edge] = []
    for dependency in catalog.dependencies:
        if allowed is not None and dependency.kind not in allowed:
            continue
        source = node_index(dependency.source)
        target = node_index(dependency.target)
        if source is None or target is None:
            continue
        edges.append(
            Edge(
                source=source,
                target=target,
                kind=dependency.kind,
                inferred=dependency.inferred,
            )
        )

    for relationship in catalog.relationships:
        # ``correlates`` implies no ordering, so it has no place in a directed graph.
        if relationship.kind == RelationshipKind.CORRELATES:
            continue
        kind = (
            DependencyKind.TRIGGERS
            if relationship.kind == RelationshipKind.TRIGGERS
            else DependencyKind.DERIVED_FROM
        )
        if allowed is not None and kind not in allowed:
            continue
        source = node_index(event_node_id(relationship.source_event))
        target = node_index(event_node_id(relationship.target_event))
        if source is None or target is None:
            logger.debug(
                "skipping relationship with unresolved endpoint: %s -> %s",
                relationship.source_event,
                relationship.target_event,
            )
            continue
        edges.append(
            Edge(
                source=source,
                target=target,
                kind=kind,
                inferred=relationship.inferred,
                weight=relationship.confidence,
            )
        )

    return Graph.build(nodes, edges)


def service_flow_graph(graph: Graph) -> Graph:
    """Project the graph onto services only.

    Service A produces an event consumed by service B, so B depends on A and the
    edge points the way data flows, ``A -> B``: the projection collapses ``service
    -> event -> service`` paths, which is what you want for circular dependency
    detection between teams. A node's out-degree counts its dependents.

    The projection is computed with one pass over the nodes (not one pass per
    path) and memoized on the source graph, because it is dense - a 10k-event
    catalog easily projects to a few hundred thousand service pairs.
    """
    cached = graph.derived("service_flow")
    if cached is not None:
        return cached

    service_nodes = [node for node in graph.nodes if node.kind == NodeKind.SERVICE]
    position = {node.id: index for index, node in enumerate(service_nodes)}
    produced_hubs: dict[int, list[int]] = {}
    consumed_by_hub: dict[int, set[int]] = {}
    produce_kinds = (DependencyKind.PRODUCES, DependencyKind.PUBLISHES_TO)
    consume_kinds = (DependencyKind.CONSUMES, DependencyKind.SUBSCRIBES_TO)
    for node_index, node in enumerate(graph.nodes):
        if node.kind == NodeKind.SERVICE:
            source = position[node.id]
            hubs = [
                hub
                for hub in graph.successors(node_index, produce_kinds)
                if graph.node(hub).kind in {NodeKind.EVENT, NodeKind.TOPIC}
            ]
            if hubs:
                produced_hubs[source] = hubs
        elif node.kind in {NodeKind.EVENT, NodeKind.TOPIC}:
            consumers = {
                position[graph.node(target).id]
                for target in graph.successors(node_index, consume_kinds)
                if graph.node(target).kind == NodeKind.SERVICE
            }
            if consumers:
                consumed_by_hub[node_index] = consumers

    # Union the consumer sets in C (``set.__ior__``) instead of walking every
    # hub membership in Python: topic hubs can list hundreds of services, so the
    # naive triple loop visits millions of candidates to produce a few hundred
    # thousand pairs.
    pairs: set[tuple[int, int]] = set()
    empty: frozenset[int] = frozenset()
    for source, hubs in produced_hubs.items():
        targets: set[int] = set()
        for hub in hubs:
            targets |= consumed_by_hub.get(hub, empty)
        targets.discard(source)
        for target in targets:
            pairs.add((source, target))

    return graph.remember(
        "service_flow",
        Graph.build(
            service_nodes,
            [Edge(source, target, DependencyKind.DEPENDS_ON) for source, target in sorted(pairs)],
        ),
    )


def event_flow_graph(graph: Graph) -> Graph:
    """Subgraph of events connected by causal relationships."""
    return graph.filtered(DESCENDANT_FLOW_KINDS)


def node_ref_map(graph: Graph) -> Mapping[str, Node]:
    """Map node ids to nodes for quick lookups."""
    return {node.id: node for node in graph.nodes}


__all__ = [
    "apply_inferred_relationships",
    "build_graph",
    "event_flow_graph",
    "infer_relationships",
    "node_ref_map",
    "service_flow_graph",
]
