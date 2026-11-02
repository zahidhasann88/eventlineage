# Performance

EventLineage is built for repositories with 10,000+ events. These numbers come from the benchmarks in
this repository, and the raw output is in [`benchmarks/results/`](../benchmarks/results).

**Machine:** Intel Core i7-8665U laptop, 16 GiB RAM, Windows 10, CPython 3.13.14. It was in normal
desktop use, so treat any single figure as accurate to about 10-30%. Your numbers will differ.

**Method:** wall-clock median of 5 runs (3 for the 10,000-event scan) after one warm-up run. Each cold
scan re-parses every file. A warm scan reuses the cache with no files changed.

## Full project

Each project is generated on disk, with one producer and one consumer per event and ten shared
topics. The 10,000-event project has 833 services and 120,037 relationships, which is deliberately
dense.

| Phase | 1,000 events | 10,000 events |
| --- | --- | --- |
| Cold scan | 332 ms | 4,017 ms |
| Warm scan | 210 ms | 3,030 ms |
| Graph build | 60 ms | 837 ms |
| Validation (17 rules) | 268 ms | 5,335 ms |
| Analysis | 176 ms | 3,474 ms |

- The warm scan saves less than you might expect because most of the time goes on merging fragments,
  not parsing them. The cache helps most with large schema files.
- Validation is the slowest phase, so it is the first place to look for a speedup.

## Graph operations

| Operation | 1,000 events (4,300 edges) | 10,000 events (41,500 edges) |
| --- | --- | --- |
| `build_graph` | 15.1 ms | 175.6 ms |
| `service_flow_graph` | 3.6 ms | 50.2 ms |
| `bfs` | 1.0 ms | 12.1 ms |
| `dfs` | 1.2 ms | 14.7 ms |
| `topological_sort` | 0.29 ms | 3.2 ms |
| `strongly_connected_components` | 2.1 ms | 27.4 ms |
| `find_cycle_groups` | 2.2 ms | 27.1 ms |

## Compared with NetworkX

Same graph, 10,000 events:

| Operation | EventLineage | NetworkX | Faster |
| --- | --- | --- | --- |
| Build | 175.6 ms | 52.9 ms | NetworkX, 3x |
| BFS | 12.1 ms | 32.0 ms | EventLineage, 2.7x |
| Strongly connected components | 27.4 ms | 13.7 ms | NetworkX, 2x |

NetworkX is not a runtime dependency. The custom graph was chosen for fixed output order, immutability
and one less dependency, not because it wins everywhere.

## Run it yourself

```console
python benchmarks/bench_scan.py --files 200 --events-per-file 50 --repeat 3 --output scan-10k.json
python benchmarks/bench_graph.py --events 10000 --services 500 --repeat 5 --compare-networkx
```

Use `--files 20` or `--events 1000 --services 50` for the smaller project.

`pytest -m performance` checks time limits on the same operations, with wide margins, to catch
accidental quadratic behaviour.
