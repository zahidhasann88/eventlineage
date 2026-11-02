"""Shared CLI plumbing: state, configuration loading, output and exit codes."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn, TypeVar

import typer
from rich.console import Console
from rich.markup import escape

from eventlineage.config import Config
from eventlineage.exceptions import EventLineageError
from eventlineage.project import Project
from eventlineage.reporting.console import make_console
from eventlineage.utils.fs import write_atomic
from eventlineage.utils.logging import configure_logging

_T = TypeVar("_T")

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_ERROR = 3


@dataclass
class State:
    """Per-invocation CLI state, carried on ``typer.Context.obj``."""

    config_path: Path | None = None
    root: Path | None = None
    verbose: int = 0
    quiet: bool = False
    no_cache: bool = False
    log_json: bool = False
    no_color: bool = False
    console: Console = field(default_factory=Console)
    _project: Project | None = None
    _config: Config | None = None

    @property
    def config(self) -> Config:
        """The resolved configuration."""
        if self._config is None:
            config = Config.load(self.config_path, root=self.root)
            if self.no_cache:
                config = config.model_copy(
                    update={"cache": config.cache.model_copy(update={"enabled": False})}
                )
            self._config = config
        return self._config

    @property
    def project(self) -> Project:
        """The opened project."""
        if self._project is None:
            self._project = Project.open(
                self.config_path,
                root=self.root,
                jobs=1,
                registry=None,
            )
            self._project.config = self.config
        return self._project


def build_state(
    ctx: typer.Context,
    *,
    config: Path | None,
    root: Path | None,
    verbose: int,
    quiet: bool,
    no_cache: bool,
    log_json: bool,
    no_color: bool,
) -> State:
    """Create the state object and configure logging/console."""
    effective_verbose = 0 if quiet else verbose
    configure_logging(effective_verbose, quiet=quiet, json_output=log_json)
    console = make_console(no_color=no_color, quiet=quiet)
    state = State(
        config_path=config,
        root=root,
        verbose=effective_verbose,
        quiet=quiet,
        no_cache=no_cache,
        log_json=log_json,
        no_color=no_color,
        console=console,
    )
    ctx.obj = state
    return state


def get_state(ctx: typer.Context) -> State:
    """Return the CLI state bound to ``ctx``."""
    state = ctx.obj
    if not isinstance(state, State):  # pragma: no cover - defensive
        state = State(console=make_console())
        ctx.obj = state
    return state


def fail(message: str, *, code: int = EXIT_ERROR, hint: str | None = None) -> NoReturn:
    """Print an error to stderr and exit with a stable code."""
    console = Console(stderr=True, soft_wrap=True)
    console.print(f"[bold red]error:[/bold red] {escape(message)}")
    if hint:
        console.print(f"[dim]hint: {escape(hint)}[/dim]")
    raise typer.Exit(code)


def run_guarded(action: Callable[[], _T]) -> _T:
    """Execute ``action`` translating EventLineage errors into stable exit codes."""
    try:
        return action()
    except EventLineageError as exc:
        fail(exc.message, code=exc.exit_code, hint=exc.hint)
    except BrokenPipeError:  # pragma: no cover - e.g. piping into head
        raise typer.Exit(EXIT_OK) from None


def emit(content: str, output: Path | None, *, console: Console | None = None) -> None:
    """Write ``content`` to ``output`` or stdout.

    Machine-readable output goes straight to stdout: Rich would wrap long JSON
    lines to the terminal width and corrupt the document when piped.
    """
    payload = content if content.endswith("\n") else content + "\n"
    if output is None:
        sys.stdout.write(payload)
        sys.stdout.flush()
        return
    write_atomic(output, payload)
    if console is not None:
        console.print(f"[dim]wrote {escape(str(output))}[/dim]")


def normalize_format(value: str, allowed: tuple[str, ...], *, option: str = "--format") -> str:
    """Validate ``value`` against ``allowed`` and return it."""
    normalized = value.strip().lower()
    if normalized not in allowed:
        fail(
            f"unsupported {option} {value!r}",
            code=EXIT_USAGE,
            hint=f"choose one of: {', '.join(allowed)}",
        )
    return normalized


__all__ = [
    "EXIT_ERROR",
    "EXIT_FINDINGS",
    "EXIT_OK",
    "EXIT_USAGE",
    "State",
    "build_state",
    "emit",
    "fail",
    "get_state",
    "normalize_format",
    "run_guarded",
]
