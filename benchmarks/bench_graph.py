#!/usr/bin/env python3
"""Benchmark the graph engine on synthetic topologies.

Usage::

    python benchmarks/bench_graph.py --events 10000 --services 500 --repeat 5
    python benchmarks/bench_graph.py --compare-networkx   # optional cross-check

Output is a JSON document (``--output`` writes it to a file) so results can be
tracked over time.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import emit, environment, measure, networkx_available
from eventlineage.analysis import analyze_catalog
from eventlineage.graph.algorithms import (
    bfs,
    degree_centrality,
    dfs,
    find_cycle_groups,
    strongly_connected_components,
    topological_sort,
)
from eventlineage.graph.builder import build_graph, service_flow_graph
from synthetic import layered_catalog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=10_000)
    parser.add_argument("--services", type=int, default=500)
    parser.add_argument("--fan-out", type=int, default=2)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--output", default=None)
    parser.add_argument(
        "--compare-networkx",
        action="store_true",
        help="Also time NetworkX traversals when it is installed",
    )
    args = parser.parse_args(argv)

    catalog = layered_catalog(args.events, args.services, fan_out=args.fan_out)
    graph = build_graph(catalog)

    results = {
        "environment": environment(),
        "topology": {
            "events": catalog.stats().events,
            "services": catalog.stats().services,
            "topics": catalog.stats().topics,
            "graph_nodes": graph.node_count,
            "graph_edges": graph.edge_count,
            "graph_memory_bytes": graph.memory_bytes(),
        },
        "timings": {
            "build_graph": measure(lambda: build_graph(catalog), repeat=args.repeat),
            # The projection is memoized on the graph, so forget it between
            # runs to time the cold path.
            "service_flow_graph": measure(
                lambda: (graph.forget("service_flow"), service_flow_graph(graph))[1],
                repeat=args.repeat,
            ),
            "bfs": measure(lambda: bfs(graph, 0), repeat=args.repeat),
            "dfs": measure(lambda: dfs(graph), repeat=args.repeat),
            "topological_sort": measure(lambda: topological_sort(graph), repeat=args.repeat),
            "strongly_connected_components": measure(
                lambda: strongly_connected_components(graph), repeat=args.repeat
            ),
            "cycle_groups": measure(lambda: find_cycle_groups(graph), repeat=args.repeat),
            "degree_centrality": measure(lambda: degree_centrality(graph), repeat=args.repeat),
            "analyze_catalog": measure(
                lambda: analyze_catalog(catalog), repeat=max(1, args.repeat // 2)
            ),
        },
    }

    if args.compare_networkx and networkx_available():
        import networkx as nx

        def to_networkx():
            instance = nx.DiGraph()
            instance.add_nodes_from(range(graph.node_count))
            instance.add_edges_from((edge.source, edge.target) for edge in graph.edges)
            return instance

        import pickle

        nx_graph = to_networkx()
        results["topology"]["networkx_pickle_bytes"] = len(pickle.dumps(nx_graph))
        results["timings"]["networkx_build"] = measure(to_networkx, repeat=args.repeat)
        results["timings"]["networkx_bfs"] = measure(
            lambda: list(nx.bfs_tree(nx_graph, 0)), repeat=args.repeat
        )
        results["timings"]["networkx_strongly_connected"] = measure(
            lambda: list(nx.strongly_connected_components(nx_graph)), repeat=args.repeat
        )
        results["networkx_available"] = True
    else:
        results["networkx_available"] = False

    emit(results, output=args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
