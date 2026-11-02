"""Inventory commands: events, services, topics."""

from __future__ import annotations

import fnmatch
import json
from typing import Annotated

import typer

from eventlineage.cli.common import EXIT_OK, emit, normalize_format, run_guarded
from eventlineage.cli.options import ConfigOption, OutputOption, RootOption, resolve_state
from eventlineage.reporting.console import (
    print_event_table,
    print_service_table,
    print_topic_table,
)


def events(
    ctx: typer.Context,
    query: Annotated[
        str | None, typer.Argument(help="Substring or glob matched against event ids")
    ] = None,
    config: ConfigOption = None,
    root: RootOption = None,
    namespace: Annotated[
        str | None, typer.Option("--namespace", "-n", help="Filter by namespace")
    ] = None,
    service: Annotated[
        str | None, typer.Option("--service", "-s", help="Only events this service uses")
    ] = None,
    tag: Annotated[str | None, typer.Option("--tag", help="Filter by tag")] = None,
    orphaned: Annotated[
        bool, typer.Option("--orphaned", help="Only events without a producer")
    ] = False,
    unconsumed: Annotated[
        bool, typer.Option("--unconsumed", help="Only events without a consumer")
    ] = False,
    without_schema: Annotated[
        bool, typer.Option("--no-schema", help="Only events without a schema")
    ] = False,
    limit: Annotated[
        int | None, typer.Option("--limit", "-l", help="Maximum rows to print")
    ] = None,
    format: Annotated[
        str, typer.Option("--format", "-f", help="table | json | markdown")
    ] = "table",
    output: OutputOption = None,
) -> None:
    """List discovered events, optionally filtered."""
    state = resolve_state(ctx, config, root)
    fmt = normalize_format(format, ("table", "json", "markdown"))
    catalog = run_guarded(state.project.catalog)
    selected = catalog.sorted_events()

    if query:
        needle = query.lower()
        # A bare word is a substring; a pattern matches like a shell glob.
        pattern = needle if any(character in needle for character in "*?[") else f"*{needle}*"
        selected = tuple(
            event
            for event in selected
            if any(
                fnmatch.fnmatchcase(candidate.lower(), pattern)
                for candidate in (event.id, event.qualified_name, event.name)
            )
        )
    if namespace:
        selected = tuple(event for event in selected if event.namespace == namespace)
    if service:
        selected = tuple(
            event
            for event in selected
            if service in event.producer_services or service in event.consumer_services
        )
    if tag:
        selected = tuple(event for event in selected if tag in event.tags)
    if orphaned:
        selected = tuple(event for event in selected if event.is_orphaned)
    if unconsumed:
        selected = tuple(event for event in selected if event.is_unconsumed)
    if without_schema:
        selected = tuple(event for event in selected if not event.has_schema())

    if fmt == "json":
        payload = json.dumps(
            [event.summary() for event in selected], indent=2, sort_keys=True, default=str
        )
        emit(payload, output, console=state.console)
    elif fmt == "markdown":
        lines = [
            "| Event | Version | Schema | Producers | Consumers |",
            "| --- | --- | --- | --- | --- |",
        ]
        for event in selected[: limit or len(selected)]:
            schema = event.active_schema()
            lines.append(
                f"| `{event.id}` | {event.version} | {schema.format.value if schema else '—'} | "
                f"{', '.join(event.producer_services) or '—'} | "
                f"{', '.join(event.consumer_services) or '—'} |"
            )
        emit("\n".join(lines), output, console=state.console)
    else:
        print_event_table(state.console, selected, console_limit=limit)
    raise typer.Exit(EXIT_OK)


def services(
    ctx: typer.Context,
    query: Annotated[
        str | None, typer.Argument(help="Substring matched against service names")
    ] = None,
    config: ConfigOption = None,
    root: RootOption = None,
    tier: Annotated[str | None, typer.Option("--tier", help="Filter by criticality tier")] = None,
    format: Annotated[str, typer.Option("--format", "-f", help="table | json")] = "table",
    output: OutputOption = None,
) -> None:
    """List services and their event involvement."""
    state = resolve_state(ctx, config, root)
    fmt = normalize_format(format, ("table", "json"))
    catalog = run_guarded(state.project.catalog)
    selected = catalog.sorted_services()
    if query:
        needle = query.lower()
        selected = tuple(service for service in selected if needle in service.name.lower())
    if tier:
        selected = tuple(service for service in selected if (service.tier or "") == tier)

    if fmt == "json":
        index = catalog.service_event_index()
        rows = [
            {
                "name": service.name,
                "kind": service.kind.value,
                "tier": service.tier,
                "produces": [event.id for event in index.produced_by(service.name)],
                "consumes": [event.id for event in index.consumed_by(service.name)],
            }
            for service in selected
        ]
        emit(json.dumps(rows, indent=2, sort_keys=True), output, console=state.console)
    elif query or tier:
        from eventlineage.core.catalog import EventCatalog

        subset = EventCatalog(project_name=catalog.project_name, root=catalog.root)
        subset.services = {service.name: service for service in selected}
        subset.events = catalog.events
        subset.topics = catalog.topics
        print_service_table(state.console, subset)
    else:
        print_service_table(state.console, catalog)
    raise typer.Exit(EXIT_OK)


def topics(
    ctx: typer.Context,
    config: ConfigOption = None,
    root: RootOption = None,
    format: Annotated[str, typer.Option("--format", "-f", help="table | json")] = "table",
    output: OutputOption = None,
) -> None:
    """List topics and how they connect services and events."""
    state = resolve_state(ctx, config, root)
    fmt = normalize_format(format, ("table", "json"))
    catalog = run_guarded(state.project.catalog)
    if fmt == "json":
        rows = [
            {
                "name": topic.name,
                "broker": topic.broker,
                "partitions": topic.partitions,
                "producers": list(topic.producers),
                "consumers": list(topic.consumers),
                "events": list(topic.events),
            }
            for topic in catalog.sorted_topics()
        ]
        emit(json.dumps(rows, indent=2, sort_keys=True), output, console=state.console)
    else:
        print_topic_table(state.console, catalog)
    raise typer.Exit(EXIT_OK)


__all__ = ["events", "services", "topics"]
