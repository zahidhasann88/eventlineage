"""Project lifecycle commands: init, scan, plugins, rules, integrations, cache."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from eventlineage.cli.common import (
    EXIT_ERROR,
    EXIT_FINDINGS,
    EXIT_OK,
    EXIT_USAGE,
    emit,
    fail,
    normalize_format,
    run_guarded,
)
from eventlineage.cli.options import ConfigOption, OutputOption, RootOption, resolve_state
from eventlineage.discovery.registry import default_registry
from eventlineage.integrations import INTEGRATIONS, available_integrations
from eventlineage.reporting.console import print_plugin_table, print_scan_summary
from eventlineage.utils.cache import ScanCache
from eventlineage.validation.engine import rule_catalog


def init(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="Directory to initialise")] = Path(),
    name: Annotated[str | None, typer.Option("--name", help="Project name")] = None,
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Overwrite an existing config")
    ] = False,
    minimal: Annotated[bool, typer.Option("--minimal", help="Write a minimal config")] = False,
) -> None:
    """Create an eventlineage.yaml project configuration."""
    state = resolve_state(ctx)
    target = path / "eventlineage.yaml"
    if target.exists() and not force:
        fail(
            f"{target} already exists",
            code=EXIT_USAGE,
            hint="pass --force to overwrite it",
        )
    try:
        from eventlineage.project import Project

        created = Project.create(path, name=name, force=force, minimal=minimal)
    except OSError as exc:
        fail(f"could not write {target}: {exc}", code=EXIT_ERROR)
    state.console.print(f"[green]created[/green] {created}")
    state.console.print(
        "[dim]next: run [bold]eventlineage scan[/bold] to discover events, then "
        "[bold]eventlineage validate[/bold][/dim]"
    )
    raise typer.Exit(EXIT_OK)


def scan(
    ctx: typer.Context,
    paths: Annotated[list[Path] | None, typer.Argument(help="Files or directories to scan")] = None,
    config: ConfigOption = None,
    root: RootOption = None,
    format: Annotated[str, typer.Option("--format", "-f", help="table | json")] = "table",
    output: OutputOption = None,
    full: Annotated[bool, typer.Option("--full", help="Ignore the incremental cache")] = False,
    verbose: Annotated[int, typer.Option("--verbose", "-v", count=True)] = 0,
) -> None:
    """Discover events, services and topics from the project."""
    state = resolve_state(ctx, config, root, verbose=verbose)
    fmt = normalize_format(format, ("table", "json"))
    project = state.project
    if not project.root.exists():
        fail(f"scan root does not exist: {project.root}", code=EXIT_USAGE)
    result = run_guarded(lambda: project.scan(paths or None, incremental=not full))
    if result.summary.files_considered == 0:
        fail(
            f"no event sources found under {project.root}",
            code=EXIT_USAGE,
            hint="run 'eventlineage init' to create a configuration, or pass a path to scan",
        )
    if fmt == "json":
        document = {
            "summary": result.summary.model_dump(mode="json"),
            "files": [record.model_dump(mode="json") for record in result.files],
            "diagnostics": [item.model_dump(mode="json") for item in result.diagnostics],
            "catalog": result.catalog.to_dict(include_schemas=False),
        }
        emit(
            json.dumps(document, indent=2, sort_keys=True, default=str),
            output,
            console=state.console,
        )
    else:
        print_scan_summary(state.console, result, verbose=state.verbose > 0)
    failed = any(item.severity.value == "error" for item in result.diagnostics)
    raise typer.Exit(EXIT_FINDINGS if failed else EXIT_OK)


def plugins(ctx: typer.Context) -> None:
    """List installed discovery plugins."""
    state = resolve_state(ctx)
    registry = default_registry()
    registry.load_entry_points()
    rows = []
    for name in registry.names():
        plugin = registry.get(name)
        rows.append(
            {
                "name": name,
                "patterns": plugin.patterns,
                "priority": plugin.priority,
                "description": plugin.description,
            }
        )
    print_plugin_table(state.console, rows)


def rules(
    ctx: typer.Context,
    format: Annotated[str, typer.Option("--format", "-f", help="table | json")] = "table",
) -> None:
    """List validation rules and their default severities."""
    state = resolve_state(ctx)
    fmt = normalize_format(format, ("table", "json"))
    entries = rule_catalog()
    if fmt == "json":
        emit(json.dumps(list(entries), indent=2, sort_keys=True), None, console=state.console)
        return
    from rich.table import Table

    table = Table(title=f"Validation rules ({len(entries)})", title_justify="left")
    table.add_column("Id", style="bold cyan", no_wrap=True)
    table.add_column("Name", no_wrap=True)
    table.add_column("Severity")
    table.add_column("Description", overflow="fold")
    for entry in entries:
        table.add_row(entry["id"], entry["name"], entry["severity"], entry["description"])
    state.console.print(table)


def integrations(
    ctx: typer.Context,
    format: Annotated[str, typer.Option("--format", "-f", help="table | json")] = "table",
) -> None:
    """Show optional infrastructure integrations and whether they are usable."""
    state = resolve_state(ctx)
    fmt = normalize_format(format, ("table", "json"))
    statuses = available_integrations()
    if fmt == "json":
        emit(
            json.dumps(
                [status.model_dump(mode="json") for status in statuses], indent=2, sort_keys=True
            ),
            None,
            console=state.console,
        )
        return
    from rich.table import Table

    table = Table(title="Integrations", title_justify="left")
    table.add_column("Name", style="bold cyan")
    table.add_column("Available")
    table.add_column("Requires")
    table.add_column("Description", overflow="fold")
    for status in statuses:
        table.add_row(
            status.name,
            "[green]yes[/green]" if status.available else f"[yellow]no[/yellow] ({status.reason})",
            status.requires or "—",
            status.description or "",
        )
    state.console.print(table)
    state.console.print(
        "[dim]EventLineage works fully offline; integrations are optional adapters "
        "for live infrastructure.[/dim]"
    )


def pull(
    ctx: typer.Context,
    integration: Annotated[str, typer.Argument(help="kafka, schema-registry, rabbitmq or nats")],
    config: ConfigOption = None,
    root: RootOption = None,
    output: OutputOption = None,
) -> None:
    """Fetch from live infrastructure into a file that `eventlineage scan` reads offline."""
    state = resolve_state(ctx, config, root)
    adapter_class = INTEGRATIONS.get(integration)
    if adapter_class is None:
        fail(
            f"unknown integration {integration!r}",
            code=EXIT_USAGE,
            hint=f"choose one of: {', '.join(sorted(INTEGRATIONS))}",
        )
    settings = getattr(state.config.integrations, integration.replace("-", "_"))

    def collect() -> str:
        adapter = adapter_class.from_settings(settings)
        return json.dumps(adapter.export(adapter.collect()), indent=2, sort_keys=True)

    emit(run_guarded(collect), output, console=state.console)
    raise typer.Exit(EXIT_OK)


cache_app = typer.Typer(help="Inspect or clear the incremental scan cache.", no_args_is_help=True)


@cache_app.command("info")
def cache_info(ctx: typer.Context, config: ConfigOption = None, root: RootOption = None) -> None:
    """Show scan cache statistics."""
    state = resolve_state(ctx, config, root)
    path = (state.config.root / state.config.cache.directory / "scan.json").resolve()
    if not path.is_file():
        state.console.print(f"[dim]no cache at {path}[/dim]")
        return
    cache = ScanCache.load(path, state.config.fingerprint())
    stats = cache.stats()
    state.console.print(
        f"cache: {path}\nentries: {stats['entries']}\nsize: {stats['size_bytes']} bytes\n"
        f"matches current config: {'yes' if cache.entries else 'no'}"
    )


@cache_app.command("clear")
def cache_clear(ctx: typer.Context, config: ConfigOption = None, root: RootOption = None) -> None:
    """Delete the scan cache."""
    state = resolve_state(ctx, config, root)
    path = (state.config.root / state.config.cache.directory / "scan.json").resolve()
    if path.is_file():
        path.unlink()
        state.console.print(f"[green]removed[/green] {path}")
    else:
        state.console.print(f"[dim]nothing to remove ({path})[/dim]")


__all__ = ["cache_app", "init", "integrations", "plugins", "pull", "rules", "scan"]
