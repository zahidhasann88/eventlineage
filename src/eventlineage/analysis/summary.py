"""Architecture-level analysis: health, hotspots, cycles and recommendations."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from eventlineage.config import Config
from eventlineage.core.catalog import CatalogStats, EventCatalog
from eventlineage.graph.algorithms import (
    bfs,
    connected_components,
    find_cycle_groups,
    topological_sort,
)
from eventlineage.graph.builder import build_graph, service_flow_graph
from eventlineage.graph.model import Graph
from eventlineage.models.base import EGBaseModel
from eventlineage.models.common import DependencyKind


class GraphStats(EGBaseModel):
    """Size and shape of the dependency graph."""

    nodes: int = 0
    edges: int = 0
    node_kinds: dict[str, int] = Field(default_factory=dict)
    edge_kinds: dict[str, int] = Field(default_factory=dict)
    components: int = 0
    largest_component: int = 0
    isolated_nodes: int = 0
    memory_bytes: int = 0
    acyclic: bool = True


class Cycle(EGBaseModel):
    """A dependency cycle, at event or service level."""

    kind: str
    nodes: tuple[str, ...]

    def render(self) -> str:
        """Render this record as one line of text."""
        if not self.nodes:
            return ""
        return " -> ".join((*self.nodes, self.nodes[0]))


class Hotspot(EGBaseModel):
    """A highly connected node."""

    name: str
    kind: str
    score: float
    value: int
    description: str | None = None


class ArchitectureSummary(EGBaseModel):
    """The output of ``eventlineage analyze``."""

    project: str = "eventlineage"
    stats: CatalogStats = Field(default_factory=CatalogStats)
    graph: GraphStats = Field(default_factory=GraphStats)
    health_score: float = 100.0
    health_grade: str = "A"
    cycles: tuple[Cycle, ...] = ()
    hotspots: tuple[Hotspot, ...] = ()
    longest_chains: tuple[tuple[str, ...], ...] = ()
    schema_formats: dict[str, int] = Field(default_factory=dict)
    recommendations: tuple[str, ...] = ()
    denormalised: dict[str, Any] = Field(default_factory=dict)

    def summary(self) -> str:
        """Return a one-line human summary."""
        return (
            f"{self.project}: {self.stats.events} events, {self.stats.services} services, "
            f"{self.stats.topics} topics — health {self.health_score:.0f}/100 ({self.health_grade})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready representation."""
        return {
            "project": self.project,
            "stats": self.stats.model_dump(mode="json"),
            "graph": self.graph.model_dump(mode="json"),
            "health_score": round(self.health_score, 1),
            "health_grade": self.health_grade,
            "cycles": [
                {"kind": cycle.kind, "nodes": list(cycle.nodes), "render": cycle.render()}
                for cycle in self.cycles
            ],
            "hotspots": [hotspot.model_dump(mode="json") for hotspot in self.hotspots],
            "longest_chains": [list(chain) for chain in self.longest_chains],
            "schema_formats": self.schema_formats,
            "recommendations": list(self.recommendations),
        }


def _health(stats: CatalogStats, cycles: tuple[Cycle, ...]) -> tuple[float, str]:
    score = 100.0
    score -= min(stats.events_without_producer * 3.0, 20.0)
    score -= min(stats.events_without_consumer * 1.0, 10.0)
    score -= min(stats.events_without_schema * 2.0, 15.0)
    score -= min(len(cycles) * 8.0, 24.0)
    score -= min(stats.orphan_topics * 1.0, 5.0)
    score -= min(stats.services_without_events * 0.5, 5.0)
    score = max(0.0, round(score, 1))
    grade = (
        "A"
        if score >= 90
        else "B"
        if score >= 80
        else "C"
        if score >= 70
        else "D"
        if score >= 55
        else "F"
    )
    return score, grade


def _longest_chains(
    graph: Graph, limit: int = 3, max_depth: int = 12
) -> tuple[tuple[str, ...], ...]:
    """Deepest causal chains in the event flow graph.

    Roots are events with no incoming flow edge; for cyclic or fully-connected
    graphs the highest fan-out nodes are used instead so the function always
    returns something useful without unbounded work.
    """
    flow = graph.filtered((DependencyKind.TRIGGERS, DependencyKind.DERIVED_FROM))
    if flow.node_count == 0:
        return ()
    # The filtered graph keeps every node, so isolated ones must not use up the cap.
    linked = [index for index in range(flow.node_count) if flow.out_degree(index)]
    roots = [index for index in linked if flow.in_degree(index) == 0]
    if not roots:
        roots = sorted(linked, key=lambda index: -flow.out_degree(index))[:10]
    chains: list[tuple[str, ...]] = []
    for root in roots[:200]:
        traversal = bfs(flow, root, max_depth=max_depth)
        deepest = max(traversal.depths, key=lambda index: (traversal.depths[index], -index))
        if traversal.depths[deepest] < 2:
            continue
        path = traversal.path_to(flow, deepest)
        chains.append(tuple(flow.node(index).ref for index in path))
    chains.sort(key=lambda chain: (-len(chain), chain))
    return tuple(chains[:limit])


def _cycles(graph: Graph, service_flow: Graph | None = None) -> tuple[Cycle, ...]:
    """Cycles collapsed per strongly connected component.

    One structural problem should produce one line in a report: a ring of five
    services contains thousands of elementary cycles but is a single cycle to
    fix.
    """
    cycles: list[Cycle] = []
    flow = graph.filtered((DependencyKind.TRIGGERS, DependencyKind.DERIVED_FROM))
    for group in find_cycle_groups(flow, max_groups=25):
        nodes = group.example or group.nodes
        cycles.append(Cycle(kind="event", nodes=tuple(flow.node(index).ref for index in nodes)))
    services = service_flow or service_flow_graph(graph)
    for group in find_cycle_groups(services, max_groups=25):
        nodes = group.example or group.nodes
        cycles.append(
            Cycle(kind="service", nodes=tuple(services.node(index).ref for index in nodes))
        )
    return tuple(cycles)


def _hotspots(
    catalog: EventCatalog, graph: Graph, service_flow: Graph | None = None
) -> tuple[Hotspot, ...]:
    hotspots: list[Hotspot] = []
    services = service_flow or service_flow_graph(graph)
    for index in range(services.node_count):
        name = services.node(index).ref
        dependents = services.out_degree(index)
        if dependents <= 1:
            continue
        hotspots.append(
            Hotspot(
                name=name,
                kind="service",
                score=round(dependents / max(services.node_count - 1, 1), 3),
                value=dependents,
                description=f"{dependents} downstream services depend on it",
            )
        )
    for event in catalog.sorted_events():
        consumers = len(event.consumer_services)
        if consumers <= 1:
            continue
        hotspots.append(
            Hotspot(
                name=event.qualified_name,
                kind="event",
                score=round(consumers / 10.0, 3),
                value=consumers,
                description=f"consumed by {consumers} services",
            )
        )
    hotspots.sort(key=lambda hotspot: (-hotspot.value, hotspot.kind, hotspot.name))
    return tuple(hotspots[:10])


def _recommendations(
    stats: CatalogStats, cycles: tuple[Cycle, ...], hotspots: tuple[Hotspot, ...]
) -> tuple[str, ...]:
    notes: list[str] = []
    if any(cycle.kind == "event" for cycle in cycles):
        notes.append(
            "Break event-level cycles first: they make ordering guarantees and replay unsafe."
        )
    if stats.events_without_producer:
        notes.append(
            f"{stats.events_without_producer} event(s) have no known producer; document the "
            "emitting system so consumers are not relying on tribal knowledge."
        )
    if stats.events_without_schema:
        notes.append(
            f"{stats.events_without_schema} event(s) have no schema; without one, breaking "
            "changes cannot be detected."
        )
    if stats.events_without_consumer:
        notes.append(
            f"{stats.events_without_consumer} event(s) have no known consumer; delete dead "
            "contracts or record their external consumers."
        )
    if hotspots:
        top = hotspots[0]
        notes.append(
            f"{top.name} is the highest-impact node ({top.description}); changes there need "
            "a staged rollout."
        )
    if not notes:
        notes.append("No structural issues detected; keep contracts versioned as the system grows.")
    return tuple(notes)


def analyze_catalog(
    catalog: EventCatalog,
    config: Config | None = None,
    *,
    graph: Graph | None = None,
) -> ArchitectureSummary:
    """Build an :class:`ArchitectureSummary` for a catalog."""
    config = config or Config()
    graph = graph or build_graph(catalog, include_topics=config.analysis.include_topics)
    components = connected_components(graph)
    stats = catalog.stats()
    service_flow = service_flow_graph(graph)
    cycles = _cycles(graph, service_flow)
    graph_stats = GraphStats(
        nodes=graph.node_count,
        edges=graph.edge_count,
        node_kinds=graph.kind_counts(),
        edge_kinds=graph.edge_kind_counts(),
        components=len(components),
        largest_component=len(components[0]) if components else 0,
        isolated_nodes=len(graph.isolated_nodes()),
        memory_bytes=graph.memory_bytes(),
        acyclic=topological_sort(graph) is not None,
    )
    hotspots = _hotspots(catalog, graph, service_flow)
    score, grade = _health(stats, cycles)
    return ArchitectureSummary(
        project=catalog.project_name,
        stats=stats,
        graph=graph_stats,
        health_score=score,
        health_grade=grade,
        cycles=cycles,
        hotspots=hotspots,
        longest_chains=_longest_chains(graph),
        schema_formats=dict(stats.schema_formats),
        recommendations=_recommendations(stats, cycles, hotspots),
        denormalised={
            "event_kinds": {
                "orphaned": stats.events_without_producer,
                "unconsumed": stats.events_without_consumer,
                "isolated": stats.isolated_events,
            }
        },
    )


__all__ = [
    "ArchitectureSummary",
    "Cycle",
    "GraphStats",
    "Hotspot",
    "analyze_catalog",
]
