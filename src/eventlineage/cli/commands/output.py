"""Output commands: graph, report, export."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from eventlineage.cli.common import EXIT_OK, EXIT_USAGE, emit, fail, normalize_format, run_guarded
from eventlineage.cli.options import ConfigOption, OutputOption, RootOption, resolve_state
from eventlineage.graph.builder import service_flow_graph
from eventlineage.graph.model import NodeKind
from eventlineage.models.common import DependencyKind
from eventlineage.reporting.dot import render_dot
from eventlineage.reporting.mermaid import render_mermaid

EDGE_CHOICES = [kind.value for kind in DependencyKind]
NODE_CHOICES = [kind.value for kind in NodeKind]


def _parse_choices(values: list[str] | None, allowed: list[str], option: str) -> list[str] | None:
    if not values:
        return None
    values = [part.strip() for value in values for part in value.split(",") if part.strip()]
    invalid = [value for value in values if value not in allowed]
    if invalid:
        fail(
            f"invalid {option}: {', '.join(invalid)}",
            code=EXIT_USAGE,
            hint=f"choose from: {', '.join(allowed)}",
        )
    return values


def graph(
    ctx: typer.Context,
    config: ConfigOption = None,
    root: RootOption = None,
    format: Annotated[str, typer.Option("--format", "-f", help="dot | mermaid | json")] = "mermaid",
    output: OutputOption = None,
    edge_kinds: Annotated[
        list[str] | None, typer.Option("--edge-kinds", help="Only include these edge kinds")
    ] = None,
    node_kinds: Annotated[
        list[str] | None, typer.Option("--node-kinds", help="Only include these node kinds")
    ] = None,
    max_nodes: Annotated[
        int | None, typer.Option("--max-nodes", help="Truncate large graphs")
    ] = None,
    title: Annotated[str | None, typer.Option("--title", help="Diagram title")] = None,
    direction: Annotated[
        str, typer.Option("--direction", help="Mermaid direction (LR, TD, ...)")
    ] = "LR",
    services_only: Annotated[
        bool, typer.Option("--services-only", help="Project the graph onto services")
    ] = False,
) -> None:
    """Render the dependency graph as DOT, Mermaid or JSON."""
    state = resolve_state(ctx, config, root)
    fmt = normalize_format(format, ("dot", "mermaid", "json"))
    allowed_edges = _parse_choices(edge_kinds, EDGE_CHOICES, "--edge-kinds")
    allowed_nodes = _parse_choices(node_kinds, NODE_CHOICES, "--node-kinds")

    dependency_graph = run_guarded(state.project.graph)
    if services_only:
        dependency_graph = service_flow_graph(dependency_graph)
    if allowed_nodes or max_nodes is not None:
        kept = [
            node.id
            for node in dependency_graph.nodes
            if not allowed_nodes or node.kind.value in allowed_nodes
        ]
        dependency_graph = dependency_graph.subgraph(kept[:max_nodes])
    if allowed_edges:
        dependency_graph = dependency_graph.filtered(allowed_edges)

    if fmt == "json":
        emit(
            json.dumps(dependency_graph.to_dict(), indent=2, sort_keys=True),
            output,
            console=state.console,
        )
    elif fmt == "dot":
        content = render_dot(
            dependency_graph,
            title=title or state.config.project_name,
        )
        emit(content, output, console=state.console)
    else:
        content = render_mermaid(
            dependency_graph,
            direction=direction,
        )
        emit(content, output, console=state.console)
    raise typer.Exit(EXIT_OK)


def report(
    ctx: typer.Context,
    config: ConfigOption = None,
    root: RootOption = None,
    format: Annotated[str, typer.Option("--format", "-f", help="markdown | json")] = "markdown",
    output: OutputOption = None,
    include_schemas: Annotated[
        bool, typer.Option("--include-schemas", help="Add the schema inventory")
    ] = False,
    diagrams: Annotated[
        bool, typer.Option("--diagrams/--no-diagrams", help="Include Mermaid diagrams")
    ] = True,
    impact_target: Annotated[
        list[str] | None,
        typer.Option("--impact", help="Add an impact section for this event/service"),
    ] = None,
    title: Annotated[str | None, typer.Option("--title", help="Report title")] = None,
    timestamp: Annotated[
        bool,
        typer.Option("--timestamp", help="Embed the generation timestamp (breaks reproducibility)"),
    ] = False,
) -> None:
    """Generate an architecture report (Markdown or JSON)."""
    state = resolve_state(ctx, config, root)
    fmt = normalize_format(format, ("markdown", "md", "json"))
    generated_at = None
    if timestamp:
        from datetime import UTC, datetime

        generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    content = run_guarded(
        lambda: state.project.report(
            format=fmt,
            include_schemas=include_schemas or None,
            include_diagrams=diagrams,
            title=title,
            generated_at=generated_at,
            impacts=impact_target or (),
        )
    )
    emit(content, output, console=state.console)
    raise typer.Exit(EXIT_OK)


def export(
    ctx: typer.Context,
    config: ConfigOption = None,
    root: RootOption = None,
    format: Annotated[
        str, typer.Option("--format", "-f", help="json | catalog | graph | dot | mermaid")
    ] = "json",
    output: OutputOption = None,
    include_schemas: Annotated[
        bool, typer.Option("--include-schemas", help="Include parsed schema fields")
    ] = False,
    max_nodes: Annotated[
        int | None, typer.Option("--max-nodes", help="Truncate graph exports")
    ] = None,
) -> None:
    """Export the catalog or graph for other tools."""
    state = resolve_state(ctx, config, root)
    fmt = normalize_format(format, ("json", "catalog", "graph", "dot", "mermaid"))

    if fmt == "catalog":
        content = run_guarded(
            lambda: json.dumps(
                state.project.catalog().to_dict(include_schemas=include_schemas),
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
        emit(content, output, console=state.console)
        raise typer.Exit(EXIT_OK)

    if fmt == "graph":
        content = run_guarded(
            lambda: json.dumps(state.project.graph().to_dict(), indent=2, sort_keys=True)
        )
        emit(content, output, console=state.console)
        raise typer.Exit(EXIT_OK)

    if fmt == "dot":
        content = run_guarded(
            lambda: render_dot(
                state.project.graph(), title=state.config.project_name, max_nodes=max_nodes
            )
        )
        emit(content, output, console=state.console)
        raise typer.Exit(EXIT_OK)

    if fmt == "mermaid":
        content = run_guarded(lambda: render_mermaid(state.project.graph(), max_nodes=max_nodes))
        emit(content, output, console=state.console)
        raise typer.Exit(EXIT_OK)

    content = run_guarded(
        lambda: state.project.report(format="json", include_schemas=include_schemas)
    )
    emit(content, output, console=state.console)
    raise typer.Exit(EXIT_OK)


__all__ = ["export", "graph", "report"]
