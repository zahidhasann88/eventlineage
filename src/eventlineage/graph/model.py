"""An immutable, memory-efficient directed graph used by the analysis engine.

Why not NetworkX?

EventLineage needs exactly three things from a graph library: fast adjacency
iteration, cheap immutable snapshots, and predictable memory use for graphs
with 10k+ events and 100k+ edges. NetworkX stores arbitrary attribute dicts per
node/edge and pays a large constant factor for that flexibility. A purpose-built
compressed-sparse-row (CSR) representation keeps every traversal within a few
cache-friendly integer arrays, is hash-free and therefore deterministic, and
removes a dependency from the core package. See ``docs/performance.md`` for the
benchmarks that back this decision and ``benchmarks/bench_graph.py`` to
reproduce them.

The graph is immutable after :meth:`Graph.build`: all adjacency structures are
tuples/arrays, which makes graphs safe to share between threads and cheap to
copy (no copy is needed at all).
"""

from __future__ import annotations

from array import array
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from eventlineage.models.common import DependencyKind, NodeKind


@dataclass(frozen=True, slots=True, eq=False)
class Node:
    """A vertex in the dependency graph."""

    id: str
    kind: NodeKind
    label: str
    ref: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Accept plain strings so callers never need the enum to build a graph;
        # the annotation is the enum because that is what callers then read.
        if not isinstance(self.kind, NodeKind):
            object.__setattr__(self, "kind", NodeKind(str(self.kind)))  # type: ignore[unreachable]

    def __str__(self) -> str:
        return self.label or self.ref or self.id

    @property
    def node_key(self) -> str:
        """Canonical ``kind:ref`` identifier."""
        return f"{self.kind.value}:{self.ref}"


@dataclass(frozen=True, slots=True)
class Edge:
    """A typed, directed edge. Endpoints are node indices, not ids."""

    source: int
    target: int
    kind: DependencyKind
    inferred: bool = False
    weight: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.kind, DependencyKind):
            object.__setattr__(self, "kind", DependencyKind(str(self.kind)))  # type: ignore[unreachable]


def kind_values(kinds: Iterable[DependencyKind | str] | None) -> set[str] | None:
    """Normalize an edge-kind filter into a set of string values.

    Accepting plain strings keeps the public API friendly (``kinds=["triggers"]``)
    without forcing callers to import the enum.
    """
    if kinds is None:
        return None
    values: set[str] = set()
    for kind in kinds:
        values.add(kind.value if isinstance(kind, DependencyKind) else str(kind))
    return values


class Graph:
    """Immutable directed graph with CSR adjacency.

    Node indices are dense (``0..n-1``) and ordered by node id, so iteration
    order is deterministic regardless of insertion order.
    """

    __slots__ = (
        "_derived",
        "_edges",
        "_in_edge_ids",
        "_in_offsets",
        "_in_targets",
        "_index",
        "_nodes",
        "_out_edge_ids",
        "_out_offsets",
        "_out_targets",
    )

    def __init__(
        self,
        nodes: tuple[Node, ...],
        index: Mapping[str, int],
        edges: tuple[Edge, ...],
        out_offsets: tuple[int, ...],
        out_targets: array[int],
        out_edge_ids: array[int],
        in_offsets: tuple[int, ...],
        in_targets: array[int],
        in_edge_ids: array[int],
    ) -> None:
        self._nodes = nodes
        self._index = dict(index)
        self._edges = edges
        self._out_offsets = out_offsets
        self._out_targets = out_targets
        self._out_edge_ids = out_edge_ids
        self._in_offsets = in_offsets
        self._in_targets = in_targets
        self._in_edge_ids = in_edge_ids
        # Derived graphs (service projection, event flow) are memoized on the
        # instance. A graph is immutable, so a cached projection can never go
        # stale; it just saves rebuilding the same 100k-edge graph twice when a
        # report runs analysis and validation in one process.
        self._derived: dict[str, Graph] = {}

    def __repr__(self) -> str:
        return f"Graph(nodes={self.node_count}, edges={self.edge_count})"

    def __len__(self) -> int:
        return len(self._nodes)

    @classmethod
    def build(cls, nodes: Iterable[Node], edges: Iterable[Edge]) -> Graph:
        """Freeze nodes/edges into CSR arrays.

        Nodes are sorted by id and edges by ``(source, target, kind)`` so that
        identical input always yields a byte-identical graph (and identical
        reports). Edge endpoints refer to positions in the *input* node
        sequence; they are remapped to the sorted order internally.
        """
        incoming_nodes = list(nodes)
        order = sorted(range(len(incoming_nodes)), key=lambda index: incoming_nodes[index].id)
        node_list = [incoming_nodes[index] for index in order]
        remap = {old: new for new, old in enumerate(order)}
        seen: dict[str, int] = {}
        for position, node in enumerate(node_list):
            if node.id in seen:
                raise ValueError(f"duplicate node id in graph: {node.id!r}")
            seen[node.id] = position

        # Deduplicate on plain ``(source, target, kind)`` tuples and sort those
        # tuples directly: sorting keys in C beats a Python ``key=`` callable on
        # graphs with 10k+ nodes and 100k+ edges.
        unique: dict[tuple[int, int, str], Edge] = {}
        for edge in edges:
            source = edge.source
            target = edge.target
            if source not in remap or target not in remap:
                raise ValueError(f"edge references unknown node index: {edge}")
            new_source = remap[source]
            new_target = remap[target]
            remapped = edge
            if new_source != source or new_target != target:
                remapped = Edge(
                    source=new_source,
                    target=new_target,
                    kind=edge.kind,
                    inferred=edge.inferred,
                    weight=edge.weight,
                )
            unique.setdefault((remapped.source, remapped.target, remapped.kind.value), remapped)
        edge_list: list[Edge] = [unique[key] for key in sorted(unique)]

        n = len(node_list)
        out_counts = [0] * n
        in_counts = [0] * n
        for edge in edge_list:
            out_counts[edge.source] += 1
            in_counts[edge.target] += 1

        out_offsets = [0] * (n + 1)
        in_offsets = [0] * (n + 1)
        for i in range(n):
            out_offsets[i + 1] = out_offsets[i] + out_counts[i]
            in_offsets[i + 1] = in_offsets[i] + in_counts[i]

        out_targets = array("i", [0]) * len(edge_list)
        out_edge_ids = array("i", [0]) * len(edge_list)
        in_targets = array("i", [0]) * len(edge_list)
        in_edge_ids = array("i", [0]) * len(edge_list)
        out_cursor = list(out_offsets[:-1])
        in_cursor = list(in_offsets[:-1])
        for edge_id, edge in enumerate(edge_list):
            slot = out_cursor[edge.source]
            out_targets[slot] = edge.target
            out_edge_ids[slot] = edge_id
            out_cursor[edge.source] += 1
            slot = in_cursor[edge.target]
            in_targets[slot] = edge.source
            in_edge_ids[slot] = edge_id
            in_cursor[edge.target] += 1

        return cls(
            tuple(node_list),
            seen,
            tuple(edge_list),
            tuple(out_offsets),
            out_targets,
            out_edge_ids,
            tuple(in_offsets),
            in_targets,
            in_edge_ids,
        )

    @classmethod
    def empty(cls) -> Graph:
        """An empty graph."""
        return cls.build((), ())

    @property
    def nodes(self) -> tuple[Node, ...]:
        """Nodes in deterministic id order."""
        return self._nodes

    @property
    def edges(self) -> tuple[Edge, ...]:
        """Deduplicated edges in deterministic order."""
        return self._edges

    @property
    def node_count(self) -> int:
        """Number of nodes."""
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        """Number of edges."""
        return len(self._edges)

    def index_of(self, node_id: str) -> int | None:
        """Dense index of ``node_id``, or ``None`` when absent."""
        return self._index.get(node_id)

    def node(self, node_index: int) -> Node:
        """Node at a dense index."""
        return self._nodes[node_index]

    def node_by_id(self, node_id: str) -> Node | None:
        """Node for ``node_id``, or ``None`` when absent."""
        position = self._index.get(node_id)
        return None if position is None else self._nodes[position]

    def require(self, node_id: str) -> int:
        """Index of ``node_id``, raising when it is missing."""
        position = self._index.get(node_id)
        if position is None:
            raise KeyError(node_id)
        return position

    def contains(self, node_id: str) -> bool:
        """Whether the graph has a node with this id."""
        return node_id in self._index

    def edge(self, edge_id: int) -> Edge:
        """Edge at a dense edge index."""
        return self._edges[edge_id]

    def edges_of_kind(self, kinds: Iterable[DependencyKind | str]) -> Iterator[Edge]:
        """Edges of the given kinds, in deterministic order."""
        allowed = kind_values(kinds) or set()
        for edge in self._edges:
            if edge.kind.value in allowed:
                yield edge

    def out_edge_ids(self, node_index: int) -> range:
        """Edge ids leaving a node."""
        return range(self._out_offsets[node_index], self._out_offsets[node_index + 1])

    def in_edge_ids(self, node_index: int) -> range:
        """Edge ids entering a node."""
        return range(self._in_offsets[node_index], self._in_offsets[node_index + 1])

    def out_degree(self, node_index: int) -> int:
        """Number of outgoing edges."""
        return self._out_offsets[node_index + 1] - self._out_offsets[node_index]

    def in_degree(self, node_index: int) -> int:
        """Number of incoming edges."""
        return self._in_offsets[node_index + 1] - self._in_offsets[node_index]

    def successors(
        self, node_index: int, kinds: Iterable[DependencyKind] | None = None
    ) -> Iterator[int]:
        """Yield successor indices, optionally filtered by edge kind."""
        start, stop = self._out_offsets[node_index], self._out_offsets[node_index + 1]
        if kinds is None:
            for slot in range(start, stop):
                yield self._out_targets[slot]
            return
        allowed = kind_values(kinds) or set()
        for slot in range(start, stop):
            if self._edges[self._out_edge_ids[slot]].kind.value in allowed:
                yield self._out_targets[slot]

    def predecessors(
        self, node_index: int, kinds: Iterable[DependencyKind] | None = None
    ) -> Iterator[int]:
        """Yield predecessor indices, optionally filtered by edge kind."""
        start, stop = self._in_offsets[node_index], self._in_offsets[node_index + 1]
        if kinds is None:
            for slot in range(start, stop):
                yield self._in_targets[slot]
            return
        allowed = kind_values(kinds) or set()
        for slot in range(start, stop):
            if self._edges[self._in_edge_ids[slot]].kind.value in allowed:
                yield self._in_targets[slot]

    def out_edges(self, node_index: int) -> Iterator[Edge]:
        """Outgoing edges of a node."""
        start, stop = self._out_offsets[node_index], self._out_offsets[node_index + 1]
        for slot in range(start, stop):
            yield self._edges[self._out_edge_ids[slot]]

    def in_edges(self, node_index: int) -> Iterator[Edge]:
        """Incoming edges of a node."""
        start, stop = self._in_offsets[node_index], self._in_offsets[node_index + 1]
        for slot in range(start, stop):
            yield self._edges[self._in_edge_ids[slot]]

    def derived(self, name: str) -> Graph | None:
        """Return a memoized projection built from this graph, if any."""
        return self._derived.get(name)

    def remember(self, name: str, graph: Graph) -> Graph:
        """Memoize ``graph`` under ``name`` and return it."""
        self._derived[name] = graph
        return graph

    def forget(self, name: str) -> None:
        """Drop a memoized projection so the next call recomputes it.

        Only useful for benchmarks, which must time the cold path, and for
        long-lived processes that want to release memory.
        """
        self._derived.pop(name, None)

    def filtered(self, kinds: Iterable[DependencyKind | str]) -> Graph:
        """Subgraph containing only edges of the given kinds."""
        allowed = kind_values(kinds) or set()
        if not allowed:
            return Graph.build(self._nodes, ())
        # Node indices are unchanged, so the surviving edges can be reused as-is.
        return Graph.build(
            self._nodes,
            (edge for edge in self._edges if edge.kind.value in allowed),
        )

    def subgraph(self, node_ids: Iterable[str]) -> Graph:
        """Induced subgraph on ``node_ids`` (unknown ids are ignored)."""
        keep = self._nodes_of(node_ids)
        remap = {old: new for new, old in enumerate(keep)}
        nodes = [self._nodes[index] for index in keep]
        edges = [
            Edge(remap[e.source], remap[e.target], e.kind, e.inferred, e.weight)
            for e in self._edges
            if e.source in remap and e.target in remap
        ]
        return Graph.build(nodes, edges)

    def _nodes_of(self, node_ids: Iterable[str]) -> list[int]:
        """Indices of the known ``node_ids``, in node-id order."""
        keep = {node_id for node_id in node_ids if node_id in self._index}
        return sorted(self._index[node_id] for node_id in keep)

    def project(
        self,
        node_ids: Mapping[str, str],
        kinds: Iterable[DependencyKind | str] | None = None,
    ) -> Graph:
        """Collapse the graph onto a smaller node set.

        ``node_ids`` maps original node ids to projected node ids. Edges whose
        endpoints land on the same projected node are dropped (they would be
        self-loops after collapsing). This is how the service-level flow graph
        is derived from the event graph.
        """
        allowed = kind_values(kinds)
        projected_nodes: dict[str, Node] = {}
        edges: list[Edge] = []
        edge_keys: set[tuple[int, int, str]] = set()
        bucket: list[int] = []
        order: dict[str, int] = {}
        pending: list[tuple[str, str, Edge]] = []

        for node in self._nodes:
            mapped = node_ids.get(node.id)
            if mapped is None:
                continue
            if mapped not in order:
                order[mapped] = len(order)
                projected_nodes[mapped] = Node(
                    id=mapped,
                    kind=node.kind,
                    label=mapped.split(":", 1)[-1],
                    ref=mapped.split(":", 1)[-1],
                )
            bucket.append(order[mapped])

        for edge in self._edges:
            if allowed is not None and edge.kind.value not in allowed:
                continue
            source_id = node_ids.get(self._nodes[edge.source].id)
            target_id = node_ids.get(self._nodes[edge.target].id)
            if source_id is None or target_id is None or source_id == target_id:
                continue
            key = (order[source_id], order[target_id], edge.kind.value)
            if key in edge_keys:
                continue
            edge_keys.add(key)
            pending.append((source_id, target_id, edge))

        projected = [node for _, node in sorted(projected_nodes.items(), key=lambda kv: kv[0])]
        index = {node.id: position for position, node in enumerate(projected)}
        for source_id, target_id, edge in pending:
            edges.append(
                Edge(
                    index[source_id],
                    index[target_id],
                    edge.kind,
                    edge.inferred,
                    edge.weight,
                )
            )
        return Graph.build(projected, edges)

    def degree_histogram(self) -> dict[int, int]:
        """Distribution of total degree across nodes."""
        counts: dict[int, int] = {}
        for node_index in range(self.node_count):
            degree = self.out_degree(node_index) + self.in_degree(node_index)
            counts[degree] = counts.get(degree, 0) + 1
        return dict(sorted(counts.items()))

    def kind_counts(self) -> dict[str, int]:
        """Node counts grouped by node kind."""
        counts: dict[str, int] = {}
        for node in self._nodes:
            counts[node.kind.value] = counts.get(node.kind.value, 0) + 1
        return dict(sorted(counts.items()))

    def edge_kind_counts(self) -> dict[str, int]:
        """Edge counts grouped by dependency kind."""
        counts: dict[str, int] = {}
        for edge in self._edges:
            counts[edge.kind.value] = counts.get(edge.kind.value, 0) + 1
        return dict(sorted(counts.items()))

    def isolated_nodes(self) -> list[int]:
        """Nodes with neither incoming nor outgoing edges."""
        return [
            index
            for index in range(self.node_count)
            if self.in_degree(index) == 0 and self.out_degree(index) == 0
        ]

    def memory_bytes(self) -> int:
        """Approximate resident size of the graph structure (bytes)."""
        adjacency = sum(
            len(entries) * entries.itemsize
            for entries in (
                self._out_targets,
                self._in_targets,
                self._out_edge_ids,
                self._in_edge_ids,
            )
        )
        offsets = (len(self._out_offsets) + len(self._in_offsets)) * 8
        edges = len(self._edges) * 64
        nodes = len(self._nodes) * 120
        return adjacency + offsets + edges + nodes

    def to_dict(self) -> dict[str, Any]:
        """Deterministic dictionary form (used by the ``export`` command)."""
        return {
            "nodes": [
                {
                    "id": node.id,
                    "kind": node.kind.value,
                    "label": node.label,
                    "ref": node.ref,
                }
                for node in self._nodes
            ],
            "edges": [
                {
                    "source": self._nodes[edge.source].id,
                    "target": self._nodes[edge.target].id,
                    "kind": edge.kind.value,
                    "inferred": edge.inferred,
                }
                for edge in self._edges
            ],
            "stats": {
                "nodes": self.node_count,
                "edges": self.edge_count,
                "node_kinds": self.kind_counts(),
                "edge_kinds": self.edge_kind_counts(),
            },
        }


def service_node_id(service: str) -> str:
    """Canonical node id for a service reference."""
    return f"{NodeKind.SERVICE.value}:{service}"


def event_node_id(event_id: str) -> str:
    """Canonical node id for an event reference."""
    return f"{NodeKind.EVENT.value}:{event_id}"


def topic_node_id(topic: str) -> str:
    """Canonical node id for a topic reference."""
    return f"{NodeKind.TOPIC.value}:{topic}"


__all__ = [
    "DependencyKind",
    "Edge",
    "Graph",
    "Node",
    "NodeKind",
    "event_node_id",
    "kind_values",
    "service_node_id",
    "topic_node_id",
]
