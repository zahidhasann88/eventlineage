"""Reusable option/argument annotations for Typer commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from eventlineage.cli.common import State, get_state

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="Path to eventlineage.yaml", show_default=False),
]
RootOption = Annotated[
    Path | None,
    typer.Option("--root", help="Project root directory (defaults to the config location)"),
]
OutputOption = Annotated[
    Path | None,
    typer.Option(
        "--output", "-o", help="Write output to a file instead of stdout", show_default=False
    ),
]
VerboseOption = Annotated[
    int,
    typer.Option("--verbose", "-v", count=True, help="Increase log verbosity (-v info, -vv debug)"),
]
QuietOption = Annotated[bool, typer.Option("--quiet", "-q", help="Suppress non-error output")]


def resolve_state(
    ctx: typer.Context,
    config: Path | None = None,
    root: Path | None = None,
    *,
    verbose: int = 0,
    quiet: bool = False,
) -> State:
    """Merge per-command overrides into the invocation state."""
    state = get_state(ctx)
    if config is not None:
        state.config_path = config
        state._config = None
        state._project = None
    if root is not None:
        state.root = root
        state._config = None
        state._project = None
    if verbose:
        state.verbose = max(state.verbose, verbose)
    if quiet:
        state.quiet = True
    return state


__all__ = [
    "ConfigOption",
    "OutputOption",
    "QuietOption",
    "RootOption",
    "VerboseOption",
    "resolve_state",
]
