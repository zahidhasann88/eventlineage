#!/usr/bin/env python3
"""Benchmark discovery and validation against a generated project on disk.

The benchmark writes ``--files`` catalog fragments into a temporary directory
(``--keep`` keeps them) and times a cold scan, a warm incremental scan and the
validation pass.

Usage::

    python benchmarks/bench_scan.py --files 200 --events-per-file 50 --repeat 3
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import emit, environment, measure
from eventlineage import Project
from eventlineage.analysis import analyze_catalog
from eventlineage.graph.builder import build_graph
from eventlineage.validation import validate_catalog


def write_project(root: Path, files: int, events_per_file: int, services: int = 0) -> Project:
    (root / "eventlineage.yaml").write_text(
        "version: 1\nproject:\n  name: scan-benchmark\ndiscovery:\n  roots: ['.']\n",
        encoding="utf-8",
    )
    total_events = max(1, files * events_per_file)
    service_count = services or max(4, total_events // 12)
    events_dir = root / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    for file_index in range(files):
        lines = ["version: 1", "events:"]
        for event_index in range(events_per_file):
            global_index = file_index * events_per_file + event_index
            name = f"domain.{global_index:06d}"
            producer = f"svc-{global_index % service_count:04d}"
            consumer = f"svc-{(global_index * 3 + 1) % service_count:04d}"
            lines.extend(
                [
                    f"  - name: {name}",
                    "    version: 1.0.0",
                    f"    topics: [topic-{file_index % 10}]",
                    "    producers:",
                    f"      - service: {producer}",
                    "    consumers:",
                    f"      - service: {consumer}",
                ]
            )
        (events_dir / f"catalog-{file_index:04d}.yaml").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
    return Project.open(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=200)
    parser.add_argument("--events-per-file", type=int, default=50)
    parser.add_argument(
        "--services",
        type=int,
        default=0,
        help="Number of services in the generated project (0 = one per 12 events)",
    )
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--output", default=None)
    parser.add_argument("--keep", action="store_true", help="Keep the generated project")
    args = parser.parse_args(argv)

    directory = Path(tempfile.mkdtemp(prefix="eventlineage-bench-"))
    project = write_project(directory, args.files, args.events_per_file, args.services)
    cold = project.scan(incremental=False)
    catalog = project.catalog()

    phases = {
        "graph_build": lambda: build_graph(catalog),
        "analyze": lambda: analyze_catalog(catalog),
        "validate": lambda: validate_catalog(catalog),
    }

    # Warm up first: ``jsonschema`` pulls in the RFC 3987 grammar validator,
    # which costs seconds on its first import. That cost is per process, not per
    # run, so it is excluded from the phase timings (docs/performance.md).
    for phase in phases.values():
        phase()

    results = {
        "environment": environment(),
        "project": {
            "root": str(directory) if args.keep else "<temporary directory>",
            "files": args.files,
            "events": cold.summary.events,
            "services": cold.summary.services,
            "relationships": cold.summary.relationships,
            "topics": cold.summary.topics,
        },
        "timings": {
            "cold_scan": measure(
                lambda: Project.open(directory).scan(incremental=False), repeat=args.repeat
            ),
            "warm_incremental_scan": measure(
                lambda: Project.open(directory).scan(incremental=True), repeat=args.repeat
            ),
            **{name: measure(phase, repeat=args.repeat) for name, phase in phases.items()},
        },
        "cache": cold.summary.cache,
    }

    emit(results, output=args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
