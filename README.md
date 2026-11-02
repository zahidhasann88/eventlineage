# EventLineage

See how events flow through your system, and what breaks before you change one.

EventLineage reads the contracts already in your repository: JSON Schema, Avro, Protobuf, AsyncAPI,
OpenAPI, Kafka topic exports, Schema Registry exports and hand-written catalogs. It builds one
dependency graph from them, then finds missing producers, circular dependencies and breaking schema
changes. It works offline on local files, as a CLI and as a Python library.

## Install

```console
pip install eventlineage
```

Requires Python 3.11+. To read from live systems, add an extra: `kafka`, `schema-registry`,
`rabbitmq`, `nats` or `all`, for example `pip install "eventlineage[kafka]"`.

## Quick start

Describe two services and the events between them in `events/catalog.yaml`:

```yaml
version: 1
services:
  order-service:
    produces: [orders.created]
    consumes: [payments.captured]
  billing-service:
    produces: [payments.captured]
    consumes: [orders.created]
```

Then run:

```console
eventlineage init                    # create eventlineage.yaml
eventlineage scan                    # discover events, services and topics
eventlineage validate                # finds the cycle above and exits 1
eventlineage impact orders.created   # see what a change would affect
```

In a repository that already has schemas or AsyncAPI files, skip the catalog: `scan` finds them.

## Main commands

| Command | What it does |
| --- | --- |
| `scan` | Discover events, services and topics |
| `validate` | Run the 17 rules, such as missing producers and circular dependencies |
| `analyze` | Show a health score, hotspots and cycles |
| `impact TARGET` | Show what a change to an event, service or topic affects |
| `compare OLD NEW` | Compare two schema files, or two versions with `--event ID` |
| `graph` | Draw the graph as Mermaid or DOT, or output it as JSON |
| `report` | Write a Markdown or JSON architecture report |
| `pull NAME` | Save data from a live broker or registry into a file `scan` can read |

Add `--format json` for machine-readable output. Exit codes: `0` success, `1` findings or a breaking
change, `2` usage or configuration error, `3` runtime error.

## Python

```python
from eventlineage import Project

project = Project.open(".")
print(project.scan().summary.events, "events")

for finding in project.validate().findings:
    print(finding.rule, finding.message)

print(project.impact("orders.created").summary())
```

## Documentation

- [CLI reference](https://github.com/zahidhasann88/eventlineage/blob/main/docs/cli.md)
- [Python SDK](https://github.com/zahidhasann88/eventlineage/blob/main/docs/sdk.md)
- [Configuration and catalog format](https://github.com/zahidhasann88/eventlineage/blob/main/docs/configuration.md)
- [Schema compatibility](https://github.com/zahidhasann88/eventlineage/blob/main/docs/compatibility.md)
- [Plugins and custom rules](https://github.com/zahidhasann88/eventlineage/blob/main/docs/plugins.md)
- [Architecture and development](https://github.com/zahidhasann88/eventlineage/blob/main/docs/architecture.md)
- [Performance](https://github.com/zahidhasann88/eventlineage/blob/main/docs/performance.md)
- [Example project](https://github.com/zahidhasann88/eventlineage/tree/main/examples)
- [Changelog](https://github.com/zahidhasann88/eventlineage/blob/main/CHANGELOG.md)

## License

Apache-2.0. See [LICENSE](https://github.com/zahidhasann88/eventlineage/blob/main/LICENSE).
