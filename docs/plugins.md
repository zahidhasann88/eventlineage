# Writing a discovery plugin

A discovery plugin turns one file into catalog fragments. EventLineage ships nine
(`eventlineage plugins`); a tenth — yours — is a subclass and a registration call.

## The contract

```python
class DiscoveryPlugin(ABC):
    name: ClassVar[str]  # stable id, used by config and logs
    patterns: ClassVar[tuple[str, ...]]  # filename globs, e.g. ("*.proto",)
    priority: ClassVar[int] = 100  # ties between equally confident plugins
    description: ClassVar[str] = ""  # shown by `eventlineage plugins`
    extensions: ClassVar[tuple[str, ...]] = ()

    def supports(self, path: Path) -> bool: ...
    def detect(self, path: Path, head: str) -> int: ...  # 0-100 confidence
    def discover(self, path: Path, ctx: DiscoveryContext) -> DiscoveryResult: ...
```

`detect` receives the first 8 KB of the file (already decoded) and returns a
confidence score. Several plugins may claim the same file — a `.json` file can be
an AsyncAPI document, a JSON Schema, a Kafka export or a catalog — and the highest
score wins. `priority` only breaks ties.

If the result depends on another file, call `ctx.reference(path)` (or read it through
`load_referenced_document(..., ctx=ctx)`): the scan cache then hashes that file too, so
editing it invalidates this one.

`discover` parses the file and returns a `DiscoveryResult`. Raise `ParseError` for
malformed input (it carries the path and line and becomes a diagnostic rather than
a crash) and call `result.warn(...)` for things the user should see but that do not
stop the scan.

## A worked example

Imagine your services declare their events in a small DSL, `*.events.yaml`:

```yaml
# services/billing/events.yaml
service: billing
tier: tier-1
emits:
  - name: payments.captured
    schema: ../../schemas/payments.captured.avsc
listens:
  - orders.created
```

The plugin is ~50 lines:

```python
from pathlib import Path

from eventlineage.discovery import (
    DiscoveryContext,
    DiscoveryPlugin,
    DiscoveryResult,
    coerce_participant,
    coerce_service,
    register_plugin,
)
from eventlineage.models import Event
from eventlineage.models.schema import EventSchema
from eventlineage.parsers import detect_schema_format


class ServiceEventsPlugin(DiscoveryPlugin):
    name = "service-events"
    patterns = ("*.events.yaml", "*.events.yml")
    priority = 140
    description = "Service-local event declarations (*.events.yaml)"
    extensions = (".yaml", ".yml")

    def detect(self, path: Path, head: str) -> int:
        if not path.name.endswith((".events.yaml", ".events.yml")):
            return 0
        return 90 if ("emits:" in head or "listens:" in head) else 40

    def discover(self, path: Path, ctx: DiscoveryContext) -> DiscoveryResult:
        result = DiscoveryResult()
        document = self.load_yaml(path, ctx)  # ParseError on bad YAML
        source = self.source(path, ctx)  # SourceLocation(path, plugin)

        service_name = document.get("service")
        if not service_name:
            result.warn("missing 'service' key", source=source, code="no-service")
            return result

        service = result.add_service(
            coerce_service({"name": service_name, "tier": document.get("tier")}, source=source)
        )

        for entry in document.get("emits", []):
            name = entry["name"] if isinstance(entry, dict) else str(entry)
            schema = None
            if isinstance(entry, dict) and entry.get("schema"):
                schema_path = path.parent / entry["schema"]
                ctx.reference(schema_path)  # edits to it must invalidate the scan cache
                reference = self.load_json_or_yaml(schema_path, ctx)
                schema = EventSchema(
                    format=detect_schema_format(reference),
                    definition=reference,
                    source=source,
                )
            result.add_event(
                Event(
                    id=name,  # the qualified id, e.g. "payments.captured"
                    name=name,
                    version=str(entry.get("version", "1.0.0"))
                    if isinstance(entry, dict)
                    else "1.0.0",
                    schema=schema,
                    source=source,
                    producers=[coerce_participant(service.name, consumer=False, source=source)],
                )
            )

        for entry in document.get("listens", []):
            name = entry["name"] if isinstance(entry, dict) else str(entry)
            result.add_event(
                Event(
                    id=name,
                    name=name,
                    source=source,
                    consumers=[coerce_participant(service.name, consumer=True, source=source)],
                )
            )

        return result


register_plugin(ServiceEventsPlugin)
```

Two things make it robust:

- `coerce_service` / `coerce_participant` accept both the short form (`"billing"`)
  and the full mapping, so your DSL can stay terse.
- Fragments are merged by the engine. If another plugin also declares
  `orders.created`, both definitions union their producers and consumers; a
  conflicting schema is reported as a diagnostic instead of failing the scan.

## Registering

Three ways, in increasing order of "someone else installs this":

```python
from eventlineage.discovery import register_plugin

register_plugin(MyPlugin)  # 1. at import time
```

```python
from eventlineage.discovery import PluginRegistry

registry = PluginRegistry()
registry.register(MyPlugin)

from eventlineage import Project

project = Project.open(".", registry=registry)  # 2. explicit registry
```

```toml
# 3. an installed package advertises its plugins
[project.entry-points."eventlineage.discovery"]
my-format = "mypkg.discovery:MyPlugin"
```

Entry points are loaded lazily on the first scan and can be disabled per project
with `discovery.disabled_plugins: [my-format]`.

`register_plugin` is also usable as a class decorator, and raises if a *different*
plugin already owns the name (pass `replace=True` when you are intentionally
overriding a built-in).

## Activating and restricting

```yaml
discovery:
  plugins: [catalog, service-events]   # allow-list; empty = all
  disabled_plugins: [openapi]          # applied after the allow-list
```

Unknown names are logged as warnings rather than raising — a config file shared
with a team that has not installed your plugin yet should still scan.

## Testing a plugin

The engine's own tests use a temporary directory and the SDK; the same pattern
works for third-party plugins:

```python
from eventlineage import Project


def test_service_events_plugin(tmp_path):
    (tmp_path / "eventlineage.yaml").write_text("version: 1\n")
    (tmp_path / "billing.events.yaml").write_text(
        "service: billing\nemits:\n  - name: payments.captured\nlistens: [orders.created]\n"
    )

    result = Project.open(tmp_path).scan()

    assert "payments.captured" in result.catalog.events
    assert result.catalog.events["payments.captured"].producer_services == ("billing",)
    assert result.summary.files_parsed == 1
```

Call `discover` directly when you want to test parsing without a project:

```python
from pathlib import Path
from eventlineage.config import Config
from eventlineage.discovery import DiscoveryContext

ctx = DiscoveryContext(root=Path("."), config=Config())
result = ServiceEventsPlugin().discover(Path("billing.events.yaml"), ctx)
assert [event.id for event in result.events] == ["payments.captured"]
```

## Custom validation rules

Rules are the other extension point. Subclass `Rule`, and installed packages reach the
CLI through an entry point:

```python
from eventlineage import Severity
from eventlineage.validation import Rule


class OwnerRule(Rule):
    id = "ACME001"  # stable: used by --rule, severity maps and suppressions
    name = "event-needs-owner"
    description = "Every event must declare an owning team."
    default_severity = Severity.WARNING
    remediation = "Add `owners:` to the event."

    def evaluate(self, ctx):
        for event in ctx.catalog.sorted_events():
            if not event.owners:
                yield self.finding(
                    event.id, f"{event.qualified_name} has no owner", source=event.source
                )
```

```toml
[project.entry-points."eventlineage.rules"]
owner = "acme_rules:OwnerRule"
```

After `pip install`, the rule shows in `eventlineage rules` and runs in `validate`. An
entry point may also name a list of rules. A rule that fails to load, or whose id is
already taken, is skipped with a warning, so it can never replace a built-in.
Configuration cannot name a module to import: a repository being scanned must not be
able to run code.

From Python alone, `register_rule(OwnerRule)` does the same.

## Rules of thumb

- **Score honestly.** Return `0` when the file is not yours. A plugin that claims
  everything silently steals files from the built-in formats.
- **Never raise for a missing optional field.** Warn, emit what you can, and let
  the scan finish; one broken file should not fail a repository-wide scan.
- **No network, no clock, no randomness.** Scans must be reproducible; the cache
  and the deterministic output depend on it.
- **Attach `source` to everything you create.** It is what turns a finding into a
  file-and-line pointer in the report.
- **Do not build the catalog.** Return fragments; merging, deduplication and
  conflict resolution belong to the engine.
