"""Analysis commands: analyze, validate, impact, compare."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console
from rich.text import Text

from eventlineage.cli.common import (
    EXIT_FINDINGS,
    EXIT_OK,
    EXIT_USAGE,
    emit,
    fail,
    normalize_format,
    run_guarded,
)
from eventlineage.cli.options import ConfigOption, OutputOption, RootOption, resolve_state
from eventlineage.compatibility.engine import compare_schemas
from eventlineage.compatibility.result import CompatibilityMode
from eventlineage.exceptions import SymbolNotFoundError
from eventlineage.models.common import Severity
from eventlineage.reporting.console import (
    print_comparison,
    print_findings,
    print_impact,
    print_summary,
)
from eventlineage.utils.schema_io import load_schema_document
from eventlineage.validation.engine import Validator, available_rules, get_rule

RISK_ORDER = ("low", "medium", "high", "critical")


def analyze(
    ctx: typer.Context,
    config: ConfigOption = None,
    root: RootOption = None,
    format: Annotated[
        str, typer.Option("--format", "-f", help="table | json | markdown")
    ] = "table",
    output: OutputOption = None,
) -> None:
    """Summarise the architecture: health, hotspots, cycles, recommendations."""
    state = resolve_state(ctx, config, root)
    fmt = normalize_format(format, ("table", "json", "markdown"))
    summary = run_guarded(state.project.analyze)
    if fmt == "json":
        emit(
            json.dumps(summary.to_dict(), indent=2, sort_keys=True, default=str),
            output,
            console=state.console,
        )
    elif fmt == "markdown":
        emit(
            state.project.report(format="markdown", include_diagrams=True),
            output,
            console=state.console,
        )
    else:
        print_summary(state.console, summary)
    raise typer.Exit(EXIT_OK)


def validate(
    ctx: typer.Context,
    config: ConfigOption = None,
    root: RootOption = None,
    fail_on: Annotated[
        str | None,
        typer.Option("--fail-on", help="Exit 1 when a finding at or above this severity exists"),
    ] = None,
    rule: Annotated[
        list[str] | None, typer.Option("--rule", help="Run only these rules (id or name)")
    ] = None,
    disable: Annotated[list[str] | None, typer.Option("--disable", help="Skip these rules")] = None,
    severity_override: Annotated[
        list[str] | None,
        typer.Option("--severity", help="Override severity: RULE=error|warning|info|off"),
    ] = None,
    format: Annotated[
        str, typer.Option("--format", "-f", help="table | json | markdown")
    ] = "table",
    output: OutputOption = None,
) -> None:
    """Run validation rules and report findings."""
    state = resolve_state(ctx, config, root)
    fmt = normalize_format(format, ("table", "json", "markdown"))

    overrides: dict[str, Severity] = {}
    for entry in severity_override or []:
        if "=" not in entry:
            fail(
                f"invalid --severity value {entry!r}",
                code=EXIT_USAGE,
                hint="use RULE=SEVERITY, e.g. --severity EG002=warning",
            )
        key, _, value = entry.partition("=")
        try:
            overrides[key.strip()] = Severity.coerce(value.strip())
        except ValueError:
            fail(f"unknown severity {value!r}", code=EXIT_USAGE, hint="error, warning, info or off")

    effective_fail_on: Severity | None = None
    if fail_on is not None:
        try:
            effective_fail_on = Severity.coerce(fail_on)
        except ValueError:
            fail(f"unknown --fail-on value {fail_on!r}", code=EXIT_USAGE)

    config_obj = state.config
    rule_classes = None
    if rule:
        selected = []
        for reference in rule:
            try:
                selected.append(get_rule(reference))
            except SymbolNotFoundError:
                fail(
                    f"unknown rule {reference!r}",
                    code=EXIT_USAGE,
                    hint="run 'eventlineage rules' to list available rules",
                )
        rule_classes = selected
    if disable:
        disabled = {reference.lower() for reference in disable}
        rule_classes = [
            candidate
            for candidate in (rule_classes or list(available_rules()))
            if candidate.id.lower() not in disabled and candidate.name.lower() not in disabled
        ]

    config_obj = config_obj.with_validation_overrides(
        fail_on=effective_fail_on,
        severity=overrides or None,
    )
    catalog = run_guarded(state.project.catalog)
    report = run_guarded(
        lambda: Validator(config_obj, rule_classes).validate(catalog, config=config_obj)
    )
    for diagnostic in catalog.diagnostics:
        if diagnostic.severity is Severity.ERROR:
            Console(stderr=True, highlight=False).print(Text(str(diagnostic), style="red"))

    if fmt == "json":
        emit(
            json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str),
            output,
            console=state.console,
        )
    elif fmt == "markdown":
        lines = [f"# Validation — {catalog.project_name}", "", f"_{report.summary()}_", ""]
        lines.append("| Severity | Rule | Subject | Finding | Source |")
        lines.append("| --- | --- | --- | --- | --- |")
        for finding in report.findings:
            lines.append(
                f"| {finding.severity.value} | {finding.rule} | {finding.subject or '—'} | "
                f"{finding.message.replace('|', '/')} | {finding.source or '—'} |"
            )
        emit("\n".join(lines), output, console=state.console)
    else:
        print_findings(state.console, report)
        for error in report.errors:
            state.console.print(f"[red]rule error:[/red] {error}")

    raise typer.Exit(report.exit_code())


def impact(
    ctx: typer.Context,
    target: Annotated[str, typer.Argument(help="Event id, service name or topic")],
    config: ConfigOption = None,
    root: RootOption = None,
    depth: Annotated[
        int | None, typer.Option("--depth", "-d", help="Limit traversal depth")
    ] = None,
    format: Annotated[
        str, typer.Option("--format", "-f", help="table | json | markdown")
    ] = "table",
    output: OutputOption = None,
    fail_on_risk: Annotated[
        str | None,
        typer.Option(
            "--fail-on-risk", help="Exit 1 when risk is at or above low|medium|high|critical"
        ),
    ] = None,
) -> None:
    """Calculate the blast radius of changing an event or service."""
    state = resolve_state(ctx, config, root)
    fmt = normalize_format(format, ("table", "json", "markdown"))
    if fail_on_risk is not None and fail_on_risk.lower() not in RISK_ORDER:
        fail(
            f"unknown risk level {fail_on_risk!r}",
            code=EXIT_USAGE,
            hint=f"one of {', '.join(RISK_ORDER)}",
        )
    report = run_guarded(lambda: state.project.impact(target, max_depth=depth))

    if fmt == "json":
        emit(
            json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str),
            output,
            console=state.console,
        )
    elif fmt == "markdown":
        lines = [f"# Impact — `{report.target}`", "", report.summary(), ""]
        lines.append("| Service | Depth | Direct | Tier | Score |")
        lines.append("| --- | --- | --- | --- | --- |")
        for service in report.affected_services:
            lines.append(
                f"| {service.name} | {service.depth} | {'yes' if service.direct else 'no'} | "
                f"{service.tier or '—'} | {service.score:.2f} |"
            )
        if report.related_topics:
            lines.extend(["", "Related topics: " + ", ".join(report.related_topics)])
        emit("\n".join(lines), output, console=state.console)
    else:
        print_impact(state.console, report)

    if fail_on_risk is not None and report.risk.rank >= RISK_ORDER.index(fail_on_risk.lower()):
        raise typer.Exit(EXIT_FINDINGS)
    raise typer.Exit(EXIT_OK)


def compare(
    ctx: typer.Context,
    old: Annotated[str, typer.Argument(help="Old schema file, or old event version with --event")],
    new: Annotated[str, typer.Argument(help="New schema file, or new event version with --event")],
    config: ConfigOption = None,
    root: RootOption = None,
    event: Annotated[
        str | None, typer.Option("--event", "-e", help="Event id: compare two versions of it")
    ] = None,
    mode: Annotated[
        str | None, typer.Option("--mode", "-m", help="backward | forward | full")
    ] = None,
    schema_format: Annotated[
        str | None,
        typer.Option(
            "--schema-format", help="Force the schema dialect (avro, protobuf, json-schema)"
        ),
    ] = None,
    format: Annotated[
        str, typer.Option("--format", "-f", help="table | json | markdown")
    ] = "table",
    output: OutputOption = None,
    no_fail: Annotated[
        bool, typer.Option("--no-fail", help="Always exit 0, even for breaking changes")
    ] = False,
) -> None:
    """Compare two schema versions and classify every difference."""
    state = resolve_state(ctx, config, root)
    fmt = normalize_format(format, ("table", "json", "markdown"))
    try:
        compatibility_mode = (
            CompatibilityMode.coerce(mode) if mode else state.config.compatibility.mode
        )
    except ValueError:
        fail(
            f"unknown compatibility mode {mode!r}",
            code=EXIT_USAGE,
            hint="backward, forward or full",
        )

    if event is not None:
        comparison = run_guarded(
            lambda: state.project.compare_versions(event, old, new, mode=compatibility_mode)
        )
        report = comparison.report
        old_label, new_label = f"{event}@{old}", f"{event}@{new}"
    else:
        previous = run_guarded(
            lambda: load_schema_document(old, format=schema_format),
        )
        current = run_guarded(
            lambda: load_schema_document(new, format=schema_format),
        )
        report = run_guarded(
            lambda: compare_schemas(previous, current, mode=compatibility_mode),
        )
        old_label, new_label = old, new

    if fmt == "json":
        emit(
            json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str),
            output,
            console=state.console,
        )
    elif fmt == "markdown":
        lines = [f"# Schema comparison — `{old_label}` → `{new_label}`", "", report.summary(), ""]
        if report.changes:
            lines.append("| Path | Change | Impact | Detail |")
            lines.append("| --- | --- | --- | --- |")
            for change in report.changes:
                impact = change.impact.value if hasattr(change.impact, "value") else change.impact
                lines.append(
                    f"| `{change.path or '.'}` | {change.change.value} | {impact} | "
                    f"{change.description} |"
                )
        else:
            lines.append("_No changes._")
        emit("\n".join(lines), output, console=state.console)
    else:
        print_comparison(state.console, report, old=old_label, new=new_label)

    if no_fail or not state.config.compatibility.fail_on_breaking:
        raise typer.Exit(EXIT_OK)
    raise typer.Exit(EXIT_OK if report.compatible else EXIT_FINDINGS)


__all__ = ["analyze", "compare", "impact", "validate"]
