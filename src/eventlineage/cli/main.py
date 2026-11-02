"""The ``eventlineage`` command line interface.

Exit codes
----------
``0``  success
``1``  the command succeeded but findings/policy failed (breaking change,
       validation findings above the threshold, risk above ``--fail-on-risk``)
``2``  usage or configuration error
``3``  unexpected runtime failure

These codes are stable and documented so CI pipelines can rely on them.
"""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path
from typing import Annotated

import typer

from eventlineage import __version__
from eventlineage.cli.commands import analysis, inspect, lifecycle, output
from eventlineage.cli.common import EXIT_ERROR, EXIT_OK, build_state
from eventlineage.exceptions import EventLineageError

app = typer.Typer(
    name="eventlineage",
    help=(
        "Event dependency intelligence for event-driven architectures.\n\n"
        "Discover events from schemas, AsyncAPI/OpenAPI documents, Kafka exports and "
        "catalogs; then validate, analyse, compare and report on them — entirely offline."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    add_completion=True,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"eventlineage {__version__}")
        raise typer.Exit(EXIT_OK)


@app.callback()
def root(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to eventlineage.yaml (default: search upwards)"),
    ] = None,
    project_root: Annotated[
        Path | None, typer.Option("--root", help="Project root directory")
    ] = None,
    verbose: Annotated[
        int, typer.Option("--verbose", "-v", count=True, help="More logging (-vv for debug)")
    ] = 0,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only errors are printed")] = False,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Disable the incremental scan cache")
    ] = False,
    log_format: Annotated[
        str, typer.Option("--log-format", help="Log format: text | json")
    ] = "text",
    no_color: Annotated[bool, typer.Option("--no-color", help="Disable ANSI colours")] = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version_callback, is_eager=True, help="Show the version"
        ),
    ] = False,
) -> None:
    """Global options applied to every command."""
    del version  # consumed by the eager --version callback
    if log_format not in {"text", "json"}:
        typer.echo(f"error: unknown --log-format {log_format!r} (use text or json)", err=True)
        raise typer.Exit(2)
    build_state(
        ctx,
        config=config,
        root=project_root,
        verbose=verbose,
        quiet=quiet,
        no_cache=no_cache,
        log_json=log_format == "json",
        no_color=no_color,
    )


app.command("init", help="Create an eventlineage.yaml project configuration.")(lifecycle.init)
app.command("scan", help="Discover events, services and topics.")(lifecycle.scan)
app.command("analyze", help="Summarise architecture health, hotspots and cycles.")(analysis.analyze)
app.command("validate", help="Run validation rules and report findings.")(analysis.validate)
app.command("graph", help="Render the dependency graph (DOT, Mermaid, JSON).")(output.graph)
app.command("impact", help="Calculate the blast radius of an event or service.")(analysis.impact)
app.command("events", help="List discovered events.")(inspect.events)
app.command("services", help="List services and their event involvement.")(inspect.services)
app.command("topics", help="List topics and their participants.")(inspect.topics)
app.command("compare", help="Compare two schema versions or two schema files.")(analysis.compare)
app.command("report", help="Generate a Markdown or JSON architecture report.")(output.report)
app.command("export", help="Export the catalog or graph for other tools.")(output.export)
app.command("rules", help="List validation rules.")(lifecycle.rules)
app.command("plugins", help="List discovery plugins.")(lifecycle.plugins)
app.command("integrations", help="Show optional infrastructure integrations.")(
    lifecycle.integrations
)
app.command("pull", help="Fetch topics or schemas from live infrastructure into a file.")(
    lifecycle.pull
)
app.add_typer(lifecycle.cache_app, name="cache", help="Inspect or clear the scan cache.")


@app.command("version")
def version() -> None:
    """Print the EventLineage version."""
    typer.echo(f"eventlineage {__version__}")


def _use_utf8_output() -> None:
    """Keep redirected output encodable.

    A Windows pipe or file defaults to a legacy code page, which cannot encode the
    arrows and dashes in reports; a real console is already UTF-8 in Python.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        reconfigure = getattr(stream, "reconfigure", None)
        if encoding != "utf8" and reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    """Console-script entry point."""
    _use_utf8_output()
    try:
        app()
    except EventLineageError as exc:  # raised outside ``run_guarded``, e.g. loading config
        typer.echo(f"error: {exc}", err=True)
        raise SystemExit(exc.exit_code) from exc
    except OSError as exc:
        # The reader went away (``eventlineage report | head``). Windows reports it as
        # EINVAL. Point stdout at devnull so the flush at interpreter exit stays quiet.
        if isinstance(exc, BrokenPipeError) or exc.errno in {errno.EPIPE, errno.EINVAL}:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            raise SystemExit(EXIT_OK) from None
        typer.echo(f"error: {exc}", err=True)
        raise SystemExit(EXIT_ERROR) from exc
    except Exception as exc:
        typer.echo(f"error: unexpected {type(exc).__name__}: {exc}", err=True)
        raise SystemExit(EXIT_ERROR) from exc
    except KeyboardInterrupt:  # pragma: no cover - interactive
        typer.echo("aborted", err=True)
        raise SystemExit(EXIT_ERROR) from None


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["app", "main"]
