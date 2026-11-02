"""Rich-powered terminal rendering.

Rich is used *only* here and in the CLI: the engine itself never imports it, so
library users pay nothing for terminal formatting.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from eventlineage.analysis.impact import ImpactReport, RiskLevel
from eventlineage.analysis.summary import ArchitectureSummary
from eventlineage.compatibility.result import ChangeImpact, CompatibilityReport
from eventlineage.core.catalog import EventCatalog
from eventlineage.core.scanner import ScanResult
from eventlineage.models.common import Severity
from eventlineage.validation.engine import ValidationReport

SEVERITY_STYLE: dict[Severity, str] = {
    Severity.ERROR: "bold red",
    Severity.WARNING: "yellow",
    Severity.INFO: "cyan",
    Severity.OFF: "dim",
}

RISK_STYLE: dict[RiskLevel, str] = {
    RiskLevel.LOW: "green",
    RiskLevel.MEDIUM: "yellow",
    RiskLevel.HIGH: "dark_orange",
    RiskLevel.CRITICAL: "bold red",
}

IMPACT_STYLE: dict[ChangeImpact, str] = {
    ChangeImpact.COMPATIBLE: "green",
    ChangeImpact.POTENTIALLY_BREAKING: "yellow",
    ChangeImpact.BREAKING: "bold red",
}


def make_console(*, no_color: bool = False, quiet: bool = False) -> Console:
    """Build the shared console (``quiet`` suppresses informational output)."""
    return Console(
        no_color=no_color,
        stderr=False,
        highlight=False,
        soft_wrap=False,
        quiet=quiet,
    )


def print_scan_summary(console: Console, result: ScanResult, *, verbose: bool = False) -> None:
    """Print the result of a scan."""
    summary = result.summary
    table = Table(title=f"Scan: {summary.project}", title_justify="left", box=None)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Root", summary.root)
    table.add_row("Files considered", str(summary.files_considered))
    table.add_row("Files parsed / cached", f"{summary.files_parsed} / {summary.files_cached}")
    table.add_row("Files ignored", str(summary.files_ignored))
    table.add_row("Files failed", str(summary.files_failed))
    table.add_row("Events", str(summary.events))
    table.add_row("Services", str(summary.services))
    table.add_row("Topics", str(summary.topics))
    table.add_row(
        "Relationships", f"{summary.relationships} ({summary.inferred_relationships} inferred)"
    )
    table.add_row("Duration", f"{summary.duration_ms:.1f} ms")
    if summary.plugin_counts:
        table.add_row(
            "Plugins",
            ", ".join(f"{name}: {count}" for name, count in summary.plugin_counts.items()),
        )
    console.print(table)

    if verbose:
        files = Table(title="Files", box=None, title_justify="left")
        for column in ("Path", "Plugin", "Events", "Topics", "Cached"):
            files.add_column(column)
        for record in result.files:
            files.add_row(
                record.path,
                record.plugin,
                str(record.events),
                str(record.topics),
                "yes" if record.cached else "no",
            )
        console.print(files)

    for diagnostic in result.diagnostics:
        style = SEVERITY_STYLE.get(diagnostic.severity, "")
        console.print(Text(f"  {diagnostic}", style=style))


def print_event_table(
    console: Console,
    events: Iterable[Any],
    *,
    title: str = "Events",
    console_limit: int | None = None,
) -> None:
    """Print a table of events."""
    items = list(events)
    table = Table(title=f"{title} ({len(items)})", title_justify="left")
    table.add_column("Event", style="bold cyan", no_wrap=True)
    table.add_column("Version")
    table.add_column("Schema")
    table.add_column("Producers")
    table.add_column("Consumers")
    table.add_column("Topics", overflow="fold")
    for event in items[: console_limit or len(items)]:
        schema = event.active_schema()
        table.add_row(
            event.id,
            event.version,
            schema.format.value if schema else "—",
            ", ".join(event.producer_services) or "—",
            ", ".join(event.consumer_services) or "—",
            ", ".join(event.topics) or "—",
        )
    console.print(table)
    if console_limit and len(items) > console_limit:
        console.print(Text(f"  … {len(items) - console_limit} more", style="dim"))


def print_service_table(console: Console, catalog: EventCatalog) -> None:
    """Print a table of services and their event involvement."""
    services = catalog.sorted_services()
    table = Table(title=f"Services ({len(services)})", title_justify="left")
    table.add_column("Service", style="bold cyan", no_wrap=True)
    table.add_column("Kind")
    table.add_column("Tier")
    table.add_column("Produces", justify="right")
    table.add_column("Consumes", justify="right")
    table.add_column("Events", overflow="fold")
    index = catalog.service_event_index()
    for service in services:
        produced = index.produced_by(service.name)
        consumed = index.consumed_by(service.name)
        table.add_row(
            service.name,
            service.kind.value,
            service.tier or "—",
            str(len(produced)),
            str(len(consumed)),
            ", ".join(event.id for event in consumed[:6]) or "—",
        )
    console.print(table)


def print_topic_table(console: Console, catalog: EventCatalog) -> None:
    """Print a table of topics and their participants."""
    topics = catalog.sorted_topics()
    table = Table(title=f"Topics ({len(topics)})", title_justify="left")
    table.add_column("Topic", style="bold cyan")
    table.add_column("Broker")
    table.add_column("Partitions", justify="right")
    table.add_column("Producers", overflow="fold")
    table.add_column("Consumers", overflow="fold")
    table.add_column("Events", overflow="fold")
    for topic in topics:
        table.add_row(
            topic.name,
            topic.broker or "—",
            str(topic.partitions) if topic.partitions is not None else "—",
            ", ".join(topic.producers) or "—",
            ", ".join(topic.consumers) or "—",
            ", ".join(topic.events) or "—",
        )
    console.print(table)


def print_findings(
    console: Console, report: ValidationReport, *, show_remediation: bool = True
) -> None:
    """Print validation findings, grouped by severity."""
    if not report.findings:
        console.print(Panel("No findings.", title="Validation", border_style="green"))
    else:
        table = Table(title="Validation findings", title_justify="left", show_lines=False)
        # Five columns do not fit an 80-column terminal: the source column is the
        # one that degrades worst, so drop it when the console is narrow.
        compact = console.width < 100
        table.add_column("Severity", no_wrap=True)
        table.add_column("Rule", no_wrap=True)
        table.add_column("Subject", no_wrap=True, overflow="ellipsis")
        table.add_column("Finding", overflow="fold")
        if not compact:
            table.add_column("Source", overflow="fold")
        for finding in report.findings:
            table.add_row(
                Text(
                    finding.severity.value.upper(), style=SEVERITY_STYLE.get(finding.severity, "")
                ),
                finding.rule,
                finding.subject or "—",
                finding.message,
                *([] if compact else [str(finding.source) if finding.source else "—"]),
            )
        console.print(table)
        if show_remediation:
            seen: set[str] = set()
            for finding in report.findings:
                if not finding.remediation or finding.rule in seen:
                    continue
                seen.add(finding.rule)
                console.print(Text(f"  {finding.rule} → {finding.remediation}", style="dim"))
    style = "red" if report.failed else "green"
    console.print(
        Text(
            f"{report.summary()} (threshold: {report.fail_on.value})",
            style=style,
        )
    )
    if report.suppressed:
        console.print(
            Text(f"  {len(report.suppressed)} finding(s) suppressed by configuration", style="dim")
        )


def print_impact(console: Console, impact: ImpactReport) -> None:
    """Print an impact report."""
    colour = RISK_STYLE.get(impact.risk, "white")
    console.print(
        Panel(
            Text(impact.summary(), style=colour),
            title=f"Impact: {impact.target}",
            border_style=colour,
        )
    )
    if impact.affected_services:
        table = Table(title="Affected services", title_justify="left")
        table.add_column("Service", style="bold cyan")
        table.add_column("Depth", justify="right")
        table.add_column("Direct")
        table.add_column("Tier")
        table.add_column("Score", justify="right")
        table.add_column("Path", overflow="fold")
        for service in impact.affected_services:
            table.add_row(
                service.name,
                str(service.depth),
                "yes" if service.direct else "no",
                service.tier or "—",
                f"{service.score:.2f}",
                " → ".join(service.path) or "—",
            )
        console.print(table)
    if impact.affected_events:
        table = Table(title="Affected events", title_justify="left")
        table.add_column("Event", style="bold cyan")
        table.add_column("Depth", justify="right")
        table.add_column("Version")
        table.add_column("Schema")
        for event in impact.affected_events:
            table.add_row(event.id, str(event.depth), event.version, event.schema_format or "—")
        console.print(table)
    if impact.related_topics:
        console.print(Text("Related topics: " + ", ".join(impact.related_topics)))
    if impact.affected_schemas:
        console.print(
            Text(
                "Affected schemas: "
                + ", ".join(
                    f"{schema.event_id}@{schema.version}" for schema in impact.affected_schemas
                )
            )
        )
    for note in impact.notes:
        console.print(Text(f"  • {note}", style="dim"))


def print_summary(console: Console, summary: ArchitectureSummary) -> None:
    """Print an architecture summary."""
    colour = (
        "green" if summary.health_score >= 90 else "yellow" if summary.health_score >= 70 else "red"
    )
    console.print(
        Panel(
            Text(summary.summary(), style=f"bold {colour}"),
            title="Architecture",
            border_style=colour,
        )
    )
    stats = summary.stats
    table = Table(box=None, title_justify="left")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Events", str(stats.events))
    table.add_row("Services", str(stats.services))
    table.add_row("Topics", str(stats.topics))
    table.add_row("Event versions", str(stats.schema_versions))
    table.add_row("Graph nodes / edges", f"{summary.graph.nodes} / {summary.graph.edges}")
    table.add_row("Components", str(summary.graph.components))
    table.add_row("Acyclic", "yes" if summary.graph.acyclic else "no")
    table.add_row("Events without producer", str(stats.events_without_producer))
    table.add_row("Events without consumer", str(stats.events_without_consumer))
    table.add_row("Events without schema", str(stats.events_without_schema))
    table.add_row("Average consumers/event", str(stats.average_consumers_per_event))
    console.print(table)

    if summary.cycles:
        console.print(Text("Cycles", style="bold red"))
        for cycle in summary.cycles:
            console.print(Text(f"  [{cycle.kind}] {cycle.render()}", style="red"))
    if summary.hotspots:
        console.print(Text("Hotspots", style="bold"))
        for hotspot in summary.hotspots[:5]:
            console.print(
                Text(f"  {hotspot.name} ({hotspot.kind}): {hotspot.description}", style="dim")
            )
    if summary.longest_chains:
        console.print(Text("Longest chains", style="bold"))
        for chain in summary.longest_chains:
            console.print(Text("  " + " → ".join(chain), style="dim"))
    console.print(Text("Recommendations", style="bold"))
    for note in summary.recommendations:
        console.print(Text(f"  • {note}"))


def print_comparison(console: Console, report: CompatibilityReport, *, old: str, new: str) -> None:
    """Print a schema comparison."""
    style = "green" if report.compatible else "bold red"
    console.print(
        Panel(
            Text(f"{report.summary()}", style=style),
            title=f"{old} → {new} ({report.format.value}, {report.mode.value})",
            border_style=style,
        )
    )
    if not report.changes:
        return
    table = Table(title="Changes", title_justify="left")
    table.add_column("Field")
    table.add_column("Change")
    table.add_column("Impact")
    table.add_column("Detail", overflow="fold")
    for change in report.changes:
        table.add_row(
            change.path or "(root)",
            change.change.value,
            Text(change.impact.value, style=IMPACT_STYLE.get(change.impact, "")),
            change.description,
        )
    console.print(table)


def print_kv_table(
    console: Console,
    title: str,
    rows: Sequence[tuple[str, str]],
    *,
    columns: Sequence[str] = ("Name", "Value"),
) -> None:
    """Print a two-column metric table."""
    table = Table(title=title, title_justify="left")
    table.add_column(columns[0], style="bold cyan")
    for column in columns[1:]:
        table.add_column(column)
    for row in rows:
        table.add_row(*row)
    console.print(table)


def print_plugin_table(console: Console, rows: Iterable[dict[str, Any]]) -> None:
    """Print the discovery plugins that are registered."""
    table = Table(title="Discovery plugins", title_justify="left")
    table.add_column("Plugin", style="bold cyan")
    table.add_column("Patterns")
    table.add_column("Priority", justify="right")
    table.add_column("Description", overflow="fold")
    for row in rows:
        table.add_row(
            str(row.get("name")),
            ", ".join(row.get("patterns", ())),
            str(row.get("priority")),
            str(row.get("description") or ""),
        )
    console.print(table)


__all__ = [
    "IMPACT_STYLE",
    "RISK_STYLE",
    "SEVERITY_STYLE",
    "make_console",
    "print_comparison",
    "print_event_table",
    "print_findings",
    "print_impact",
    "print_kv_table",
    "print_plugin_table",
    "print_scan_summary",
    "print_service_table",
    "print_summary",
    "print_topic_table",
]
