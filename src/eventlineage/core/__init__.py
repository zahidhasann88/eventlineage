"""Scan pipeline and the normalized catalog.

``discovery`` turns files into fragments; this package merges them into an
:class:`~eventlineage.core.catalog.EventCatalog` and owns the incremental scan
cache. Nothing here knows about the CLI, the terminal or broker integrations.
"""

from __future__ import annotations

from eventlineage.core.catalog import (
    CatalogStats,
    EventCatalog,
    ServiceEventIndex,
    build_catalog,
)
from eventlineage.core.merge import merge_events
from eventlineage.core.scanner import Scanner, ScanResult, ScanSummary, scan_project

__all__ = [
    "CatalogStats",
    "EventCatalog",
    "ScanResult",
    "ScanSummary",
    "Scanner",
    "ServiceEventIndex",
    "build_catalog",
    "merge_events",
    "scan_project",
]
