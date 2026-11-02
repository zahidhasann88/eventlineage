"""Compatibility vocabulary: modes, change kinds, impacts and reports."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field

from eventlineage.models.base import EGBaseModel
from eventlineage.models.common import SchemaFormat


class CompatibilityMode(StrEnum):
    """Direction in which two schema versions are checked.

    * ``backward`` — a consumer using the **new** schema can read data produced
      with the **old** schema (the default for schema registries: upgrade
      consumers first).
    * ``forward`` — a consumer still using the **old** schema can read data
      produced with the **new** schema.
    * ``full`` — both directions must hold.

    The mode is what turns a raw structural difference into a verdict: removing
    an optional field is backward compatible but can break forward readers.
    """

    BACKWARD = "backward"
    FORWARD = "forward"
    FULL = "full"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @classmethod
    def coerce(cls, value: str | CompatibilityMode) -> CompatibilityMode:
        """Normalize a string into the enum member it names."""
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise ValueError(f"unknown compatibility mode: {value!r}") from exc


class ChangeType(StrEnum):
    """Kinds of structural schema differences EventLineage detects."""

    FIELD_REMOVED = "field_removed"
    FIELD_ADDED = "field_added"
    FIELD_RENAMED = "field_renamed"
    FIELD_NUMBER_CHANGED = "field_number_changed"
    TYPE_CHANGED = "type_changed"
    REQUIRED_ADDED = "required_added"
    REQUIRED_REMOVED = "required_removed"
    NULLABILITY_CHANGED = "nullability_changed"
    CARDINALITY_CHANGED = "cardinality_changed"
    DEFAULT_CHANGED = "default_changed"
    ENUM_VALUE_ADDED = "enum_value_added"
    ENUM_VALUE_REMOVED = "enum_value_removed"
    CONSTRAINT_TIGHTENED = "constraint_tightened"
    CONSTRAINT_LOOSENED = "constraint_loosened"
    CONSTRAINT_CHANGED = "constraint_changed"
    ROOT_TYPE_CHANGED = "root_type_changed"


class ChangeImpact(StrEnum):
    """How badly a change can affect participants."""

    BREAKING = "breaking"
    POTENTIALLY_BREAKING = "potentially_breaking"
    COMPATIBLE = "compatible"

    @property
    def severity(self) -> int:
        """Impact level as a sortable integer."""
        return {"compatible": 0, "potentially_breaking": 1, "breaking": 2}[self.value]

    @property
    def is_breaking(self) -> bool:
        """Whether the impact is breaking for this mode."""
        return self is ChangeImpact.BREAKING


def worst(*impacts: ChangeImpact) -> ChangeImpact:
    """Return the most severe impact of the arguments."""
    return max(impacts, key=lambda impact: impact.severity, default=ChangeImpact.COMPATIBLE)


REMEDIATION: dict[ChangeType, str] = {
    ChangeType.FIELD_REMOVED: (
        "Keep the field but mark it deprecated for at least one release cycle, or "
        "publish a new event version and migrate consumers first."
    ),
    ChangeType.FIELD_ADDED: (
        "Add new fields as optional with a sensible default so older producers and "
        "consumers can be upgraded independently."
    ),
    ChangeType.FIELD_RENAMED: (
        "Add the new name alongside the old one (or an Avro alias) and remove the old "
        "field in a later version."
    ),
    ChangeType.FIELD_NUMBER_CHANGED: (
        "Never reuse or change protobuf field numbers; declare the old number as "
        "reserved and introduce a new field instead."
    ),
    ChangeType.TYPE_CHANGED: (
        "Introduce a new field with the new type and deprecate the old one; type "
        "changes cannot be rolled out safely."
    ),
    ChangeType.REQUIRED_ADDED: (
        "Keep the field optional, or backfill it in the producer before making it "
        "required in a later version."
    ),
    ChangeType.REQUIRED_REMOVED: "Relaxing a field is safe; document the change for consumers.",
    ChangeType.NULLABILITY_CHANGED: (
        "Treat null as a valid value for at least one release before disallowing it."
    ),
    ChangeType.CARDINALITY_CHANGED: (
        "Use an array from the start, or publish a new field holding the repeated form."
    ),
    ChangeType.DEFAULT_CHANGED: "Document the new default; it changes consumer behaviour.",
    ChangeType.ENUM_VALUE_ADDED: (
        "Ensure consumers treat enums as open (ignore unknown values) before adding a value."
    ),
    ChangeType.ENUM_VALUE_REMOVED: (
        "Deprecate the value and keep it accepted until all producers stop emitting it."
    ),
    ChangeType.CONSTRAINT_TIGHTENED: (
        "Loosen the constraint or verify all producers already satisfy the new rule."
    ),
    ChangeType.CONSTRAINT_LOOSENED: (
        "Ensure consumers tolerate values outside the previous range before loosening."
    ),
    ChangeType.CONSTRAINT_CHANGED: "Verify producers and consumers agree on the new constraint.",
    ChangeType.ROOT_TYPE_CHANGED: "Publish a new event and migrate consumers explicitly.",
}


class SchemaChange(EGBaseModel):
    """One detected difference between two schema versions."""

    change: ChangeType
    path: str = Field(description="Dotted path of the affected field ('' for the root)")
    description: str
    mode: CompatibilityMode = CompatibilityMode.BACKWARD
    impact: ChangeImpact = ChangeImpact.COMPATIBLE
    impacts: dict[str, ChangeImpact] = Field(
        default_factory=dict, description="Impact per compatibility mode"
    )
    old_value: Any = None
    new_value: Any = None
    field_type: str | None = None
    remediation: str | None = None

    @property
    def breaking(self) -> bool:
        """Whether any change is breaking under the report's mode."""
        return self.impact is ChangeImpact.BREAKING

    def identity(self) -> tuple[str, str, str]:
        """Return a stable identity string for this record."""
        return (self.change.value, self.path, self.impact.value)

    def render(self) -> str:
        """Render the change as ``path: description``."""
        old = "" if self.old_value is None else f" ({self.old_value!r} -> {self.new_value!r})"
        return f"{self.path or '<root>'}: {self.description}{old}"


class CompatibilityReport(EGBaseModel):
    """Verdict for two schema versions under one compatibility mode."""

    format: SchemaFormat
    mode: CompatibilityMode = CompatibilityMode.BACKWARD
    old_name: str | None = None
    new_name: str | None = None
    old_hash: str | None = None
    new_hash: str | None = None
    changes: tuple[SchemaChange, ...] = ()

    @property
    def breaking_changes(self) -> tuple[SchemaChange, ...]:
        """Changes classified as breaking under the report's mode."""
        return tuple(change for change in self.changes if change.impact is ChangeImpact.BREAKING)

    @property
    def potentially_breaking_changes(self) -> tuple[SchemaChange, ...]:
        """Changes that are potentially breaking."""
        return tuple(
            change for change in self.changes if change.impact is ChangeImpact.POTENTIALLY_BREAKING
        )

    @property
    def compatible(self) -> bool:
        """True when the new schema is acceptable under the requested mode."""
        return not self.breaking_changes

    @property
    def identical(self) -> bool:
        """Whether the two schemas are byte-identical."""
        return not self.changes

    def counts(self) -> dict[str, int]:
        """Return counts grouped by severity."""
        counts = {"breaking": 0, "potentially_breaking": 0, "compatible": 0}
        for change in self.changes:
            counts[change.impact.value] += 1
        return counts

    def by_change_type(self) -> dict[str, int]:
        """Return changes grouped by change type."""
        counts: dict[str, int] = {}
        for change in self.changes:
            counts[change.change.value] = counts.get(change.change.value, 0) + 1
        return dict(sorted(counts.items()))

    def summary(self) -> str:
        """Return a one-line human summary."""
        if not self.changes:
            return "schemas are identical"
        counts = self.counts()
        verdict = "COMPATIBLE" if self.compatible else "BREAKING"
        return (
            f"{verdict} ({self.mode.value}): {counts['breaking']} breaking, "
            f"{counts['potentially_breaking']} potentially breaking, "
            f"{counts['compatible']} compatible changes"
        )

    def for_mode(self, mode: CompatibilityMode) -> CompatibilityReport:
        """Re-evaluate the same diffs for a different compatibility mode."""
        changes = tuple(
            change.model_copy(
                update={
                    "mode": mode,
                    "impact": change.impacts.get(mode.value, change.impact),
                    "remediation": change.remediation,
                }
            )
            for change in self.changes
        )
        return self.model_copy(update={"mode": mode, "changes": changes})

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready representation."""
        return {
            "format": self.format.value,
            "mode": self.mode.value,
            "compatible": self.compatible,
            "identical": self.identical,
            "old_name": self.old_name,
            "new_name": self.new_name,
            "counts": self.counts(),
            "changes": [
                {
                    "change": change.change.value,
                    "path": change.path,
                    "impact": change.impact.value,
                    "description": change.description,
                    "old": change.old_value,
                    "new": change.new_value,
                    "remediation": change.remediation,
                }
                for change in self.changes
            ],
        }


class EventVersionComparison(EGBaseModel):
    """Comparison between two versions of the same event."""

    event_id: str
    old_version: str
    new_version: str
    report: CompatibilityReport

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready representation."""
        return {
            "event": self.event_id,
            "old_version": self.old_version,
            "new_version": self.new_version,
            **self.report.to_dict(),
        }
