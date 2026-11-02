"""Shared helpers for the EventLineage benchmark scripts.

Every benchmark prints a JSON document so results can be diffed between
machines and pasted into ``docs/performance.md``. Nothing here depends on the
CLI, so the numbers describe the engine only.
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from eventlineage import __version__


def percentile(values: Iterable[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def measure(
    callable_: Callable[[], Any],
    *,
    repeat: int = 5,
    warmup: int = 1,
) -> dict[str, float]:
    """Run ``callable_`` ``repeat`` times and summarize the wall-clock times."""
    for _ in range(warmup):
        callable_()
    samples: list[float] = []
    for _ in range(repeat):
        started = time.perf_counter()
        callable_()
        samples.append((time.perf_counter() - started) * 1000.0)
    return {
        "runs": repeat,
        "min_ms": round(min(samples), 3),
        "median_ms": round(statistics.median(samples), 3),
        "mean_ms": round(statistics.fmean(samples), 3),
        "p95_ms": round(percentile(samples, 0.95), 3),
        "max_ms": round(max(samples), 3),
    }


def environment() -> dict[str, Any]:
    return {
        "eventlineage": __version__,
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
    }


def emit(document: dict[str, Any], *, output: str | None = None) -> None:
    payload = json.dumps(document, indent=2, sort_keys=True, default=str)
    if output:
        Path(output).write_text(payload + "\n", encoding="utf-8")
        print(f"wrote {output}", file=sys.stderr)
    else:
        print(payload)


def networkx_available() -> bool:
    try:  # pragma: no cover - optional dependency
        import networkx  # noqa: F401
    except ImportError:
        return False
    return True
