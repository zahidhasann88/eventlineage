"""High-level project facade — the entry point of the Python SDK.

    >>> from eventlineage import Project
    >>> project = Project.open(".")
    >>> catalog = project.scan().catalog
    >>> report = project.validate()
    >>> impact = project.impact("orders.created")
    >>> print(impact.summary())

The facade owns nothing the low-level API cannot do; it exists so the common
workflow is three lines instead of ten, and so the CLI and SDK share exactly the
same code path.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eventlineage.analysis.impact import ImpactReport, analyze_impact
from eventlineage.analysis.summary import ArchitectureSummary, analyze_catalog
from eventlineage.compatibility.engine import (
    compare_event_history,
    compare_event_versions,
    compare_schemas,
)
from eventlineage.compatibility.result import (
    CompatibilityMode,
    CompatibilityReport,
    EventVersionComparison,
)
from eventlineage.config import Config, default_config_document
from eventlineage.core.catalog import EventCatalog
from eventlineage.core.scanner import Scanner, ScanResult
from eventlineage.discovery.registry import PluginRegistry
from eventlineage.exceptions import ProjectNotFoundError, SymbolNotFoundError
from eventlineage.graph.builder import build_graph
from eventlineage.graph.model import Graph
from eventlineage.models.common import Severity
from eventlineage.models.schema import EventSchema
from eventlineage.reporting.dot import render_dot
from eventlineage.reporting.json_report import build_json_report, render_json
from eventlineage.reporting.markdown_report import render_markdown_report
from eventlineage.reporting.mermaid import render_mermaid
from eventlineage.utils.logging import get_logger
from eventlineage.validation.engine import ValidationReport, Validator
from eventlineage.validation.rule import Rule

logger = get_logger(__name__)


@dataclass
class Project:
    """A loaded EventLineage project."""

    config: Config
    registry: PluginRegistry | None = None
    jobs: int = 1
    _scan: ScanResult | None = field(default=None, repr=False)
    _graph: Graph | None = field(default=None, repr=False)
    _log: logging.Logger = field(default_factory=lambda: get_logger("project"), repr=False)

    @classmethod
    def open(
        cls,
        path: Path | str | None = None,
        *,
        root: Path | str | None = None,
        require_config: bool = False,
        jobs: int = 1,
        registry: PluginRegistry | None = None,
    ) -> Project:
        """Load configuration from ``path`` (or search upwards from ``root``).

        Passing a directory is accepted as a convenience: it is treated as the
        project root, so ``Project.open("my-platform")`` does the obvious thing.
        """
        if path is not None:
            candidate = Path(path).expanduser()
            if candidate.is_dir() and root is None:
                root, path = candidate, None
        config = Config.load(path, root=root, required=require_config)
        return cls(config=config, registry=registry, jobs=jobs)

    @classmethod
    def create(
        cls,
        root: Path | str,
        *,
        name: str | None = None,
        force: bool = False,
        minimal: bool = False,
    ) -> Path:
        """Write a starter ``eventlineage.yaml`` and return its path."""
        directory = Path(root)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "eventlineage.yaml"
        if target.exists() and not force:
            raise FileExistsError(str(target))
        if minimal:
            content = "version: 1\n\nproject:\n  name: %s\n" % (name or directory.name)
        else:
            content = default_config_document()
            if name:
                content = content.replace("name: my-platform", f"name: {name}")
        target.write_text(content, encoding="utf-8")
        return target

    @property
    def root(self) -> Path:
        """The project root directory."""
        return self.config.root

    def scan(
        self,
        paths: Sequence[Path | str] | None = None,
        *,
        incremental: bool = True,
        refresh: bool = False,
    ) -> ScanResult:
        """Scan the project (cached within the lifetime of this object)."""
        if self._scan is not None and not refresh and paths is None:
            return self._scan
        scanner = Scanner(self.config, registry=self.registry, jobs=self.jobs)
        result = scanner.scan(paths, incremental=incremental)
        if paths is None:
            self._scan = result
            self._graph = None
        return result

    def catalog(self, *, refresh: bool = False) -> EventCatalog:
        """The scanned catalog, scanning on first use."""
        return self.scan(refresh=refresh).catalog

    def graph(self, *, refresh: bool = False) -> Graph:
        """The dependency graph for the catalog, built on first use."""
        if self._graph is None or refresh:
            self._graph = build_graph(
                self.catalog(refresh=refresh),
                include_topics=self.config.analysis.include_topics,
            )
        return self._graph

    def validate(
        self,
        *,
        rules: Sequence[type[Rule]] | None = None,
        fail_on: Severity | None = None,
    ) -> ValidationReport:
        """Run the validation rules against the catalog."""
        config = self.config.with_validation_overrides(fail_on=fail_on) if fail_on else self.config
        return Validator(config, list(rules) if rules else None).validate(
            self.catalog(), config=config
        )

    def analyze(self) -> ArchitectureSummary:
        """Summarise the architecture of the scanned project."""
        return analyze_catalog(self.catalog(), self.config, graph=self.graph())

    def impact(
        self,
        target: str,
        *,
        max_depth: int | None = None,
    ) -> ImpactReport:
        """Impact analysis for an event, service or topic.

        Raises :class:`SymbolNotFoundError` when the symbol cannot be resolved.
        """
        return analyze_impact(
            self.catalog(),
            target,
            graph=self.graph(),
            max_depth=max_depth if max_depth is not None else self.config.analysis.max_impact_depth,
            risk_high_services=self.config.analysis.risk_high_services,
            risk_critical_services=self.config.analysis.risk_critical_services,
        )

    def compare_versions(
        self,
        event: str,
        old_version: str,
        new_version: str,
        *,
        mode: CompatibilityMode | None = None,
    ) -> EventVersionComparison:
        """Compare two versions of one event in the catalog."""
        resolved = self.catalog().resolve_event(event)
        if resolved is None:
            raise SymbolNotFoundError(event, "event", candidates=tuple(self.catalog().events))
        return compare_event_versions(
            resolved,
            old_version,
            new_version,
            mode=mode or self.config.compatibility.mode,
        )

    def compare_schemas(
        self,
        old: EventSchema | Path | str,
        new: EventSchema | Path | str,
        *,
        mode: CompatibilityMode | None = None,
    ) -> CompatibilityReport:
        """Compare two schemas, given as :class:`EventSchema` objects or paths."""
        return compare_schemas(
            self._as_schema(old),
            self._as_schema(new),
            mode=mode or self.config.compatibility.mode,
        )

    @staticmethod
    def _as_schema(value: EventSchema | Path | str) -> EventSchema:
        if isinstance(value, EventSchema):
            return value
        from eventlineage.utils.schema_io import load_schema_document

        return load_schema_document(value)

    def version_history(
        self, event: str | None = None, *, mode: CompatibilityMode | None = None
    ) -> tuple[EventVersionComparison, ...]:
        """Compare consecutive versions, for one event or the whole catalog."""
        effective_mode = mode or self.config.compatibility.mode
        if event is not None:
            resolved = self.catalog().resolve_event(event)
            if resolved is None:
                raise SymbolNotFoundError(event, "event", candidates=tuple(self.catalog().events))
            return compare_event_history(resolved, effective_mode)
        comparisons: list[EventVersionComparison] = []
        for entry in self.catalog().sorted_events():
            comparisons.extend(compare_event_history(entry, effective_mode))
        return tuple(comparisons)

    def report(
        self,
        *,
        format: str = "markdown",
        include_schemas: bool | None = None,
        include_diagrams: bool | None = None,
        title: str | None = None,
        generated_at: str | None = None,
        impacts: Sequence[str] = (),
    ) -> str:
        """Render a report as ``markdown``, ``json``, ``dot`` or ``mermaid``."""
        catalog = self.catalog()
        title = title or self.config.report.title
        if format in {"dot", "graphviz"}:
            return render_dot(self.graph(), title=title or catalog.project_name)
        if format == "mermaid":
            return render_mermaid(self.graph())
        if format not in {"json", "md", "markdown"}:
            raise ValueError(
                f"unsupported report format: {format!r} (expected markdown, json, dot or mermaid)"
            )
        summary = self.analyze()
        validation = self.validate()
        impact_reports = [self.impact(target) for target in impacts]
        comparisons = self.version_history()
        include_schemas = (
            self.config.report.include_schemas if include_schemas is None else include_schemas
        )
        include_diagrams = (
            self.config.report.include_diagrams if include_diagrams is None else include_diagrams
        )
        if format == "json":
            document = build_json_report(
                catalog,
                summary=summary,
                validation=validation,
                impacts=impact_reports,
                comparisons=comparisons,
                include_schemas=include_schemas,
                title=title,
                generated_at=generated_at,
            )
            return render_json(document)
        if format in {"md", "markdown"}:
            return render_markdown_report(
                catalog,
                summary=summary,
                validation=validation,
                impacts=impact_reports,
                comparisons=comparisons,
                include_schemas=include_schemas,
                include_diagrams=include_diagrams,
                title=title,
                generated_at=generated_at,
                max_events=self.config.report.max_events,
                max_impacted_nodes=self.config.report.max_impacted_nodes,
            )
        raise ValueError(f"unsupported report format: {format!r}")

    def stats(self) -> dict[str, Any]:
        """Return catalog statistics as a plain mapping."""
        return self.catalog().stats().model_dump(mode="json")


def open_project(path: Path | str | None = None, **kwargs: Any) -> Project:
    """Alias for :meth:`Project.open`."""
    return Project.open(path, **kwargs)


def require_project(path: Path | str | None = None, **kwargs: Any) -> Project:
    """Like :meth:`Project.open` but fails when no configuration exists."""
    return Project.open(path, require_config=True, **kwargs)


__all__ = ["Project", "ProjectNotFoundError", "open_project", "require_project"]
