"""Scale tests: the documented performance targets (10k events, 100k edges).

These are marked ``performance`` so a normal unit run stays fast; run them with
``pytest -m performance``.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from eventlineage.analysis import analyze_catalog, analyze_impact
from eventlineage.core.catalog import EventCatalog
from eventlineage.graph.algorithms import bfs, find_cycle_groups, topological_sort
from eventlineage.graph.builder import build_graph
from eventlineage.models.event import Event
from eventlineage.models.participant import Consumer, Producer, Service
from eventlineage.validation import validate_catalog

pytestmark = pytest.mark.performance

EVENTS = 10_000
SERVICES = 500


def synthetic_catalog(events: int = EVENTS, services: int = SERVICES) -> EventCatalog:
    """A realistic-ish fan-shaped topology: each event has a producer and 1-3 consumers."""
    catalog = EventCatalog(project_name="synthetic")
    for index in range(events):
        producer = f"svc-{index % services:04d}"
        consumers = [
            Consumer(service=f"svc-{(index * 7 + offset) % services:04d}") for offset in range(1, 3)
        ]
        catalog.add_event(
            Event(
                id=f"event.{index:05d}",
                name=f"event.{index:05d}",
                version="1.0.0",
                producers=(Producer(service=producer),),
                consumers=tuple(consumers),
                topics=(f"topic.{index % 100:03d}",),
            )
        )
    for index in range(services):
        catalog.add_service(Service(name=f"svc-{index:04d}", tier="tier-2"))
    catalog.normalize()
    return catalog


@pytest.fixture(scope="module")
def large_catalog() -> EventCatalog:
    return synthetic_catalog()


def test_catalog_normalisation_is_fast(large_catalog: EventCatalog) -> None:
    assert large_catalog.stats().events == EVENTS


def test_graph_build_under_budget(large_catalog: EventCatalog) -> None:
    started = time.perf_counter()
    graph = build_graph(large_catalog)
    elapsed = time.perf_counter() - started
    assert graph.node_count >= EVENTS
    assert elapsed < 5.0, f"graph build took {elapsed:.2f}s"


def test_bfs_traversal_under_budget(large_catalog: EventCatalog) -> None:
    graph = build_graph(large_catalog)
    started = time.perf_counter()
    result = bfs(graph, 0)
    elapsed = time.perf_counter() - started
    assert result.order
    assert elapsed < 2.0, f"BFS took {elapsed:.2f}s"


def test_topological_sort_handles_cycles(large_catalog: EventCatalog) -> None:
    graph = build_graph(large_catalog)
    started = time.perf_counter()
    topological_sort(graph)
    elapsed = time.perf_counter() - started
    assert elapsed < 5.0, f"topological sort took {elapsed:.2f}s"


def test_cycle_detection_under_budget(large_catalog: EventCatalog) -> None:
    graph = build_graph(large_catalog)
    started = time.perf_counter()
    groups = find_cycle_groups(graph)
    elapsed = time.perf_counter() - started
    assert isinstance(groups, list)
    assert elapsed < 5.0, f"cycle detection took {elapsed:.2f}s"


def test_validation_under_budget(large_catalog: EventCatalog) -> None:
    started = time.perf_counter()
    report = validate_catalog(large_catalog)
    elapsed = time.perf_counter() - started
    assert report.rules_run
    assert elapsed < 30.0, f"validation took {elapsed:.2f}s"


def test_analysis_under_budget(large_catalog: EventCatalog) -> None:
    started = time.perf_counter()
    summary = analyze_catalog(large_catalog)
    elapsed = time.perf_counter() - started
    assert summary.stats.events == EVENTS
    assert elapsed < 30.0, f"analysis took {elapsed:.2f}s"


def test_impact_under_budget(large_catalog: EventCatalog) -> None:
    started = time.perf_counter()
    report = analyze_impact(large_catalog, "event.00000")
    elapsed = time.perf_counter() - started
    assert report.resolved
    assert elapsed < 10.0, f"impact analysis took {elapsed:.2f}s"


def test_scan_scales_with_file_count(tmp_path: Path) -> None:
    """Scanning 1k generated catalog fragments stays interactive."""
    from eventlineage import Project

    (tmp_path / "eventlineage.yaml").write_text(
        "version: 1\nproject:\n  name: many\n", encoding="utf-8"
    )
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    for index in range(200):
        body = "\n".join(
            f"  - name: generated.{index:03d}.{offset}\n    version: 1.0.0\n" for offset in range(5)
        )
        (events_dir / f"catalog-{index:03d}.yaml").write_text(
            f"version: 1\nevents:\n{body}", encoding="utf-8"
        )
    project = Project.open(tmp_path)
    started = time.perf_counter()
    result = project.scan(incremental=False)
    elapsed = time.perf_counter() - started
    assert result.summary.events == 1000
    assert elapsed < 60.0, f"scan took {elapsed:.2f}s"
