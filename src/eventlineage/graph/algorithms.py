"""Graph algorithms used by the analysis and validation engines.

All functions are iterative (no recursion) so that pathological graphs — for
instance a 10 000-node linear chain — cannot blow the Python stack. Traversals
are O(V + E) and allocate one integer array of size ``V`` for bookkeeping.

Every algorithm accepts an optional ``kinds`` filter so a single graph can be
queried as different logical views (e.g. "event flow edges only") without
rebuilding it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from eventlineage.graph.model import Graph, kind_values
from eventlineage.models.common import DependencyKind

EdgeKinds = Iterable[DependencyKind] | None


@dataclass(frozen=True, slots=True)
class TraversalResult:
    """Result of a BFS/DFS traversal."""

    root: int
    order: tuple[int, ...]
    depths: Mapping[int, int]
    parents: Mapping[int, int] = field(default_factory=dict)

    def nodes(self) -> tuple[int, ...]:
        """Nodes reachable in this component."""
        return self.order

    def depth_of(self, node_index: int) -> int | None:
        """Depth at which this node was reached."""
        return self.depths.get(node_index)

    def max_depth(self) -> int:
        """Deepest level reached by the traversal."""
        return max(self.depths.values(), default=0)

    def path_to(self, graph: Graph, node_index: int) -> tuple[int, ...]:
        """Shortest path from the traversal root to ``node_index``."""
        if node_index == self.root:
            return (self.root,)
        if node_index not in self.depths:
            return ()
        path = [node_index]
        current = node_index
        guard = 0
        limit = len(self.depths) + 1
        while current != self.root and guard <= limit:
            parent = self.parents.get(current)
            if parent is None:
                return ()
            path.append(parent)
            current = parent
            guard += 1
        path.reverse()
        return tuple(path)


@dataclass(frozen=True, slots=True)
class DFSResult:
    """Iterative DFS over a single root or the whole graph (forest)."""

    preorder: tuple[int, ...]
    postorder: tuple[int, ...]
    parents: Mapping[int, int]
    roots: tuple[int, ...]


def _iterate(graph: Graph, node: int, reverse: bool, kinds: EdgeKinds) -> Iterable[int]:
    return graph.predecessors(node, kinds) if reverse else graph.successors(node, kinds)


def bfs(
    graph: Graph,
    start: int,
    *,
    reverse: bool = False,
    max_depth: int | None = None,
    kinds: EdgeKinds = None,
    stop_at: frozenset[int] | None = None,
) -> TraversalResult:
    """Breadth-first traversal from ``start``.

    ``reverse=True`` walks edges backwards (upstream analysis). ``stop_at``
    prunes entire subtrees once those nodes are reached (their own children are
    not expanded) which is how impact analysis avoids re-walking shared
    dependencies.
    """
    if not 0 <= start < graph.node_count:
        raise IndexError(f"node index out of range: {start}")
    depths: dict[int, int] = {start: 0}
    parents: dict[int, int] = {}
    order: list[int] = [start]
    queue: deque[int] = deque([start])
    if max_depth is not None and max_depth < 0:
        return TraversalResult(root=start, order=(start,), depths=depths)
    while queue:
        node = queue.popleft()
        depth = depths[node] + 1
        if max_depth is not None and depth > max_depth:
            continue
        if stop_at is not None and node in stop_at and node != start:
            continue
        for neighbour in _iterate(graph, node, reverse, kinds):
            if neighbour in depths:
                continue
            depths[neighbour] = depth
            parents[neighbour] = node
            order.append(neighbour)
            queue.append(neighbour)
    return TraversalResult(root=start, order=tuple(order), depths=depths, parents=parents)


def dfs(
    graph: Graph,
    start: int | None = None,
    *,
    reverse: bool = False,
    kinds: EdgeKinds = None,
) -> DFSResult:
    """Depth-first traversal producing preorder/postorder and a spanning forest.

    When ``start`` is ``None`` every node is visited (in index order), yielding
    a deterministic forest which is what connected-component walkers need.
    """
    nodes = range(graph.node_count) if start is None else (start,)
    seen: set[int] = set()
    preorder: list[int] = []
    postorder: list[int] = []
    parents: dict[int, int] = {}
    roots: list[int] = []
    for root in nodes:
        if root in seen:
            continue
        roots.append(root)
        seen.add(root)
        stack: list[tuple[int, Iterable[int]]] = [
            (root, iter(_iterate(graph, root, reverse, kinds)))
        ]
        preorder.append(root)
        while stack:
            node, iterator = stack[-1]
            advanced = False
            for neighbour in iterator:
                if neighbour in seen:
                    continue
                seen.add(neighbour)
                parents[neighbour] = node
                preorder.append(neighbour)
                stack.append((neighbour, iter(_iterate(graph, neighbour, reverse, kinds))))
                advanced = True
                break
            if not advanced:
                stack.pop()
                postorder.append(node)
    return DFSResult(
        preorder=tuple(preorder),
        postorder=tuple(postorder),
        parents=parents,
        roots=tuple(roots),
    )


def reachable(
    graph: Graph,
    start: int,
    *,
    reverse: bool = False,
    max_depth: int | None = None,
    kinds: EdgeKinds = None,
) -> frozenset[int]:
    """Set of nodes reachable from ``start`` (excluding ``start`` itself)."""
    result = bfs(graph, start, reverse=reverse, max_depth=max_depth, kinds=kinds)
    return frozenset(result.depths) - {start}


def shortest_path(
    graph: Graph, source: int, target: int, *, kinds: EdgeKinds = None
) -> tuple[int, ...]:
    """Unweighted shortest path (BFS), or an empty tuple when unreachable."""
    if source == target:
        return (source,)
    traversal = bfs(graph, source, kinds=kinds, stop_at=None)
    return traversal.path_to(graph, target)


def has_cycle(graph: Graph, *, kinds: EdgeKinds = None) -> bool:
    """Whether the (optionally filtered) graph contains a directed cycle.

    Uses Kahn's algorithm: O(V + E), no recursion, and it doubles as a cheap
    pre-check before enumerating cycles.
    """
    allowed = kind_values(kinds)
    indegree = [0] * graph.node_count
    for edge in graph.edges:
        if allowed is None or edge.kind.value in allowed:
            indegree[edge.target] += 1
    queue = deque(index for index, degree in enumerate(indegree) if degree == 0)
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for neighbour in graph.successors(node, kinds):
            indegree[neighbour] -= 1
            if indegree[neighbour] == 0:
                queue.append(neighbour)
    return visited != graph.node_count


def topological_sort(graph: Graph, *, kinds: EdgeKinds = None) -> tuple[int, ...] | None:
    """Kahn topological order, or ``None`` when the graph is cyclic."""
    allowed = kind_values(kinds)
    indegree = [0] * graph.node_count
    for edge in graph.edges:
        if allowed is None or edge.kind.value in allowed:
            indegree[edge.target] += 1
    queue = deque(sorted(index for index, degree in enumerate(indegree) if degree == 0))
    order: list[int] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for neighbour in sorted(graph.successors(node, kinds)):
            indegree[neighbour] -= 1
            if indegree[neighbour] == 0:
                queue.append(neighbour)
    return tuple(order) if len(order) == graph.node_count else None


def strongly_connected_components(
    graph: Graph, *, kinds: EdgeKinds = None, include_trivial: bool = True
) -> list[tuple[int, ...]]:
    """Tarjan's SCC algorithm, iterative.

    Components are returned sorted by their smallest node index, each component
    internally sorted, giving deterministic output for reports and tests.
    """
    index_counter = 0
    indices: dict[int, int] = {}
    lowlink: dict[int, int] = {}
    on_stack: set[int] = set()
    stack: list[int] = []
    components: list[tuple[int, ...]] = []

    for root in range(graph.node_count):
        if root in indices:
            continue
        work: list[tuple[int, Iterable[int]]] = [(root, iter(graph.successors(root, kinds)))]
        indices[root] = lowlink[root] = index_counter
        index_counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, iterator = work[-1]
            advanced = False
            for neighbour in iterator:
                if neighbour not in indices:
                    indices[neighbour] = lowlink[neighbour] = index_counter
                    index_counter += 1
                    stack.append(neighbour)
                    on_stack.add(neighbour)
                    work.append((neighbour, iter(graph.successors(neighbour, kinds))))
                    advanced = True
                    break
                if neighbour in on_stack:
                    lowlink[node] = min(lowlink[node], indices[neighbour])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == indices[node]:
                component: list[int] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                component.sort()
                if len(component) > 1 or include_trivial:
                    if len(component) == 1 and not include_trivial:
                        continue
                    components.append(tuple(component))
    components.sort(key=lambda component: component[0])
    return components


def _self_loop_nodes(graph: Graph, kinds: EdgeKinds) -> list[int]:
    return [
        index
        for index in range(graph.node_count)
        if any(target == index for target in graph.successors(index, kinds))
    ]


def find_cycles(
    graph: Graph,
    *,
    kinds: EdgeKinds = None,
    max_cycles: int = 100,
    max_length: int = 32,
    include_self_loops: bool = True,
) -> list[tuple[int, ...]]:
    """Enumerate elementary cycles, bounded by count and length.

    Cycles are discovered per SCC with the classic "only start at the smallest
    node of the cycle" rule, which makes the enumeration duplicate-free without
    materialising all paths. Bounds (``max_cycles``, ``max_length``) guarantee
    the function stays responsive on adversarial graphs — a complete digraph of
    1 000 nodes has an astronomical number of cycles.
    """
    cycles: list[tuple[int, ...]] = []
    cyclic_components = [
        component
        for component in strongly_connected_components(graph, kinds=kinds)
        if len(component) > 1
    ]
    component_of: dict[int, int] = {}
    for position, component in enumerate(cyclic_components):
        for node in component:
            component_of[node] = position

    for node in _self_loop_nodes(graph, kinds):
        if include_self_loops:
            cycles.append((node,))
        if len(cycles) >= max_cycles:
            return cycles

    for position, component in enumerate(cyclic_components):
        members = set(component)
        for start in component:
            if len(cycles) >= max_cycles:
                return cycles
            path: list[int] = [start]
            on_path: set[int] = {start}
            # ``dict.fromkeys``: two edge kinds between one pair are one step of a cycle.
            stack: list[tuple[int, Iterable[int]]] = [
                (start, iter(dict.fromkeys(graph.successors(start, kinds))))
            ]
            while stack:
                node, iterator = stack[-1]
                if len(cycles) >= max_cycles:
                    return cycles
                advanced = False
                for neighbour in iterator:
                    if neighbour == start and len(path) > 1:
                        cycles.append(tuple(path))
                        if len(cycles) >= max_cycles:
                            return cycles
                        continue
                    if (
                        neighbour in on_path
                        or neighbour not in members
                        or component_of.get(neighbour) != position
                        or neighbour < start
                        or len(path) >= max_length
                    ):
                        continue
                    path.append(neighbour)
                    on_path.add(neighbour)
                    stack.append(
                        (neighbour, iter(dict.fromkeys(graph.successors(neighbour, kinds))))
                    )
                    advanced = True
                    break
                if not advanced:
                    stack.pop()
                    removed = path.pop()
                    on_path.discard(removed)
    cycles.sort(key=lambda cycle: (len(cycle), cycle))
    return cycles


@dataclass(frozen=True, slots=True)
class CycleGroup:
    """A strongly connected component that contains a cycle.

    Reporting one finding per SCC instead of one per elementary cycle keeps
    diagnostics readable: a graph where five services form a ring has thousands
    of elementary cycles but exactly one structural problem.
    """

    nodes: tuple[int, ...]
    example: tuple[int, ...] = ()

    def size(self) -> int:
        """Number of nodes or edges in the structure."""
        return len(self.nodes)


def find_cycle_groups(
    graph: Graph,
    *,
    kinds: EdgeKinds = None,
    max_groups: int = 25,
) -> list[CycleGroup]:
    """One entry per cyclic strongly connected component, with an example cycle."""
    groups: list[CycleGroup] = []
    for component in strongly_connected_components(graph, kinds=kinds):
        if len(component) < 2:
            for node in component:
                if any(target == node for target in graph.successors(node, kinds)):
                    groups.append(CycleGroup(nodes=component, example=(node,)))
                    break
            if len(groups) >= max_groups:
                break
            continue
        # A representative cycle is found with a BFS inside the component: it is
        # O(V+E) and therefore safe on dense components where enumerating
        # elementary cycles would explode.
        example = _shortest_cycle(graph, component, kinds)
        groups.append(CycleGroup(nodes=component, example=example))
        if len(groups) >= max_groups:
            break
    return groups


def _shortest_cycle(
    graph: Graph, component: tuple[int, ...], kinds: EdgeKinds = None
) -> tuple[int, ...]:
    """Shortest cycle through the smallest node of ``component`` (BFS, O(V+E))."""
    members = set(component)
    for start in component:
        parents: dict[int, int] = {}
        seen: set[int] = {start}
        queue: deque[int] = deque([start])
        while queue:
            node = queue.popleft()
            for neighbour in graph.successors(node, kinds):
                if neighbour not in members:
                    continue
                if neighbour == start:
                    path = [node]
                    while path[-1] != start:
                        path.append(parents[path[-1]])
                    path.reverse()
                    return tuple(path)
                if neighbour in seen:
                    continue
                seen.add(neighbour)
                parents[neighbour] = node
                queue.append(neighbour)
    return tuple(component[:1])


def cycle_through(graph: Graph, node: int, *, kinds: EdgeKinds = None) -> tuple[int, ...]:
    """A shortest cycle through ``node``, or an empty tuple when it is on none."""
    for component in strongly_connected_components(graph, kinds=kinds):
        if node not in component:
            continue
        if len(component) > 1 or node in graph.successors(node, kinds):
            return _shortest_cycle(graph, (node, *component), kinds)
        break
    return ()


def _find(parent: list[int], node: int) -> int:
    root = node
    while parent[root] != root:
        root = parent[root]
    while parent[node] != root:
        parent[node], node = root, parent[node]
    return root


def connected_components(graph: Graph, *, kinds: EdgeKinds = None) -> list[tuple[int, ...]]:
    """Weakly connected components (edge direction ignored).

    Union-find is used instead of repeated BFS because it is markedly faster on
    sparse, many-component graphs — typical for event architectures where each
    domain is only loosely connected.
    """
    parent = list(range(graph.node_count))
    allowed = kind_values(kinds)
    for edge in graph.edges:
        if allowed is not None and edge.kind.value not in allowed:
            continue
        root_a = _find(parent, edge.source)
        root_b = _find(parent, edge.target)
        if root_a != root_b:
            parent[max(root_a, root_b)] = min(root_a, root_b)
    groups: dict[int, list[int]] = {}
    for node in range(graph.node_count):
        groups.setdefault(_find(parent, node), []).append(node)
    components = [tuple(sorted(members)) for members in groups.values()]
    components.sort(key=lambda component: (-len(component), component[0]))
    return components


def largest_component(graph: Graph, *, kinds: EdgeKinds = None) -> tuple[int, ...]:
    """Size of the largest connected component."""
    components = connected_components(graph, kinds=kinds)
    return components[0] if components else ()


def upstream(
    graph: Graph, node: int, *, max_depth: int | None = None, kinds: EdgeKinds = None
) -> TraversalResult:
    """Everything that can reach ``node`` (producers, upstream services)."""
    return bfs(graph, node, reverse=True, max_depth=max_depth, kinds=kinds)


def downstream(
    graph: Graph, node: int, *, max_depth: int | None = None, kinds: EdgeKinds = None
) -> TraversalResult:
    """Everything ``node`` can reach (consumers, downstream services)."""
    return bfs(graph, node, max_depth=max_depth, kinds=kinds)


def degree_centrality(graph: Graph) -> dict[int, float]:
    """Normalized total degree — cheap proxy for "most connected" nodes."""
    if graph.node_count <= 1:
        return dict.fromkeys(range(graph.node_count), 0.0)
    scale = 1.0 / (graph.node_count - 1)
    return {
        index: (graph.in_degree(index) + graph.out_degree(index)) * scale
        for index in range(graph.node_count)
    }


def impact_propagation(
    graph: Graph,
    start: int,
    *,
    kinds: EdgeKinds = None,
    max_depth: int | None = None,
    weight_decay: float = 0.85,
) -> dict[int, float]:
    """Propagate an impact score outward from ``start``.

    Each hop multiplies the score by ``weight_decay`` (0.85 by default) and
    multiple routes to the same node take the maximum, never the sum, so a node
    reachable through ten paths does not become ten times as risky. This mirrors
    how blast radius is reasoned about in practice.
    """
    scores: dict[int, float] = {start: 1.0}
    frontier: deque[tuple[int, int]] = deque([(start, 0)])
    while frontier:
        node, depth = frontier.popleft()
        if max_depth is not None and depth >= max_depth:
            continue
        for neighbour in graph.successors(node, kinds):
            score = scores[node] * weight_decay
            if score > scores.get(neighbour, 0.0):
                scores[neighbour] = score
                frontier.append((neighbour, depth + 1))
    return scores
