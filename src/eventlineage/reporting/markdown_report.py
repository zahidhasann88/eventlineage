"""Markdown architecture reports.

The report is designed to be committed to a repository and reviewed in a pull
request: stable ordering, no volatile metadata unless a timestamp is passed
explicitly, tables that stay readable, and a Mermaid diagram that renders on
GitHub and GitLab without any extra tooling.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from eventlineage.analysis.impact import ImpactReport
from eventlineage.analysis.summary import ArchitectureSummary
from eventlineage.compatibility.result import EventVersionComparison
from eventlineage.core.catalog import EventCatalog
from eventlineage.graph.builder import build_graph
from eventlineage.models.common import Severity
from eventlineage.models.event import Event
from eventlineage.reporting.mermaid import render_mermaid
from eventlineage.validation.engine import ValidationReport


def _escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _schema_format(event: Event) -> str:
    """Schema dialect of an event's active version, or an em dash."""
    active = event.active_schema()
    return active.format.value if active is not None else "—"


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    rows = list(rows)
    if not rows:
        return "_None._\n"
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_escape(cell) for cell in row) + " |")
    return "\n".join(lines) + "\n"


def _code_list(values: Iterable[str], empty: str = "—") -> str:
    items = [f"`{value}`" for value in values]
    return ", ".join(items) if items else empty


def render_markdown_report(
    catalog: EventCatalog,
    *,
    summary: ArchitectureSummary | None = None,
    validation: ValidationReport | None = None,
    impacts: Sequence[ImpactReport] = (),
    comparisons: Sequence[EventVersionComparison] = (),
    include_schemas: bool = False,
    include_diagrams: bool = True,
    title: str | None = None,
    generated_at: str | None = None,
    max_events: int | None = None,
    max_findings: int = 200,
    max_impacted_nodes: int = 200,
) -> str:
    """Render the architecture report as Markdown."""
    stats = summary.stats if summary else catalog.stats()
    heading = title or f"{catalog.project_name} — event architecture"
    lines: list[str] = [f"# {heading}", ""]

    if generated_at:
        lines.extend([f"_Generated {generated_at} by EventLineage._", ""])

    lines.extend(
        [
            "## Overview",
            "",
            _table(
                ["Metric", "Value"],
                [
                    ("Events", stats.events),
                    ("Services", stats.services),
                    ("Topics", stats.topics),
                    ("Producers", stats.producers),
                    ("Consumers", stats.consumers),
                    ("Event versions", stats.schema_versions),
                    ("Known relationships", stats.relationships),
                    (
                        "Schema dialects",
                        ", ".join(f"{k}: {v}" for k, v in stats.schema_formats.items()) or "—",
                    ),
                ],
            ),
        ]
    )

    if summary is not None:
        lines.extend(
            [
                "## Architecture health",
                "",
                f"**Score:** {summary.health_score:.0f}/100 (grade **{summary.health_grade}**)\n",
                _table(
                    ["Signal", "Count"],
                    [
                        ("Events without a producer", stats.events_without_producer),
                        ("Events without a consumer", stats.events_without_consumer),
                        ("Events without a schema", stats.events_without_schema),
                        ("Isolated events", stats.isolated_events),
                        ("Orphan topics", stats.orphan_topics),
                        ("Graph nodes", summary.graph.nodes),
                        ("Graph edges", summary.graph.edges),
                        ("Connected components", summary.graph.components),
                        ("Dependency cycles", len(summary.cycles)),
                    ],
                ),
                "### Recommendations",
                "",
            ]
        )
        lines.extend(f"- {note}" for note in summary.recommendations)
        lines.append("")

    lines.extend(["## Events", ""])
    events = catalog.sorted_events()
    shown = events[:max_events] if max_events else events
    lines.append(
        _table(
            ["Event", "Version", "Schema", "Producers", "Consumers", "Topics"],
            [
                (
                    f"`{event.id}`",
                    event.version,
                    _schema_format(event),
                    _code_list(event.producer_services),
                    _code_list(event.consumer_services),
                    _code_list(event.topics),
                )
                for event in shown
            ],
        )
    )
    if max_events and len(events) > len(shown):
        lines.append(f"_… {len(events) - len(shown)} more events omitted._\n")

    lines.extend(["## Services", ""])
    service_rows = []
    index = catalog.service_event_index()
    for service in catalog.sorted_services():
        produced = index.produced_by(service.name)
        consumed = index.consumed_by(service.name)
        service_rows.append(
            (
                f"`{service.name}`",
                service.kind.value,
                service.tier or "—",
                len(produced),
                len(consumed),
                _code_list(event.id for event in consumed[:4]),
            )
        )
    lines.append(
        _table(
            ["Service", "Kind", "Tier", "Produces", "Consumes", "Top consumers of"],
            service_rows,
        )
    )

    if catalog.topics:
        lines.extend(["## Topics", ""])
        lines.append(
            _table(
                ["Topic", "Broker", "Partitions", "Producers", "Consumers", "Events"],
                [
                    (
                        f"`{topic.name}`",
                        topic.broker or "—",
                        topic.partitions if topic.partitions is not None else "—",
                        _code_list(topic.producers),
                        _code_list(topic.consumers),
                        _code_list(topic.events),
                    )
                    for topic in catalog.sorted_topics()
                ],
            )
        )

    if summary is not None and summary.cycles:
        lines.extend(["## Dependency cycles", ""])
        lines.append(
            _table(
                ["Level", "Cycle"],
                [(cycle.kind, f"`{cycle.render()}`") for cycle in summary.cycles],
            )
        )

    if summary is not None and summary.hotspots:
        lines.extend(["## Hotspots", ""])
        lines.append(
            _table(
                ["Node", "Kind", "Links", "Why it matters"],
                [
                    (hotspot.name, hotspot.kind, hotspot.value, hotspot.description)
                    for hotspot in summary.hotspots
                ],
            )
        )

    if include_diagrams:
        graph = build_graph(catalog)
        if graph.node_count:
            lines.extend(
                [
                    "## Dependency graph",
                    "",
                    "```mermaid",
                    render_mermaid(graph, direction="LR"),
                    "```",
                    "",
                ]
            )

    if validation is not None:
        lines.extend(["## Validation", ""])
        lines.append(f"_{validation.summary()}_\n")
        for severity in (Severity.ERROR, Severity.WARNING, Severity.INFO):
            findings = [finding for finding in validation.findings if finding.severity is severity]
            if not findings:
                continue
            lines.extend([f"### {severity.value.title()} ({len(findings)})", ""])
            lines.append(
                _table(
                    ["Rule", "Subject", "Finding", "Source", "Remediation"],
                    [
                        (
                            finding.rule,
                            finding.subject or "—",
                            finding.message,
                            str(finding.source) if finding.source else "—",
                            finding.remediation or "—",
                        )
                        for finding in findings[:max_findings]
                    ],
                )
            )
            if len(findings) > max_findings:
                lines.append(f"_… {len(findings) - max_findings} more findings omitted._\n")

    if impacts:
        lines.extend(["## Impact analysis", ""])
        for impact in impacts:
            lines.extend([f"### `{impact.target}`", "", impact.summary(), ""])
            lines.append(
                _table(
                    ["Service", "Depth", "Direct", "Tier", "Score"],
                    [
                        (
                            service.name,
                            service.depth,
                            "yes" if service.direct else "no",
                            service.tier or "—",
                            f"{service.score:.2f}",
                        )
                        for service in impact.affected_services[:max_impacted_nodes]
                    ],
                )
            )
            hidden = len(impact.affected_services) - max_impacted_nodes
            if hidden > 0:
                lines.append(f"_… {hidden} more services omitted._\n")
            if impact.related_topics:
                lines.extend([f"Related topics: {_code_list(impact.related_topics)}", ""])
            if impact.notes:
                lines.extend(f"- {note}" for note in impact.notes)
                lines.append("")

    if comparisons:
        lines.extend(["## Schema compatibility", ""])
        rows: list[tuple[Any, ...]] = []
        for comparison in comparisons:
            report = comparison.report
            rows.append(
                (
                    f"`{comparison.event_id}`",
                    f"{comparison.old_version} → {comparison.new_version}",
                    report.mode.value,
                    "yes" if report.compatible else "**no**",
                    report.counts()["breaking"],
                    report.counts()["potentially_breaking"],
                )
            )
        lines.append(
            _table(
                ["Event", "Versions", "Mode", "Compatible", "Breaking", "Potential"],
                rows,
            )
        )
        for comparison in comparisons:
            breaking = comparison.report.breaking_changes
            if not breaking:
                continue
            lines.extend(
                [
                    f"#### `{comparison.event_id}` "
                    f"{comparison.old_version} → {comparison.new_version}",
                    "",
                ]
            )
            lines.append(
                _table(
                    ["Field", "Change", "Impact", "Detail"],
                    [
                        (
                            change.path or "(root)",
                            change.change.value,
                            change.impact.value,
                            change.description,
                        )
                        for change in comparison.report.changes
                    ],
                )
            )

    if include_schemas:
        lines.extend(["## Schema inventory", ""])
        inventory: list[tuple[Any, ...]] = []
        for event in catalog.sorted_events():
            schema = event.active_schema()
            if schema is None:
                continue
            inventory.append(
                (
                    f"`{event.id}`",
                    schema.format.value,
                    schema.content_hash,
                    str(len(schema.parsed().fields)),
                    str(schema.source) if schema.source else "—",
                )
            )
        lines.append(_table(["Event", "Format", "Hash", "Root fields", "Source"], inventory))

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("Generated by [EventLineage](https://github.com/zahidhasann88/eventlineage).")
    lines.append("")
    return "\n".join(lines)


__all__ = ["render_markdown_report"]
