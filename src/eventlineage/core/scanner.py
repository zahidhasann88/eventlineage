"""The scan pipeline: walk files, run discovery plugins, build a catalog."""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from pydantic import Field

from eventlineage.config import Config
from eventlineage.core.catalog import EventCatalog, SourceFile
from eventlineage.core.merge import SchemaConflict
from eventlineage.discovery.base import (
    HEAD_SIZE,
    DiscoveryContext,
    DiscoveryPlugin,
    DiscoveryResult,
)
from eventlineage.discovery.registry import PluginRegistry, default_registry
from eventlineage.exceptions import DiscoveryError, ParseError
from eventlineage.graph.builder import apply_inferred_relationships
from eventlineage.models.base import EGBaseModel
from eventlineage.models.common import Diagnostic, Severity, SourceLocation
from eventlineage.models.dependency import EventRelationship
from eventlineage.utils.cache import ScanCache, digest_bytes
from eventlineage.utils.fs import iter_files, relative_posix

logger = logging.getLogger(__name__)


class FileScan(EGBaseModel):
    """Per-file outcome, exposed by ``eventlineage scan --verbose``."""

    path: str
    plugin: str
    digest: str
    events: int = 0
    topics: int = 0
    services: int = 0
    relationships: int = 0
    cached: bool = False
    duration_ms: float = 0.0
    error: str | None = None


class ScanSummary(EGBaseModel):
    """Aggregate statistics for one scan."""

    root: str = "."
    project: str = "eventlineage"
    files_considered: int = 0
    files_parsed: int = 0
    files_cached: int = 0
    files_ignored: int = 0
    files_failed: int = 0
    events: int = 0
    topics: int = 0
    services: int = 0
    relationships: int = 0
    inferred_relationships: int = 0
    folded_schema_events: int = 0
    duration_ms: float = 0.0
    plugin_counts: dict[str, int] = Field(default_factory=dict)
    cache: dict[str, int] = Field(default_factory=dict)
    incremental: bool = True

    @property
    def files_used(self) -> int:
        """Files considered, skipped or failed."""
        return self.files_parsed + self.files_cached


class ScanResult(EGBaseModel):
    """Catalog plus scan metadata."""

    catalog: EventCatalog
    summary: ScanSummary
    files: tuple[FileScan, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def sources(self) -> tuple[SourceFile, ...]:
        """Every file that contributed to the catalog."""
        return self.catalog.sources

    @property
    def ok(self) -> bool:
        """Whether the scan finished without errors."""
        return not any(item.severity is Severity.ERROR for item in self.diagnostics)

    def warnings(self) -> tuple[Diagnostic, ...]:
        """Diagnostics that are warnings rather than errors."""
        return tuple(item for item in self.diagnostics if item.severity is Severity.WARNING)


@dataclass(slots=True)
class _FileOutcome:
    scan: FileScan | None
    result: DiscoveryResult | None
    diagnostics: list[Diagnostic]


class Scanner:
    """Coordinates discovery across a project tree.

    The scanner is stateless between runs; all state lives in the cache file, so
    two scanners pointed at the same project never interfere.
    """

    def __init__(
        self,
        config: Config,
        *,
        registry: PluginRegistry | None = None,
        logger_: logging.Logger | None = None,
        jobs: int = 1,
    ) -> None:
        self._config = config
        self._registry = registry or default_registry()
        self._log = logger_ or logger
        self._jobs = max(1, jobs)
        self._plugins = self._registry.active(
            enabled=config.discovery.plugins,
            disabled=config.discovery.disabled_plugins,
        )
        self._context = DiscoveryContext(
            root=config.root, config=config, logger=self._log.getChild("discovery")
        )

    @property
    def plugins(self) -> tuple[DiscoveryPlugin, ...]:
        """Plugin names in registration order."""
        return tuple(self._plugins)

    def collect_files(self, paths: Sequence[Path | str] | None = None) -> list[Path]:
        """Resolve scan roots into a deterministic list of candidate files."""
        if paths:
            roots = [Path(path) for path in paths]
        else:
            roots = [self._config.root / root for root in self._config.discovery.roots]
        discovery = self._config.discovery
        files = list(
            iter_files(
                roots,
                exclude=discovery.exclude,
                include=discovery.include,
                follow_symlinks=discovery.follow_symlinks,
                skip_hidden_dirs=discovery.skip_hidden_dirs,
                base=self._config.root,
            )
        )
        return sorted(files, key=lambda path: relative_posix(path, self._config.root))

    def scan(
        self,
        paths: Sequence[Path | str] | None = None,
        *,
        incremental: bool = True,
        use_cache: bool | None = None,
    ) -> ScanResult:
        """Scan the project and return a fully normalized catalog."""
        started = time.perf_counter()
        cache_enabled = (
            self._config.cache.enabled
            if use_cache is None
            else (use_cache and self._config.cache.enabled)
        )
        cache = self._load_cache(enabled=cache_enabled and incremental)

        files = self.collect_files(paths)
        self._log.debug(
            "scanning %d candidate files with %d plugins", len(files), len(self._plugins)
        )

        outcomes = self._run(files, cache=cache)

        catalog = EventCatalog(
            project_name=self._config.project_name,
            root=str(self._config.root),
            config_fingerprint=self._config.fingerprint(),
        )
        diagnostics: list[Diagnostic] = []
        file_records: list[FileScan] = []
        plugin_counts: Counter[str] = Counter()
        conflicts: list[SchemaConflict] = []
        relationships: dict[tuple[str, str, str], EventRelationship] = {}
        cached_count = parsed_count = failed_count = ignored_count = 0

        for outcome in outcomes:
            if outcome.scan is None:
                ignored_count += 1
                diagnostics.extend(outcome.diagnostics)
                continue
            file_records.append(outcome.scan)
            diagnostics.extend(outcome.diagnostics)
            if outcome.scan.error:
                failed_count += 1
                continue
            if outcome.scan.cached:
                cached_count += 1
            else:
                parsed_count += 1
            plugin_counts[outcome.scan.plugin] += 1
            result = outcome.result
            if result is None:  # pragma: no cover - defensive
                continue
            for service in result.services:
                catalog.add_service(service)
            for topic in result.topics:
                catalog.add_topic(topic)
            for relationship in result.relationships:
                relationships.setdefault(relationship.key(), relationship)
            for event in result.events:
                catalog.add_event(event, conflicts=conflicts)

        catalog.set_relationships(relationships.values())

        for conflict in conflicts:
            kept = conflict.kept.path if conflict.kept else "unknown source"
            rejected = conflict.rejected.path if conflict.rejected else "unknown source"
            diagnostics.append(
                Diagnostic(
                    severity=Severity.WARNING,
                    code="schema-conflict",
                    message=(
                        f"event {conflict.event_id!r} is defined with a different schema in "
                        f"{rejected}; keeping the definition from {kept}"
                    ),
                    source=conflict.rejected,
                )
            )

        folded = catalog.fold_duplicate_schemas()
        for duplicate_id, owner_id in folded:
            self._log.info(
                "folded schema-only event %s into %s (identical schema)", duplicate_id, owner_id
            )
        catalog.normalize()
        inferred = 0
        if self._config.analysis.infer_relationships:
            inferred = apply_inferred_relationships(catalog)

        catalog.sources = tuple(
            SourceFile(
                path=record.path,
                plugin=record.plugin,
                digest=record.digest,
                events=record.events,
                topics=record.topics,
                services=record.services,
                cached=record.cached,
            )
            for record in sorted(file_records, key=lambda record: record.path)
        )
        catalog.diagnostics = tuple(diagnostics)

        if cache_enabled:
            if paths is None:  # a partial scan says nothing about the other files
                cache.prune({record.path for record in file_records})
            cache.save()

        duration_ms = (time.perf_counter() - started) * 1000.0
        stats = catalog.stats()
        summary = ScanSummary(
            root=str(self._config.root),
            project=self._config.project_name,
            files_considered=len(files),
            files_parsed=parsed_count,
            files_cached=cached_count,
            files_ignored=ignored_count,
            files_failed=failed_count,
            events=stats.events,
            topics=stats.topics,
            services=stats.services,
            relationships=len(catalog.relationships),
            inferred_relationships=inferred,
            folded_schema_events=len(folded),
            duration_ms=round(duration_ms, 2),
            plugin_counts=dict(sorted(plugin_counts.items())),
            cache=cache.stats() if cache_enabled else {},
            incremental=incremental and cache_enabled,
        )
        return ScanResult(
            catalog=catalog,
            summary=summary,
            files=tuple(file_records),
            diagnostics=tuple(diagnostics),
        )

    def _load_cache(self, *, enabled: bool) -> ScanCache:
        path = (self._config.root / self._config.cache.directory / "scan.json").resolve()
        if not enabled:
            return ScanCache(path=path, fingerprint=self._config.fingerprint())
        return ScanCache.load(
            path, self._config.fingerprint(), max_bytes=self._config.cache.max_bytes
        )

    def _run(self, files: Sequence[Path], *, cache: ScanCache) -> list[_FileOutcome]:
        if self._jobs == 1 or len(files) < 16:
            return [self._scan_file(path, cache) for path in files]
        with ThreadPoolExecutor(max_workers=self._jobs) as pool:
            return list(pool.map(lambda path: self._scan_file(path, cache), files))

    def _scan_file(self, path: Path, cache: ScanCache) -> _FileOutcome:
        reference = relative_posix(path, self._config.root)
        started = time.perf_counter()
        discovery = self._config.discovery
        max_bytes = discovery.max_file_size_kb * 1024
        try:
            raw = path.read_bytes()
        except OSError as exc:  # pragma: no cover - filesystem failure
            return _FileOutcome(
                scan=None,
                result=None,
                diagnostics=[
                    Diagnostic(
                        severity=Severity.WARNING,
                        message=f"cannot read {reference}: {exc}",
                        code="read-error",
                    )
                ],
            )
        if len(raw) > max_bytes:
            return _FileOutcome(
                scan=FileScan(
                    path=reference,
                    plugin="-",
                    digest="",
                    error=f"file exceeds {discovery.max_file_size_kb} KiB limit",
                ),
                result=None,
                diagnostics=[
                    Diagnostic(
                        severity=Severity.WARNING,
                        message=(
                            f"skipped {reference}: larger than {discovery.max_file_size_kb} KiB"
                        ),
                        code="file-too-large",
                    )
                ],
            )
        digest = digest_bytes(raw)
        text = raw.decode("utf-8-sig", errors="replace")

        plugin = self._select_plugin(path, text)
        if plugin is None:
            return _FileOutcome(scan=None, result=None, diagnostics=[])

        cached = cache.get(reference, digest)
        if (
            cached is not None
            and cached.plugin == plugin.name
            and cached.dependencies == self._digests(cached.dependencies)
        ):
            try:
                result = DiscoveryResult.from_dict(cached.payload)
            except Exception:  # pragma: no cover - corrupt cache entry
                self._log.debug("ignoring corrupt cache entry for %s", reference)
                result = None
            if result is not None:
                return _FileOutcome(
                    scan=FileScan(
                        path=reference,
                        plugin=plugin.name,
                        digest=digest,
                        events=len(result.events),
                        topics=len(result.topics),
                        services=len(result.services),
                        relationships=len(result.relationships),
                        cached=True,
                        duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                    ),
                    result=result,
                    diagnostics=list(result.diagnostics),
                )

        context = replace(self._context, references=set())
        try:
            result = plugin.discover(path, context)
        except (ParseError, DiscoveryError) as exc:
            self._log.debug("plugin %s failed on %s", plugin.name, reference, exc_info=True)
            return _FileOutcome(
                scan=FileScan(
                    path=reference,
                    plugin=plugin.name,
                    digest=digest,
                    error=str(exc),
                ),
                result=None,
                diagnostics=[
                    Diagnostic(
                        severity=Severity.WARNING,
                        message=str(exc),
                        source=SourceLocation(path=reference, plugin=plugin.name),
                        code="parse-error",
                    )
                ],
            )
        except Exception as exc:  # pragma: no cover - unexpected plugin bug
            self._log.warning("plugin %s crashed on %s", plugin.name, reference, exc_info=True)
            return _FileOutcome(
                scan=FileScan(
                    path=reference,
                    plugin=plugin.name,
                    digest=digest,
                    error=f"{type(exc).__name__}: {exc}",
                ),
                result=None,
                diagnostics=[
                    Diagnostic(
                        severity=Severity.ERROR,
                        message=(
                            f"discovery plugin {plugin.name!r} crashed: {type(exc).__name__}: {exc}"
                        ),
                        source=SourceLocation(path=reference, plugin=plugin.name),
                        code="plugin-crash",
                    )
                ],
            )

        dependencies = self._digests(
            relative_posix(item, self._config.root) for item in context.references
        )
        cache.put(reference, digest, plugin.name, result.to_dict(), dependencies)
        return _FileOutcome(
            scan=FileScan(
                path=reference,
                plugin=plugin.name,
                digest=digest,
                events=len(result.events),
                topics=len(result.topics),
                services=len(result.services),
                relationships=len(result.relationships),
                cached=False,
                duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
            ),
            result=result,
            diagnostics=list(result.diagnostics),
        )

    def _digests(self, references: Iterable[str]) -> dict[str, str]:
        """Current digest of each referenced file; a missing file digests to ``""``."""
        digests: dict[str, str] = {}
        for reference in references:
            try:
                digests[reference] = digest_bytes((self._config.root / reference).read_bytes())
            except OSError:
                digests[reference] = ""
        return digests

    def _select_plugin(self, path: Path, text: str) -> DiscoveryPlugin | None:
        """Pick the most confident plugin for ``path`` (deterministic tie-break)."""
        head = text[:HEAD_SIZE]
        best: tuple[int, int, str, DiscoveryPlugin] | None = None
        for plugin in self._plugins:
            if not plugin.supports(path):
                continue
            try:
                score = plugin.detect(path, head)
            except Exception:  # pragma: no cover - defensive
                self._log.debug("detect() of %s raised", plugin.name, exc_info=True)
                score = 0
            if score <= 0:
                continue
            candidate = (-score, -plugin.priority, plugin.name, plugin)
            if best is None or candidate[:3] < best[:3]:
                best = candidate
        return None if best is None else best[3]


def scan_project(
    config: Config | None = None,
    *,
    paths: Sequence[Path | str] | None = None,
    incremental: bool = True,
    registry: PluginRegistry | None = None,
) -> ScanResult:
    """Convenience wrapper used by the SDK."""
    config = config or Config.load()
    return Scanner(config, registry=registry).scan(paths, incremental=incremental)


__all__ = ["FileScan", "ScanResult", "ScanSummary", "Scanner", "scan_project"]
