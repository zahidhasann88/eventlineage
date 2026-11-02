"""Deterministic JSON reporting.

Every collection is sorted before serialization and every map is emitted with
sorted keys, so two runs against the same inputs produce byte-identical files —
which is what makes generated reports usable as CI artifacts and review diffs.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from eventlineage.analysis.impact import ImpactReport
from eventlineage.analysis.summary import ArchitectureSummary
from eventlineage.compatibility.result import EventVersionComparison
from eventlineage.core.catalog import EventCatalog
from eventlineage.validation.engine import ValidationReport

SCHEMA_VERSION = "1.0"


def build_json_report(
    catalog: EventCatalog,
    *,
    summary: ArchitectureSummary | None = None,
    validation: ValidationReport | None = None,
    impacts: Sequence[ImpactReport] = (),
    comparisons: Sequence[EventVersionComparison] = (),
    include_schemas: bool = False,
    title: str | None = None,
    generated_at: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the full JSON report document."""
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "project": catalog.project_name,
        "title": title or f"{catalog.project_name} event architecture",
        "catalog": catalog.to_dict(include_schemas=include_schemas),
    }
    if generated_at:
        document["generated_at"] = generated_at
    if summary is not None:
        document["analysis"] = summary.to_dict()
    if validation is not None:
        document["validation"] = validation.to_dict()
    if impacts:
        document["impact"] = [impact.to_dict() for impact in impacts]
    if comparisons:
        document["compatibility"] = [comparison.to_dict() for comparison in comparisons]
    if extra:
        document.update({key: value for key, value in extra.items() if key not in document})
    return document


def render_json(document: Mapping[str, Any], *, indent: int = 2) -> str:
    """Serialize a report document deterministically."""
    return json.dumps(document, indent=indent, sort_keys=True, default=str) + "\n"


def render_catalog_json(catalog: EventCatalog, *, include_schemas: bool = False) -> str:
    """Render a catalog as stable, sorted JSON."""
    return render_json(catalog.to_dict(include_schemas=include_schemas))


__all__ = ["SCHEMA_VERSION", "build_json_report", "render_catalog_json", "render_json"]
