"""Public entry points for schema comparison."""

from __future__ import annotations

import itertools
from typing import Any

from eventlineage.compatibility.avro import AvroComparer
from eventlineage.compatibility.base import (
    JsonSchemaComparer,
    OpenApiSchemaComparer,
    StructuralComparer,
)
from eventlineage.compatibility.protobuf import ProtobufComparer
from eventlineage.compatibility.result import (
    ChangeImpact,
    ChangeType,
    CompatibilityMode,
    CompatibilityReport,
    EventVersionComparison,
    SchemaChange,
)
from eventlineage.exceptions import CompatibilityError
from eventlineage.models.common import SchemaFormat
from eventlineage.models.event import Event
from eventlineage.models.schema import EventSchema
from eventlineage.parsers import detect_schema_format, parse_schema
from eventlineage.utils.versioning import sort_versions

_COMPARERS: dict[SchemaFormat, StructuralComparer] = {
    SchemaFormat.JSON_SCHEMA: JsonSchemaComparer(),
    SchemaFormat.OPENAPI: OpenApiSchemaComparer(),
    SchemaFormat.AVRO: AvroComparer(),
    SchemaFormat.PROTOBUF: ProtobufComparer(),
    SchemaFormat.UNKNOWN: JsonSchemaComparer(),
}


def comparer_for(format: SchemaFormat | str) -> StructuralComparer:
    """Return the comparer for a schema dialect."""
    if isinstance(format, str):
        format = SchemaFormat(format)
    return _COMPARERS.get(format, _COMPARERS[SchemaFormat.UNKNOWN])


def _resolve_format(old: EventSchema, new: EventSchema) -> SchemaFormat:
    old_format = (
        old.format
        if old.format != SchemaFormat.UNKNOWN
        else detect_schema_format(old.definition or old.raw)
    )
    new_format = (
        new.format
        if new.format != SchemaFormat.UNKNOWN
        else detect_schema_format(new.definition or new.raw)
    )
    if old_format == SchemaFormat.UNKNOWN:
        old_format = new_format
    if new_format == SchemaFormat.UNKNOWN:
        new_format = old_format
    if old_format != new_format:
        raise CompatibilityError(
            "cannot compare schemas of different dialects: "
            f"{old_format.value} vs {new_format.value}"
        )
    return old_format


def compare_schemas(
    old: EventSchema,
    new: EventSchema,
    mode: CompatibilityMode = CompatibilityMode.BACKWARD,
) -> CompatibilityReport:
    """Compare two schema documents and classify every difference.

    Raises :class:`CompatibilityError` when the dialects differ, because a
    cross-dialect comparison has no well-defined compatibility verdict.
    """
    mode = CompatibilityMode.coerce(mode)
    format = _resolve_format(old, new)
    if old.is_empty() or new.is_empty():
        # Missing contract information is not a breaking change: comparing a
        # known schema to an undocumented one would only produce noise.
        return CompatibilityReport(
            format=format,
            mode=mode,
            old_name=old.name,
            new_name=new.name,
            old_hash=old.content_hash,
            new_hash=new.content_hash,
            changes=(),
        )
    comparer = comparer_for(format)
    if (
        isinstance(comparer, ProtobufComparer)
        and isinstance(old.definition, str)
        and isinstance(new.definition, str)
    ):
        return comparer.compare_files(
            old.definition,
            new.definition,
            mode,
            old_name=old.name,
            new_name=new.name,
            old_hash=old.content_hash,
            new_hash=new.content_hash,
        )
    old_parsed, new_parsed = parse_schema(old), parse_schema(new)
    return comparer.compare(
        old_parsed,
        new_parsed,
        mode,
        old_name=old.name,
        new_name=new.name,
        old_hash=old.content_hash,
        new_hash=new.content_hash,
    )


def compare_definitions(
    old_definition: Any,
    new_definition: Any,
    *,
    format: SchemaFormat | str | None = None,
    mode: CompatibilityMode = CompatibilityMode.BACKWARD,
) -> CompatibilityReport:
    """Compare two raw schema documents (dicts, lists or proto text)."""
    old_format = (
        SchemaFormat(format) if format is not None else detect_schema_format(old_definition)
    )
    new_format = (
        SchemaFormat(format) if format is not None else detect_schema_format(new_definition)
    )
    old_schema = EventSchema(format=old_format, definition=old_definition)
    new_schema = EventSchema(format=new_format, definition=new_definition)
    return compare_schemas(old_schema, new_schema, mode)


def compare_event_versions(
    event: Event,
    old_version: str,
    new_version: str,
    mode: CompatibilityMode = CompatibilityMode.BACKWARD,
) -> EventVersionComparison:
    """Compare two named versions of the same event."""
    entries = {entry.version: entry for entry in event.version_entries()}
    missing = [version for version in (old_version, new_version) if version not in entries]
    if missing:
        raise CompatibilityError(
            f"event {event.id!r} has no version(s) {', '.join(missing)}; "
            f"known versions: {', '.join(sorted(entries)) or 'none'}"
        )
    old_schema = entries[old_version].schema
    new_schema = entries[new_version].schema
    if old_schema is None or new_schema is None:
        raise CompatibilityError(
            f"event {event.id!r} version(s) {old_version if old_schema is None else new_version} "
            "have no schema to compare"
        )
    report = compare_schemas(old_schema, new_schema, mode)
    return EventVersionComparison(
        event_id=event.id,
        old_version=old_version,
        new_version=new_version,
        report=report,
    )


def compare_event_history(
    event: Event, mode: CompatibilityMode = CompatibilityMode.BACKWARD
) -> tuple[EventVersionComparison, ...]:
    """Compare each consecutive pair of versions of ``event`` (oldest first)."""
    with_schema = [
        entry
        for entry in event.version_entries()
        if entry.schema is not None and not entry.schema.is_empty()
    ]
    if len(with_schema) < 2:
        return ()
    ordered = sorted(with_schema, key=lambda entry: sort_versions([entry.version])[0])
    ordered.sort(key=lambda entry: _version_order(entry.version))
    comparisons: list[EventVersionComparison] = []
    for previous, current in itertools.pairwise(ordered):
        report = compare_schemas(previous.schema, current.schema, mode)  # type: ignore[arg-type]
        comparisons.append(
            EventVersionComparison(
                event_id=event.id,
                old_version=previous.version,
                new_version=current.version,
                report=report,
            )
        )
    return tuple(comparisons)


def _version_order(version: str) -> tuple[tuple[int, int, str], ...]:
    from eventlineage.utils.versioning import version_key

    return version_key(version)


def breaking_changes(report: CompatibilityReport) -> tuple[SchemaChange, ...]:
    """Changes that are breaking under the report's mode."""
    return tuple(change for change in report.changes if change.impact is ChangeImpact.BREAKING)


__all__ = [
    "ChangeImpact",
    "ChangeType",
    "CompatibilityMode",
    "CompatibilityReport",
    "EventVersionComparison",
    "SchemaChange",
    "breaking_changes",
    "compare_definitions",
    "compare_event_history",
    "compare_event_versions",
    "compare_schemas",
    "comparer_for",
]
