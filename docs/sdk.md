# Python SDK

The CLI is a thin shell over a public Python API. Everything it does is available
from Python, with the same models, the same determinism guarantees and no terminal
dependency (`eventlineage` imports Rich only when rendering to a console).

```python
import eventlineage

project = eventlineage.Project.open("/srv/shop")
catalog = project.scan().catalog
```

---

## Opening a project

`Project.open` finds `eventlineage.yaml` by searching upwards from the given path,
or accepts an explicit config file. It raises `ProjectNotFoundError` (a subclass of
`EventLineageError`) when nothing is found.

```python
from eventlineage import Project

project = Project.open()  # from the cwd
project = Project.open("/srv/shop")  # directory
project = Project.open("/srv/shop/eventlineage.yaml")  # config file
project = Project.open("/srv/shop", jobs=8)  # parallel discovery
project = Project.open(require_config=True)  # never fall back to defaults
```

| Argument | Description |
| --- | --- |
| `path` | directory or `eventlineage.yaml`; `None` searches upwards from the cwd |
| `root=` | override the project root |
| `require_config=` | raise instead of using built-in defaults |
| `jobs=` | worker threads for discovery (`1` = sequential) |
| `registry=` | a custom `PluginRegistry` (see [plugins.md](plugins.md)) |

`Project.create(root, name=..., force=..., minimal=...)` writes a configuration
file and returns its path — the programmatic equivalent of `eventlineage init`.

## Scanning

```python
result = project.scan()  # incremental (uses the cache)
result = project.scan(incremental=False)  # ignore the cache
result = project.scan(["services/billing"])  # scan specific paths
```

`ScanResult` carries everything the scan produced:

```python
result.catalog  # EventCatalog
result.summary  # ScanSummary: events, services, topics, relationships, timings
result.summary.cache  # {"entries": 12, "hits": 12, "misses": 0, "size_bytes": 68636}
result.sources  # tuple[SourceFile, ...] - path, plugin, digest
result.diagnostics  # tuple[Diagnostic, ...] - parse warnings, schema conflicts
```

Later calls (`project.catalog()`, `project.graph()`, `project.validate()`, …)
reuse the in-memory scan; pass `refresh=True` to force a rescan.

The same thing without a `Project`, for pipelines that already hold a `Config`:

```python
from eventlineage import scan_project

result = scan_project(config, paths=["services"], incremental=False)
```

## Per-file plugin runs

If discovery is triggered by an event (a file watcher, a paste handler), iterate:

```python
for source in project.scan().sources:
    print(source.path, source.plugin, source.digest[:8])
```

`source.digest` is the SHA-256 of the file content — useful as a cache key in
downstream tools.

## The catalog

`EventCatalog` is a Pydantic model, so it serializes to stable JSON and can be
round-tripped through disk.

```python
catalog = project.catalog()

catalog.events  # dict[str, Event]
catalog.topics  # dict[str, Topic]
catalog.services  # dict[str, Service]
catalog.relationships  # tuple[EventRelationship, ...]

catalog.sorted_events()  # deterministic order (by id)
catalog.stats()  # CatalogStats
catalog.resolve_event("orders.created")
catalog.events_produced_by("order-service")
catalog.events_consumed_by("notification-service")
catalog.orphan_events()
catalog.unconsumed_events()
catalog.dangling_consumers()
catalog.unknown_participants()
```

Building one by hand — for tests, or to analyse a design that is not in a repo yet:

```python
from eventlineage import Event, Producer, Consumer, build_catalog

catalog = build_catalog(
    project_name="sketch",
    events=[
        Event(
            name="order.created",
            version="1.0.0",
            producers=[Producer(service="orders")],
            consumers=[Consumer(service="billing")],
        )
    ],
)
```

### Working with events

```python
event = catalog.events["orders.created"]

event.id  # fully qualified id
event.version  # active version string
event.active_schema()  # EventSchema | None
event.all_versions()  # every EventVersion, newest first
event.producer_services  # tuple[str, ...]
event.consumer_services  # tuple[str, ...]
event.participant_services()  # producers + consumers
event.is_orphaned
event.is_unconsumed
event.topics
event.owners
event.tags
```

Querying many services at once is `O(events)`, not `O(services × events)` — build
the index once:

```python
index = catalog.service_event_index()
index.produced_by("order-service")  # tuple[Event, ...]
index.consumed_by("billing-service")
index.produced  # Mapping[str, tuple[Event, ...]]
```

## Graphs

```python
graph = project.graph()

graph.node_count, graph.edge_count
graph.nodes  # tuple[Node, ...] sorted by id
graph.edges  # tuple[Edge, ...] sorted by (source, target, kind)
graph.successors(0)  # outgoing neighbours (indices)
graph.predecessors(0, kinds=["consumes"])  # filtered by edge kind
graph.node_by_id("event:orders.created")  # Node | None
graph.index_of("event:orders.created")  # int | None
graph.contains("event:orders.created")  # bool
graph.subgraph(["event:orders.created", "event:payments.captured"])
graph.filtered(["triggers", "derived_from"])
graph.memory_bytes()
```

Algorithms take a graph and never touch the catalog:

```python
from eventlineage.graph.algorithms import (
    bfs,
    dfs,
    topological_sort,
    strongly_connected_components,
    connected_components,
    find_cycle_groups,
    degree_centrality,
)

bfs(graph, start)  # returns BFS order
topological_sort(graph)  # None when the graph has a cycle
strongly_connected_components(graph)
find_cycle_groups(graph)  # one representative cycle per SCC
```

Projections:

```python
from eventlineage import service_flow_graph

flow = service_flow_graph(graph)  # service -> service dependencies only
```

`service_flow_graph` is memoized on the source graph; call `graph.forget("service_flow")`
if you need to release the memory.

## Validation

```python
report = project.validate()

report.findings  # tuple[Finding, ...] sorted, deterministic
report.counts()  # {"error": 3, "warning": 4, "info": 2}
report.by_severity(Severity.ERROR)  # findings of one severity
report.by_rule()  # findings grouped by rule id
report.worst_severity  # highest severity present
report.passed, report.failed  # bool, against the configured threshold
report.threshold()  # the severity threshold that was applied
report.exit_code()  # 0 or 1, matching the CLI
report.suppressed  # findings suppressed by configuration
report.summary()  # one-line human summary
report.to_dict()  # JSON-ready document
```

Each `Finding` carries `rule` (e.g. `EG006`), `rule_name`, `severity`, `message`,
`subject`, `subject_kind`, `source` (`SourceLocation` with path and line), and
`remediation`.

Run a subset, or a custom rule set:

```python
from eventlineage import Severity
from eventlineage.validation.rules import CircularEventDependencyRule

report = project.validate(rules=[CircularEventDependencyRule])
report = project.validate(fail_on=Severity.WARNING)
```

`validate_catalog(catalog, config, rules=..., mode=...)` does the same without a
`Project`.

## Analysis

```python
summary = project.analyze()

summary.health_score  # 0-100
summary.health_grade  # A..F
summary.cycles  # event- and service-level cycles
summary.hotspots  # most depended-on services and events
summary.longest_chains  # longest causal chains
summary.graph  # node/edge/component/memory stats
summary.stats  # entity counts
summary.recommendations  # prioritised, deterministic
summary.denormalised  # precomputed counts reused by renderers
summary.model_dump(mode="json")  # JSON-ready document
```

## Impact analysis

```python
impact = project.impact("orders.created", max_depth=3)

impact.target, impact.target_kind
impact.risk  # low | medium | high | critical
impact.risk_score  # 0-100
impact.max_depth
impact.affected_services  # name, depth, direct, score, role, tier, path
impact.affected_events
impact.affected_schemas
impact.related_topics
impact.direct_consumers, impact.transitive_consumers
impact.cycles_detected
impact.summary()
```

Targets are resolved as `event:id`, `service:name` or `topic:name`; a bare name is
resolved in that order. `SymbolNotFoundError` lists the closest candidates.

## Schema comparison

Compare two versions inside the catalog:

```python
comparison = project.compare_versions("orders.created", "1.2.0", "1.3.0").report
comparison.compatible  # bool
comparison.counts()  # {"breaking": 1, "potentially_breaking": 0, "compatible": 5}
comparison.breaking_changes  # tuple[SchemaChange, ...]
comparison.potentially_breaking_changes
comparison.changes  # each with path, change, impact, remediation
comparison.summary()

for change in comparison.changes:
    print(change.path, change.change, change.impact)
```

Compare two files (paths are read and parsed for you):

```python
report = project.compare_schemas("old.avsc", "new.avsc", mode="full")
report = project.compare_schemas(old_schema, new_schema)  # EventSchema objects
```

Compare two raw definition documents without a project:

```python
from eventlineage import compare_definitions

report = compare_definitions(old_dict, new_dict, format="json-schema", mode="backward")
```

Whole-catalog history:

```python
for entry in project.version_history():  # every consecutive pair
    if not entry.report.compatible:
        print(entry.event_id, entry.old_version, "->", entry.new_version)
```

## Reporting

```python
markdown = project.report(
    format="markdown", include_schemas=True, impacts=["orders.created"], title="Architecture"
)
json_doc = project.report(format="json")
dot = project.report(format="dot")
mermaid = project.report(format="mermaid")
```

Or compose the renderers directly:

```python
from eventlineage import render_markdown_report, render_dot, render_mermaid, render_json

text = render_markdown_report(catalog, summary=summary, validation=report)
open("docs/events.json", "w").write(render_json(summary.model_dump(mode="json")))
```

Reports are deterministic: `generated_at` is only embedded when you pass it, so
two runs on the same catalog produce identical bytes.

## Errors

All exceptions derive from `EventLineageError`:

| Exception | Raised when |
| --- | --- |
| `ProjectNotFoundError` | no `eventlineage.yaml` found |
| `ConfigError` | the configuration is invalid (message includes the offending key) |
| `DiscoveryError` | a plugin cannot read a file it claimed |
| `ParseError` | a document is malformed (carries `path` and `line`) |
| `SymbolNotFoundError` | a target event/service/topic does not exist (carries candidates) |
| `IntegrationUnavailableError` | an optional dependency is missing |

```python
from eventlineage import EventLineageError, SymbolNotFoundError

try:
    project.impact("orders.creatd")
except SymbolNotFoundError as exc:
    print(exc.symbol, exc.kind)  # orders.creatd event
    print(exc.message)  # "... (did you mean: orders.created?)"
    print(exc.hint)  # "run 'eventlineage events' to list available events"
```

## Integrations

`eventlineage pull` is a thin shell over the adapters:

```python
from eventlineage.integrations import SchemaRegistryIntegration

adapter = SchemaRegistryIntegration("http://registry.internal:8081")
result = adapter.collect()  # IntegrationResult: topics, events, notes
document = adapter.export(result)  # the file `eventlineage scan` reads
```

## Logging

The library logs through the standard `logging` module under the `eventlineage`
namespace and never configures handlers itself:

```python
import logging

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
```

Structured (JSON) logging is available from the CLI with `--log-format json`;
library users can install the provided formatter:

```python
from eventlineage.utils.logging import JsonFormatter
```

## Threading and reuse

Catalogs and graphs are immutable after construction and safe to share between
threads. A `Project` caches its scan result; call `scan()` again or use
`refresh=True` to pick up file changes.
