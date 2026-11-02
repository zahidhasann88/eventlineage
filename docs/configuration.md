# Configuration and catalog format

EventLineage reads one YAML file, `eventlineage.yaml`, found by searching upwards from
the working directory (or given with `--config` / `EVENTLINEAGE_CONFIG`). Everything
else it knows comes from the artifacts already in the repository.

Configuration is layered. Later layers win:

1. built-in defaults,
2. `eventlineage.yaml`,
3. `EVENTLINEAGE_*` environment variables,
4. command-line flags.

An unknown key is an error, not a silent no-op — `ConfigError` names the offending
key and the file it came from.

---

## `eventlineage.yaml` reference

Below is a complete file; every key is optional and the value shown is the
default unless noted.

```yaml
version: 1                    # configuration schema version, currently 1

project:
  name: my-platform           # shown in reports; defaults to the directory name
  root: .                     # scan root, relative to this file
  version: null               # free-form, reported but not interpreted

discovery:
  roots: [.]                  # directories to walk
  include: []                 # extra globs; empty = every file a plugin recognises
  exclude:                    # globs, matched against the relative path
    - "**/.git/**"
    - "**/node_modules/**"
    - "**/.venv/**"
    - "**/dist/**"
    - "**/build/**"
    - "**/target/**"
    - "**/.eventlineage/**"
  plugins: []                 # whitelist of plugin names; empty = all registered
  disabled_plugins: []        # blacklist, applied after `plugins`
  follow_symlinks: false
  max_file_size_kb: 4096      # larger files are skipped with a diagnostic
  skip_hidden_dirs: true
  protobuf:
    event_suffixes: [Event, Command, Message]   # messages treated as events
    use_annotations: true                       # read (eventlineage.*) options
    include_services: true                      # proto services -> Service records

validation:
  fail_on: error              # error | warning | info | off — the exit-1 threshold
  severity: {}                # per-rule overrides, e.g. {EG002: warning, EG012: off}
  disabled_rules: []          # rule ids or names
  only_rules: []              # if non-empty, run only these
  suppress:                   # accepted exceptions, with a reason
    - rule: EG001
      subject: legacy.audit.written     # optional: event/service/topic name
      path: services/legacy/**          # optional: source path glob
      reason: produced by a vendor system outside this repository

compatibility:
  mode: backward              # backward | forward | full
  fail_on_breaking: true      # `compare` exits 1 on a breaking change

analysis:
  infer_relationships: true   # infer event -> event causality through services
  include_topics: true        # topics participate in the graph
  max_impact_depth: null      # traversal cap for `impact`
  risk_high_services: 5       # >= this many affected services = HIGH
  risk_critical_services: 15  # >= this many affected services = CRITICAL

cache:
  enabled: true
  directory: .eventlineage/cache
  max_bytes: 67108864         # 64 MiB; a larger cache is discarded on load

report:
  title: null                 # defaults to the project name
  include_schemas: false
  include_diagrams: true
  max_events: null            # truncate long event tables (JSON is never truncated)
  max_impacted_nodes: 200

integrations:                 # all optional, all offline-by-default
  kafka: {}
  schema_registry: {}
  rabbitmq: {}
  nats: {}
```

### Integration settings

`eventlineage pull NAME` passes its section to the adapter. Nothing else contacts live
infrastructure, and an unknown key is an error. `${NAME}` is replaced from the
environment, so secrets stay out of the file.

```yaml
integrations:
  kafka:
    bootstrap_servers: "broker-1:9092"
    timeout: 10.0
    config: { security.protocol: SASL_SSL }   # passed to the Kafka client
    include_internal: false                   # also list `_`-prefixed topics
  schema_registry:
    url: "http://registry.internal:8081"
    username: ci
    password: ${REGISTRY_PASSWORD}
    include_keys: false
  rabbitmq:
    url: "http://rabbit.internal:15672/api"   # the management API
    username: guest
    password: ${RABBITMQ_PASSWORD}
    vhost: /
  nats:
    servers: ["nats://nats.internal:4222"]
```

`pull` writes a sorted file that `scan` reads with no network and no optional
dependency, so commit it and let CI work from the file.

---

## Environment variables

| Variable | Overrides |
| --- | --- |
| `EVENTLINEAGE_CONFIG` | path to the configuration file |
| `EVENTLINEAGE_LOG_LEVEL` | log level |
| `EVENTLINEAGE_NO_CACHE` | any non-empty value disables the cache |
| `EVENTLINEAGE_COMPAT_MODE` | `compatibility.mode` |
| `EVENTLINEAGE_FAIL_ON` | `validation.fail_on` |

---

## The native catalog format

The `catalog` plugin reads JSON or YAML files that declare `events`, `services`,
`topics` or `relationships`. It is the richest source and the right place to
record decisions that cannot be inferred from code: ownership, tiers, version
history, explicit relationships and accepted exceptions.

The plugin claims a file when it has an `events`/`services`/`topics`/
`relationships` key or an `eventlineage:` marker at the top level.

### Events

```yaml
version: 1
events:
  - name: orders.created                 # required
    id: orders.created                   # optional; defaults to name
    namespace: orders                    # optional grouping
    version: 1.2.0                       # active version; defaults to newest declared
    status: active                       # active | deprecated | retired
    description: An order was accepted.
    owners: [team-orders]
    tags: [core, revenue]
    topics: [orders.v1]
    schema: { ... }                      # see below
    versions: [ ... ]                    # history, newest first is not required
    producers:
      - service: order-service
        schema_version: 1.2.0            # what this producer emits
        kind: service                    # service | application | external | unknown
    consumers:
      - service: billing-service
        schema_version: 1.2.0
      - service: search-indexer
        kind: external
    metadata: { ... }                    # free-form, preserved
```

`producers`/`consumers` also accept the short form `producers: [order-service]`, and
the aliases `produced_by` / `consumed_by`.

### Schema references

Four forms are supported:

```yaml
# 1. file reference (relative to the catalog file); the dialect is detected
schema:
  format: json-schema
  file: ../schemas/orders.created.v1_2.json

# 2. inline definition
schema:
  format: avro
  definition:
    type: record
    name: OrderCreated
    fields: [...]

# 3. reference into the same document (JSON pointer)
schema:
  ref: "#/definitions/OrderCreated"

# 4. protobuf, selecting one message from a file
schema:
  format: protobuf
  file: ../proto/inventory.proto
  message: shop.inventory.InventoryReservedEvent
```

`format` is one of `json-schema`, `avro`, `protobuf`; when omitted it is detected
from the content (`$schema`/`properties`, `type: record` + `fields`, `syntax =
"proto3"`). A bare inline document is also accepted.

### Version history

```yaml
    versions:
      - version: 1.0.0
        released_at: 2024-02-11
        status: active
        notes: initial contract
        schema: { format: json-schema, file: ../schemas/orders.created.v1.json }
      - version: 1.1.0
        released_at: 2024-08-03
        notes: added `channel` (optional)
        schema: { format: json-schema, file: ../schemas/orders.created.v1_1.json }
```

The `schema` on the event itself applies to the active version; entries in
`versions` may omit it to inherit the event schema. Version strings are compared
semantically (`1.10.0` > `1.9.0`).

### Services

```yaml
services:
  order-service:
    kind: service                    # service | gateway | worker | external
    tier: tier-1                     # free-form; used by impact risk scoring
    language: kotlin
    owners: [team-orders]
    produces: [orders.created]       # by id or name
    consumes: [payments.captured]
```

`services` may be a mapping keyed by name (as above) or a list with a `name` key.

### Topics

```yaml
topics:
  - name: orders.v1
    broker: kafka                    # kafka | rabbitmq | nats | ...
    partitions: 12
    replication_factor: 3
    config:
      retention.ms: "604800000"
      cleanup.policy: delete
```

### Relationships

```yaml
relationships:
  - from: payments.failed            # source event
    to: orders.cancelled             # target event
    kind: triggers                   # triggers | derived_from | correlates
    description: A failed payment cancels the order.
    inferred: false
    confidence: 1.0
```

Relationship semantics:

| Kind | Meaning |
| --- | --- |
| `triggers` | handling `from` causes `to` to be emitted |
| `derived_from` | `to` is computed from `from` (aggregates, projections) |
| `correlates` | the two events describe the same business process; no ordering implied, so it creates no graph edge |

The graph projection additionally uses `depends_on` edges for service-to-service
dependencies it derives itself.

### Full example

[`examples/shop-platform/catalogs/orders.yaml`](../examples/shop-platform/catalogs/orders.yaml)
uses every feature above, together with the other eight discovery formats.

---

## Which files are read

| Format | Plugin | Detected by |
| --- | --- | --- |
| `*.json`, `*.yaml`, `*.yml` | `asyncapi` | `asyncapi:` key / `asyncapi` version field |
| `*.avsc`, `*.avro`, `*.json` | `avro` | `type: record` + `fields`, or a bare Avro record |
| `*.json`, `*.yaml`, `*.yml` | `catalog` | `events`/`services`/`topics`/`relationships` keys, `eventlineage:` marker |
| `*.json` | `json-schema` | `$schema`, `properties`, `type: object` without event metadata; the event is named by `x-event-id`, a one-word `title`, or the file name, and `name.v1_2.json` means version `1.2` |
| `*.json`, `*.yaml`, `*.yml` | `kafka-topics` | topic arrays / `topics: [...]` with partitions |
| `*.json`, `*.yaml`, `*.yml` | `openapi` | `openapi:` + `x-events`, `webhooks` or `callbacks` |
| `*.proto` | `protobuf` | `syntax = "proto3"` |
| `*.json`, `*.yaml`, `*.yml` | `schema-registry-export` | subject/version/schema arrays |
| `*.yaml`, `*.yml`, `*.json` | `service-config` | service definitions with produce/consume lists |

Standalone files of one event under different versions build its version history.
Two sources that give one version different schemas are a conflict: the first wins and
a diagnostic is reported. Schema Registry numbers are per-subject counters, so they
never extend a catalog's history.

Several plugins can claim the same file; the highest-confidence detection wins, and
`priority` breaks ties. `eventlineage plugins` shows the current registry, and
`discovery.plugins` / `discovery.disabled_plugins` limit it.

The cache key includes the configuration, the EventLineage release and every file a
source refers to, so edits and upgrades invalidate it automatically.
