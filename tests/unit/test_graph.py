"""Graph engine: traversal, cycles, components and provenance."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.conftest import make_catalog, make_event, simple_catalog

from eventlineage.graph.algorithms import (
    bfs,
    connected_components,
    degree_centrality,
    dfs,
    downstream,
    find_cycle_groups,
    find_cycles,
    has_cycle,
    impact_propagation,
    largest_component,
    reachable,
    shortest_path,
    strongly_connected_components,
    topological_sort,
    upstream,
)
from eventlineage.graph.builder import apply_inferred_relationships, build_graph, service_flow_graph
from eventlineage.graph.model import Edge, Graph, Node


def node(name: str, kind: str = "event") -> Node:
    return Node(id=name, kind=kind, label=name, ref=name)


def chain_graph() -> Graph:
    """A -> b -> c -> d (event flow)."""
    nodes = [node(name) for name in ("a", "b", "c", "d")]
    edges = [
        Edge(source=0, target=1, kind="triggers"),
        Edge(source=1, target=2, kind="triggers"),
        Edge(source=2, target=3, kind="triggers"),
    ]
    return Graph.build(nodes, edges)


def diamond_graph() -> Graph:
    """A -> b, a -> c, b -> d, c -> d."""
    nodes = [node(name) for name in ("a", "b", "c", "d")]
    edges = [
        Edge(source=0, target=1, kind="triggers"),
        Edge(source=0, target=2, kind="triggers"),
        Edge(source=1, target=3, kind="triggers"),
        Edge(source=2, target=3, kind="triggers"),
    ]
    return Graph.build(nodes, edges)


class TestGraphModel:
    def test_nodes_are_sorted_by_id(self) -> None:
        graph = Graph.build(
            [node("z"), node("a")],
            [],
        )
        assert [node.id for node in graph.nodes] == ["a", "z"]

    def test_duplicate_node_ids_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate node id"):
            Graph.build([node("a"), node("a")], [])

    def test_out_of_range_edge_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown node index"):
            Graph.build([node("a")], [Edge(source=0, target=5, kind="triggers")])

    def test_duplicate_edges_are_collapsed(self) -> None:
        graph = Graph.build(
            [node("a"), node("b")],
            [
                Edge(source=0, target=1, kind="triggers"),
                Edge(source=0, target=1, kind="triggers"),
            ],
        )
        assert graph.edge_count == 1

    def test_build_is_order_independent(self) -> None:
        nodes = [node(name) for name in ("a", "b", "c")]
        forward = Graph.build(nodes, [Edge(source=0, target=1, kind="triggers")])
        backward = Graph.build(
            list(reversed(nodes)), [Edge(source=2, target=1, kind="triggers")]
        )  # index 2 is "a", index 1 is "b"

        def as_ids(graph: Graph) -> list[tuple[str, str]]:
            return [
                (graph.node(edge.source).id, graph.node(edge.target).id) for edge in graph.edges
            ]

        assert as_ids(forward) == as_ids(backward) == [("a", "b")]

    def test_index_lookups(self) -> None:
        graph = chain_graph()
        assert graph.index_of("b") == 1
        assert graph.contains("c")
        assert graph.node(0).id == "a"
        assert graph.node_by_id("d").label == "d"
        assert graph.degree_histogram()[1] >= 1

    def test_successors_and_predecessors_respect_kinds(self) -> None:
        graph = Graph.build(
            [
                node("a"),
                node("b"),
                node("c"),
            ],
            [
                Edge(source=0, target=1, kind="triggers"),
                Edge(source=0, target=2, kind="consumes"),
            ],
        )
        assert set(graph.successors(0)) == {1, 2}
        assert list(graph.successors(0, kinds=("triggers",))) == [1]
        assert set(graph.predecessors(1)) == {0}

    def test_filtered_keeps_requested_edges(self) -> None:
        graph = Graph.build(
            [node("a"), node("b")],
            [
                Edge(source=0, target=1, kind="triggers"),
                Edge(source=0, target=1, kind="consumes"),
            ],
        )
        assert graph.filtered(kinds=("triggers",)).edge_count == 1
        assert graph.edge_kind_counts()["triggers"] == 1

    def test_subgraph_remaps_edges(self) -> None:
        graph = chain_graph()
        subgraph = graph.subgraph({"b", "c", "d"})
        assert [item.id for item in subgraph.nodes] == ["b", "c", "d"]
        assert subgraph.edge_count == 2
        assert subgraph.node(next(iter(subgraph.successors(subgraph.index_of("b"))))).id == "c"

    def test_memory_bytes_is_reported(self) -> None:
        assert chain_graph().memory_bytes() > 0


class TestTraversal:
    def test_bfs_depths(self) -> None:
        result = bfs(chain_graph(), 0)
        assert tuple(result.order) == (0, 1, 2, 3)
        assert result.depths == {0: 0, 1: 1, 2: 2, 3: 3}

    def test_bfs_max_depth(self) -> None:
        result = bfs(chain_graph(), 0, max_depth=1)
        assert tuple(result.order) == (0, 1)

    def test_bfs_reverse(self) -> None:
        result = bfs(chain_graph(), 3, reverse=True)
        assert tuple(result.order) == (3, 2, 1, 0)

    def test_dfs_preorder_and_postorder(self) -> None:
        result = dfs(chain_graph())
        assert tuple(result.preorder) == (0, 1, 2, 3)
        assert tuple(result.postorder) == (3, 2, 1, 0)

    def test_reachable_and_downstream(self) -> None:
        graph = diamond_graph()
        assert reachable(graph, 0) == frozenset({1, 2, 3})
        assert set(downstream(graph, 0).order) == {0, 1, 2, 3}
        assert set(upstream(graph, 3).order) == {0, 1, 2, 3}

    def test_shortest_path(self) -> None:
        graph = chain_graph()
        assert tuple(shortest_path(graph, 0, 3)) == (0, 1, 2, 3)
        assert tuple(shortest_path(graph, 3, 0)) == ()

    def test_topological_sort_on_dag(self) -> None:
        order = topological_sort(chain_graph())
        assert order is not None
        assert list(order) == [0, 1, 2, 3]

    def test_topological_sort_returns_none_on_cycle(self) -> None:
        graph = Graph.build(
            [node("a"), node("b")],
            [
                Edge(source=0, target=1, kind="triggers"),
                Edge(source=1, target=0, kind="triggers"),
            ],
        )
        assert topological_sort(graph) is None

    def test_impact_propagation_decays_with_distance(self) -> None:
        weights = impact_propagation(chain_graph(), 0)
        assert weights[0] == pytest.approx(1.0)
        assert weights[1] > weights[2] > weights[3] > 0

    def test_degree_centrality(self) -> None:
        centrality = degree_centrality(chain_graph())
        assert max(centrality.values()) > min(centrality.values())


class TestConnectivity:
    def test_connected_components(self) -> None:
        graph = Graph.build(
            [node(name) for name in ("a", "b", "c")],
            [Edge(source=0, target=1, kind="triggers")],
        )
        components = connected_components(graph)
        assert sorted(len(component) for component in components) == [1, 2]
        assert list(largest_component(graph)) == [0, 1]

    def test_scc_groups_cycles(self) -> None:
        graph = Graph.build(
            [node(name) for name in ("a", "b", "c")],
            [
                Edge(source=0, target=1, kind="triggers"),
                Edge(source=1, target=2, kind="triggers"),
                Edge(source=2, target=0, kind="triggers"),
            ],
        )
        components = strongly_connected_components(graph, include_trivial=False)
        assert [tuple(component) for component in components] == [(0, 1, 2)]

    def test_isolated_nodes(self) -> None:
        graph = Graph.build([node("a"), node("b")], [])
        assert list(graph.isolated_nodes()) == [0, 1]


class TestCycles:
    def test_has_cycle(self) -> None:
        assert has_cycle(chain_graph()) is False

    def test_find_cycles_reports_one_cycle_once(self) -> None:
        graph = Graph.build(
            [node(name) for name in ("a", "b")],
            [
                Edge(source=0, target=1, kind="triggers"),
                Edge(source=1, target=0, kind="triggers"),
            ],
        )
        cycles = find_cycles(graph)
        assert len(cycles) == 1
        assert set(cycles[0]) == {0, 1}

    def test_find_cycle_groups_collapses_a_ring(self) -> None:
        # A five-node ring has ten elementary cycles but one structural problem.
        nodes = [node(f"n{index}") for index in range(5)]
        edges = [Edge(source=index, target=(index + 1) % 5, kind="triggers") for index in range(5)]
        graph = Graph.build(nodes, edges)
        groups = find_cycle_groups(graph)
        assert len(groups) == 1
        assert len(groups[0].nodes) == 5
        assert len(groups[0].example) >= 2

    def test_find_cycle_groups_on_acyclic_graph(self) -> None:
        assert find_cycle_groups(chain_graph()) == []


@given(st.integers(min_value=1, max_value=40))
def test_random_dag_is_topologically_sortable(size: int) -> None:
    nodes = [node(f"n{index}") for index in range(size)]
    edges = [
        Edge(source=source, target=target, kind="triggers")
        for source in range(size)
        for target in range(source + 1, size)
        if (source * 7 + target * 13) % 5 == 0
    ]
    graph = Graph.build(nodes, edges)
    order = topological_sort(graph)
    assert order is not None
    position = {node: index for index, node in enumerate(order)}
    for edge in graph.edges:
        assert position[edge.source] < position[edge.target]


class TestBuilder:
    def test_build_graph_nodes_and_edges(self) -> None:
        graph = build_graph(simple_catalog())
        assert graph.contains("event:order.created")
        assert graph.contains("service:payment-service")
        assert graph.contains("topic:orders.v1")
        assert graph.edge_count > 0
        assert graph.kind_counts()["event"] == 3

    def test_build_graph_can_exclude_topics(self) -> None:
        graph = build_graph(simple_catalog(), include_topics=False)
        assert not graph.contains("topic:orders.v1")

    def test_service_flow_graph_collapses_events(self) -> None:
        flow = service_flow_graph(build_graph(simple_catalog()))
        assert set(flow.kind_counts()) == {"service"}
        assert flow.node_count == 3

    def test_relationships_create_event_edges(self) -> None:
        from eventlineage.models.dependency import EventRelationship

        catalog = make_catalog(
            [
                make_event("a.b", producers=["x"], consumers=["y"]),
                make_event("c.d", producers=["y"], consumers=["z"]),
            ]
        )
        catalog.add_relationship(
            EventRelationship(source_event="a.b", target_event="c.d", kind="triggers")
        )
        graph = build_graph(catalog)
        pairs = {(graph.node(edge.source).id, graph.node(edge.target).id) for edge in graph.edges}
        assert ("event:a.b", "event:c.d") in pairs
        assert ("service:x", "event:a.b") in pairs

    def test_inferred_relationships_are_marked(self) -> None:
        catalog = make_catalog(
            [
                make_event("a.b", producers=["x"], consumers=["y"]),
                make_event("c.d", producers=["y"]),
            ]
        )
        added = apply_inferred_relationships(catalog)
        assert added >= 1
        assert any(relationship.inferred for relationship in catalog.relationships)

    def test_graph_is_deterministic(self) -> None:
        first = build_graph(simple_catalog()).to_dict()
        second = build_graph(simple_catalog()).to_dict()
        assert first == second
