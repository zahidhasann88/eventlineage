# CLI reference

```
eventlineage [GLOBAL OPTIONS] COMMAND [COMMAND OPTIONS]
```

Every command reads the project configuration (`eventlineage.yaml`, found by
searching upwards from the working directory), scans or reuses the cached catalog,
and writes its result to stdout — or to a file with `--output`.

## Global options

| Option | Short | Default | Description |
| --- | --- | --- | --- |
| `--config PATH` | `-c` | search upwards | Path to `eventlineage.yaml` |
| `--root PATH` | | config location | Project root directory |
| `--verbose INT` | `-v` | `0` | Increase log verbosity (`-vv` for debug) |
| `--quiet` | `-q` | off | Print errors only |
| `--no-cache` | | off | Disable the incremental scan cache |
| `--log-format TEXT` | | `text` | `text` or `json` (structured logs) |
| `--no-color` | | off | Disable ANSI colours |
| `--version` | | | Print the version and exit |
| `--help` | `-h` | | Show help |

`--log-format json` emits one JSON object per log record on stderr with
`ts`, `level`, `logger`, `message` and any structured fields — ready for
shipping to a log pipeline.

### Commands at a glance

| Command | Purpose |
| --- | --- |
| `init` | write a project configuration |
| `scan` | discover events, services, topics, relationships |
| `analyze` | architecture health, cycles, hotspots, recommendations |
| `validate` | run validation rules, report findings |
| `graph` | render the dependency graph (DOT, Mermaid, JSON) |
| `impact` | blast radius of an event, service or topic |
| `events` | list events with filters |
| `services` | list services and their event involvement |
| `topics` | list topics with participants and configuration |
| `compare` | compare two schema versions or two schema files |
| `report` | Markdown or JSON architecture report |
| `export` | catalog/graph export for other tools |
| `rules` | list validation rules |
| `plugins` | list discovery plugins |
| `integrations` | show optional infrastructure integrations |
| `pull` | fetch from live infrastructure into a file `scan` reads offline |
| `cache` | inspect (`cache info`) or delete (`cache clear`) the scan cache |
| `version` | print the version |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | success, nothing breached the configured threshold |
| `1` | findings at or above the failure threshold (validation, impact risk, breaking schema change) |
| `2` | usage error: invalid configuration, empty scan, bad arguments, unknown event or version, schemas that cannot be compared |
| `3` | runtime error: parse failure, I/O error, unexpected crash |

Exit code `1` is the one CI cares about: it means "the tool ran correctly and the
answer is: this needs attention".

---

## `eventlineage init`

```console
eventlineage init [PATH] [--name TEXT] [--force] [--minimal]
```

Writes `eventlineage.yaml` into `PATH` (default `.`). `--minimal` writes a
four-line config; `--force` overwrites an existing file. Existing files are never
overwritten silently.

## `eventlineage scan`

```console
eventlineage scan [PATHS]... [--format table|json] [--output PATH] [--full]
```

Discovers catalog fragments. Without `PATHS` the configured roots are scanned;
explicit paths override the configuration for that run.

| Option | Description |
| --- | --- |
| `--format` / `-f` | `table` (default) or `json` |
| `--output` / `-o` | write to a file instead of stdout |
| `--full` | ignore the incremental cache and re-parse every file |

The table shows the file accounting (considered / parsed / cached / ignored /
failed), the entity counts, the scan duration, the per-plugin file counts and any
diagnostics (parse warnings, schema conflicts).

## `eventlineage analyze`

```console
eventlineage analyze [--format table|json|markdown] [--output PATH]
```

Produces an `ArchitectureSummary`: entity stats, graph stats (nodes, edges,
components, whether the graph is acyclic, memory), a health score and grade,
event- and service-level cycles, hotspots, longest chains and prioritised
recommendations.

## `eventlineage validate`

```console
eventlineage validate [--fail-on SEVERITY] [--rule ID]... [--disable ID]...
                    [--severity RULE=SEVERITY]... [--format ...] [--output PATH]
```

| Option | Description |
| --- | --- |
| `--fail-on` | exit `1` when a finding at or above this severity exists (`error`, `warning`, `info`) |
| `--rule` | run only the given rules (id or name, repeatable) |
| `--disable` | skip the given rules (repeatable) |
| `--severity` | override a rule's severity, e.g. `EG002=warning`; `off` disables it |

Findings are sorted deterministically by severity, rule id and subject. The
footer reports counts, the active threshold, and how many findings were suppressed
by configuration. `--format markdown` is convenient for PR comments;
`--format json` carries the rule id, description, remediation, source location and
tags for each finding.

## `eventlineage graph`

```console
eventlineage graph [--format dot|mermaid|json] [--output PATH]
                 [--edge-kinds LIST] [--node-kinds LIST] [--max-nodes N]
                 [--title TEXT] [--direction LR] [--services-only]
```

| Option | Description |
| --- | --- |
| `--format` / `-f` | `mermaid` (default), `dot`, `json` |
| `--edge-kinds` | comma-separated or repeated: `produces,consumes,publishes_to,subscribes_to,routes_via,triggers,derived_from,depends_on` |
| `--node-kinds` | comma-separated or repeated: `event,service,topic` |
| `--max-nodes` | keep the first N nodes in node-id order |
| `--services-only` | project onto services (collapses `service → event → service` paths) |
| `--direction` | Mermaid direction: `LR`, `TD`, `TB`, `RL`, `BT` |

Node and edge order is deterministic, so diagrams do not churn between runs.

## `eventlineage impact`

```console
eventlineage impact TARGET [--depth N] [--fail-on-risk LEVEL] [--format ...] [--output PATH]
```

`TARGET` may be an event id, a service name or a topic name; `event:`, `service:`
and `topic:` prefixes disambiguate when a name collides.

The report contains direct and transitive consumers, affected services with depth
and path, affected events, affected schemas, related topics, producers, detected
cycles on the path, a risk level (`low`/`medium`/`high`/`critical`) and a 0-100
risk score. `--fail-on-risk high` exits `1` when the blast radius is at or above
that level.

```console
$ eventlineage impact orders.created --format json | jq '.risk, .affected_services | length'
"high"
6
```

## `eventlineage events`

```console
eventlineage events [QUERY] [--namespace NS] [--service NAME] [--tag TAG]
                  [--orphaned] [--unconsumed] [--no-schema] [--limit N]
                  [--format table|json|markdown] [--output PATH]
```

`QUERY` is a substring or glob matched against event ids. Filters combine with
AND. `--limit` truncates the table (JSON output is never truncated).

## `eventlineage services`

```console
eventlineage services [QUERY] [--tier TIER] [--format table|json] [--output PATH]
```

Lists services with kind, tier, produce/consume counts and the events they
consume.

## `eventlineage topics`

```console
eventlineage topics [--format table|json] [--output PATH]
```

Lists topics with broker, partition count, replication factor, producers,
consumers and events.

## `eventlineage compare`

```console
eventlineage compare OLD NEW [--event ID] [--mode backward|forward|full]
                  [--schema-format avro|protobuf|json-schema]
                  [--format table|json|markdown] [--no-fail]
```

Two modes:

**Files.** `OLD` and `NEW` are schema file paths; the dialect is inferred from the
file name and content, or forced with `--schema-format`.

```console
$ eventlineage compare schemas/orders.v1.json schemas/orders.v2.json
```

**Versions.** With `--event ID`, `OLD` and `NEW` are version strings of one event
in the catalog:

```console
$ eventlineage compare --event orders.created 1.2.0 1.3.0
```

The command exits `1` when the comparison is breaking under the given mode (use
`--no-fail` or `compatibility.fail_on_breaking: false` to always exit `0`). Each change carries a path, a change type, an
impact (`compatible`, `potentially_breaking`, `breaking`) and a remediation hint.

## `eventlineage report`

```console
eventlineage report [--format markdown|json] [--output PATH] [--include-schemas]
                  [--diagrams/--no-diagrams] [--impact TARGET]... [--title TEXT]
                  [--timestamp]
```

Generates the architecture document: summary, health, entity inventory, services,
topics, cycles, hotspots, findings, schema inventory (with `--include-schemas`),
Mermaid diagrams (on by default) and optional impact sections.

Output is byte-identical between runs unless `--timestamp` is passed — the
report is safe to diff in CI.

## `eventlineage export`

```console
eventlineage export [--format json|catalog|graph|dot|mermaid] [--output PATH]
                  [--include-schemas] [--max-nodes N]
```

| Format | Content |
| --- | --- |
| `json` | full catalog document (stable key order) |
| `catalog` | EventLineage native catalog, re-loadable by the `catalog` plugin |
| `graph` | nodes + edges as JSON |
| `dot` | Graphviz DOT |
| `mermaid` | Mermaid flowchart |

## `eventlineage rules`, `plugins`, `integrations`

```console
eventlineage rules          # id, name, severity, description for all 17 rules
eventlineage plugins        # plugin name, filename patterns, priority, description
eventlineage integrations   # integration name, availability, required package
```

`integrations` reports whether the optional dependency for each adapter is
installed and never attempts a network call.

`rules` includes rules from installed packages (see [plugins.md](plugins.md)).

## `eventlineage pull`

```console
eventlineage pull kafka|schema-registry|rabbitmq|nats [--output PATH]
```

The only command that opens a network connection. It reads `integrations.*`
([configuration.md](configuration.md)) and prints a sorted JSON export, or writes it
with `--output`. `eventlineage scan` then reads that file like any other source:

```console
$ eventlineage pull schema-registry --output registry/subjects.json
$ eventlineage pull kafka --output kafka/topics.json
$ eventlineage validate
```

Kafka topics starting with `_` are skipped unless `include_internal` is set. The
adapter needs its optional dependency (`pip install "eventlineage[kafka]"`). A connection
failure exits `3`.

## `eventlineage cache`

```console
$ eventlineage cache info
cache: /srv/shop/.eventlineage/cache/scan.json
entries: 12
size: 68636 bytes
matches current config: yes

$ eventlineage cache clear
removed /srv/shop/.eventlineage/cache/scan.json
```

The cache lives in `.eventlineage/cache/scan.json` by default and is keyed by the digest
of each file and of the files it refers to, plus the configuration and the EventLineage
release, so any of those changing invalidates the affected entries.

## Using it in CI

```yaml
# .github/workflows/architecture.yml
name: architecture
on: [pull_request]

jobs:
  eventlineage:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.13" }
      - run: pip install eventlineage
      - run: eventlineage validate --fail-on warning
      - run: eventlineage compare --event orders.created 1.2.0 1.3.0
      - run: eventlineage report --output architecture.md
```

`validate` and `compare` exit `1` on a problem, which fails the pull request.

## Environment variables

| Variable | Effect |
| --- | --- |
| `EVENTLINEAGE_CONFIG` | path to the configuration file (same as `--config`) |
| `EVENTLINEAGE_LOG_LEVEL` | default log level |
| `EVENTLINEAGE_NO_CACHE` | any non-empty value disables the cache |
| `EVENTLINEAGE_COMPAT_MODE` | default compatibility mode |
| `EVENTLINEAGE_FAIL_ON` | default validation threshold |

Precedence: command-line flags, then environment variables, then
`eventlineage.yaml`, then built-in defaults.
