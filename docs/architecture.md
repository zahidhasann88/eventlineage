# Architecture

Core logic never imports the CLI or the integrations, and Rich is used only for terminal output.
You can use the engine from a library, a service or a notebook.

## How a scan works

```
files → discovery plugins → fragments → catalog → graph → validation, analysis, reports
```

1. **Collect.** Walk the configured roots, applying exclude globs and the file-size limit.
2. **Detect.** Each plugin scores the file from 0 to 100. The highest score wins.
3. **Discover.** The chosen plugin turns the file into events, topics, services and relationships.
4. **Cache.** Results are stored by file digest, with the digest of every file they refer to.
5. **Merge.** Fragments of one event are combined. New versions extend its history. Two different
   schemas for one version keep the first and report a diagnostic.
6. **Derive.** Create implied topics, build the dependency list and infer event-to-event links
   through services.

## Packages

| Package | Purpose |
| --- | --- |
| `core` | Scan pipeline, catalog and fragment merging |
| `models` | Pydantic models such as `Event`, `EventSchema`, `Topic` and `Service` |
| `discovery` | Plugin API and the nine built-in formats |
| `parsers` | JSON Schema, Avro and Protobuf turned into one field model |
| `graph` | Immutable graph, algorithms and projections |
| `validation` | Rule engine and the 17 rules |
| `compatibility` | Schema comparison |
| `analysis` | Impact analysis and architecture summary |
| `reporting` | JSON, Markdown, DOT, Mermaid and terminal output |
| `integrations` | Optional Kafka, Schema Registry, RabbitMQ and NATS adapters |
| `cli` | Typer commands |
| `utils` | Cache, YAML, versions, logging and file helpers |

## Key decisions

- **Custom graph instead of NetworkX.** `Graph` stores adjacency in compressed integer arrays and is
  never changed after it is built. Output order stays fixed and graphs are safe to share. NetworkX
  wins on some operations; see [performance.md](performance.md).
- **Deterministic output.** Everything that reaches a report is sorted, so two runs on the same files
  give identical bytes unless you pass `--timestamp`.
- **Offline by default.** `eventlineage pull` is the only command that opens a network connection. It
  writes a file, and every later scan reads that file.
- **One field model.** Each schema format is lowered to the same fields, and format rules live in
  small comparers: Avro in `compatibility/avro.py`, Protobuf in `compatibility/protobuf.py`.

## Development

```console
python -m venv .venv
pip install -e ".[all,dev]"
pytest                   # unit and integration tests
pytest -m performance    # large synthetic catalogs
ruff check . && ruff format --check .
mypy
```

The default tests need no network: adapters use fake clients. To test them against real brokers:

```console
docker compose -f tests/live/docker-compose.yml up -d --wait
pytest -m live
docker compose -f tests/live/docker-compose.yml down -v
```

## Releasing

Update `__version__` in `src/eventlineage/__init__.py` and `CHANGELOG.md`, then push a `vX.Y.Z` tag.
`.github/workflows/release.yml` runs CI, publishes to PyPI and creates the GitHub release. Running
that workflow by hand publishes to TestPyPI instead.
