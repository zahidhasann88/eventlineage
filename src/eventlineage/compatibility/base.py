"""Structural schema comparison shared by every dialect.

The comparer walks two normalized field trees in parallel and emits
:class:`SchemaChange` records. Each change carries an impact *per compatibility
mode* (backward/forward/full), so the same diff can be re-evaluated without
re-parsing; the report then selects the impact for the requested mode.

Format differences are handled by overriding a handful of hooks:

``is_required``
    Avro treats a field with a default as optional; protobuf2 has ``required``.
``added_field_impacts``
    Avro requires a default for a newly added field to stay backward compatible.
``type_change_impacts``
    Protobuf judges compatibility by wire type, not by logical type name.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import ClassVar

from eventlineage.compatibility.result import (
    REMEDIATION,
    ChangeImpact,
    ChangeType,
    CompatibilityMode,
    CompatibilityReport,
    SchemaChange,
    worst,
)
from eventlineage.models.common import SchemaFormat
from eventlineage.models.schema import ParsedSchema, SchemaField
from eventlineage.parsers.schema_types import (
    CanonicalType,
    canonical,
    types_related,
    widening_allowed,
)

_ImpactPair = tuple[ChangeImpact, ChangeImpact]  # (backward, forward)

#: Bump-heavy keywords where a numeric direction decides tightening/loosening.
_INCREASE_TIGHTENS = frozenset(
    {"minLength", "minItems", "minimum", "exclusiveMinimum", "minProperties"}
)
_DECREASE_TIGHTENS = frozenset(
    {"maxLength", "maxItems", "maximum", "exclusiveMaximum", "maxProperties"}
)

#: Constraints that are ignored by compatibility analysis (metadata-ish).
_IGNORED_CONSTRAINTS = frozenset(
    {"doc", "description", "title", "examples", "example", "deprecated", "aliases"}
)


def _pair(backward: ChangeImpact, forward: ChangeImpact) -> _ImpactPair:
    return (backward, forward)


def _worst_pair(first: _ImpactPair, second: _ImpactPair) -> _ImpactPair:
    return (worst(first[0], second[0]), worst(first[1], second[1]))


class StructuralComparer:
    """Dialect-agnostic diff of two normalized schema field trees."""

    format: ClassVar[SchemaFormat] = SchemaFormat.JSON_SCHEMA

    def is_required(self, field: SchemaField) -> bool:
        """Whether ``field`` must be present in every message."""
        return field.required

    def added_field_impacts(self, field: SchemaField) -> _ImpactPair:
        """Impact of a field that exists only in the new schema."""
        if self.is_required(field):
            # New readers demand a field that old data does not contain.
            return _pair(ChangeImpact.BREAKING, ChangeImpact.COMPATIBLE)
        return _pair(ChangeImpact.COMPATIBLE, ChangeImpact.COMPATIBLE)

    def removed_field_impacts(self, field: SchemaField) -> _ImpactPair:
        """Impact of a field that exists only in the old schema."""
        if self.is_required(field):
            # Old readers demand a field that new data will not contain.
            return _pair(ChangeImpact.COMPATIBLE, ChangeImpact.BREAKING)
        return _pair(ChangeImpact.COMPATIBLE, ChangeImpact.POTENTIALLY_BREAKING)

    def required_change_impacts(self, old: SchemaField, new: SchemaField) -> _ImpactPair:
        """Impact of a field becoming required or optional."""
        if not self.is_required(old) and self.is_required(new):
            return _pair(ChangeImpact.BREAKING, ChangeImpact.COMPATIBLE)
        return _pair(ChangeImpact.COMPATIBLE, ChangeImpact.POTENTIALLY_BREAKING)

    def rename_impacts(self, old: SchemaField, new: SchemaField) -> _ImpactPair:
        """Impact of a rename, honouring declared aliases."""
        if self._aliases(new).intersection({old.name}) or self._aliases(old).intersection(
            {new.name}
        ):
            return _pair(ChangeImpact.COMPATIBLE, ChangeImpact.COMPATIBLE)
        return _pair(ChangeImpact.BREAKING, ChangeImpact.BREAKING)

    def type_change_impacts(self, old: SchemaField, new: SchemaField) -> _ImpactPair | None:
        """Return impacts, or ``None`` when the change should be treated structurally."""
        old_type, new_type = canonical(old.type), canonical(new.type)
        if old_type == new_type:
            return None
        if widening_allowed(old.type, new.type):
            # New readers can read narrower old values; old readers may choke on
            # wider new values.
            return _pair(ChangeImpact.COMPATIBLE, ChangeImpact.POTENTIALLY_BREAKING)
        if widening_allowed(new.type, old.type):
            return _pair(ChangeImpact.BREAKING, ChangeImpact.COMPATIBLE)
        return _pair(ChangeImpact.BREAKING, ChangeImpact.BREAKING)

    def enum_added_impacts(self) -> _ImpactPair:
        """Impact of an enum value added in the new schema."""
        return _pair(ChangeImpact.COMPATIBLE, ChangeImpact.BREAKING)

    def enum_removed_impacts(self) -> _ImpactPair:
        """Impact of an enum value removed in the new schema."""
        return _pair(ChangeImpact.BREAKING, ChangeImpact.COMPATIBLE)

    def compare(
        self,
        old: ParsedSchema,
        new: ParsedSchema,
        mode: CompatibilityMode = CompatibilityMode.BACKWARD,
        *,
        old_name: str | None = None,
        new_name: str | None = None,
        old_hash: str | None = None,
        new_hash: str | None = None,
    ) -> CompatibilityReport:
        """Compare two parsed schemas and classify every difference."""
        changes: list[SchemaChange] = []
        if old.root_type != new.root_type:
            changes.append(
                self._change(
                    ChangeType.ROOT_TYPE_CHANGED,
                    "",
                    f"root type changed from {old.root_type!r} to {new.root_type!r}",
                    _pair(ChangeImpact.BREAKING, ChangeImpact.BREAKING),
                    mode,
                    old_value=old.root_type,
                    new_value=new.root_type,
                )
            )
        else:
            self._compare_fields(old.fields, new.fields, "", changes, mode)
            self._compare_root_constraints(old, new, changes, mode)
        ordered = sorted(changes, key=lambda change: (change.path, change.change.value))
        return CompatibilityReport(
            format=self.format,
            mode=mode,
            old_name=old_name or old.name,
            new_name=new_name or new.name,
            old_hash=old_hash,
            new_hash=new_hash,
            changes=tuple(ordered),
        )

    def _compare_root_constraints(
        self,
        old: ParsedSchema,
        new: ParsedSchema,
        changes: list[SchemaChange],
        mode: CompatibilityMode,
    ) -> None:
        """Compare what applies to the document itself, which has no field to hang on."""

        def root(parsed: ParsedSchema) -> SchemaField:
            value = parsed.metadata.get("additional_properties")
            return SchemaField(
                name="", constraints={} if value is None else {"additionalProperties": value}
            )

        self._compare_constraints(root(old), root(new), "", changes, mode)

    def _compare_fields(
        self,
        old_fields: Sequence[SchemaField],
        new_fields: Sequence[SchemaField],
        prefix: str,
        changes: list[SchemaChange],
        mode: CompatibilityMode,
    ) -> None:
        old_map = {field.name: field for field in old_fields}
        new_map = {field.name: field for field in new_fields}

        removed = [old_map[name] for name in sorted(old_map) if name not in new_map]
        added = [new_map[name] for name in sorted(new_map) if name not in old_map]
        paired = self._pair_renames(removed, added)
        paired_old = {old_field.name for old_field, _ in paired}
        paired_new = {new_field.name for _, new_field in paired}

        for old_field, new_field in paired:
            path = self._join(prefix, new_field.name)
            changes.append(
                self._change(
                    ChangeType.FIELD_RENAMED,
                    path,
                    f"field {old_field.name!r} appears to be renamed to {new_field.name!r}",
                    self.rename_impacts(old_field, new_field),
                    mode,
                    old_value=old_field.name,
                    new_value=new_field.name,
                    field_type=new_field.type,
                )
            )
            if old_field.fields and new_field.fields:
                self._compare_fields(old_field.fields, new_field.fields, path, changes, mode)

        for field in removed:
            if field.name in paired_old:
                continue
            path = self._join(prefix, field.name)
            changes.append(
                self._change(
                    ChangeType.FIELD_REMOVED,
                    path,
                    f"field {field.name!r} was removed",
                    self.removed_field_impacts(field),
                    mode,
                    old_value=field.type,
                    field_type=field.type,
                )
            )

        for field in added:
            if field.name in paired_new:
                continue
            path = self._join(prefix, field.name)
            changes.append(
                self._change(
                    ChangeType.FIELD_ADDED,
                    path,
                    f"field {field.name!r} was added as {field.type}"
                    + ("" if self.is_required(field) else " (optional)"),
                    self.added_field_impacts(field),
                    mode,
                    new_value=field.type,
                    field_type=field.type,
                )
            )

        for name in sorted(set(old_map) & set(new_map)):
            self._compare_field(old_map[name], new_map[name], prefix, changes, mode)

    def _compare_field(
        self,
        old: SchemaField,
        new: SchemaField,
        prefix: str,
        changes: list[SchemaChange],
        mode: CompatibilityMode,
    ) -> None:
        path = self._join(prefix, old.name)

        if old.repeated != new.repeated:
            changes.append(
                self._change(
                    ChangeType.CARDINALITY_CHANGED,
                    path,
                    "cardinality changed between single value and list",
                    _pair(ChangeImpact.BREAKING, ChangeImpact.BREAKING),
                    mode,
                    old_value="repeated" if old.repeated else "single",
                    new_value="repeated" if new.repeated else "single",
                )
            )

        type_impacts = self.type_change_impacts(old, new)
        if type_impacts is not None:
            changes.append(
                self._change(
                    ChangeType.TYPE_CHANGED,
                    path,
                    f"type changed from {old.type!r} to {new.type!r}",
                    type_impacts,
                    mode,
                    old_value=old.type,
                    new_value=new.type,
                    field_type=new.type,
                )
            )
        elif old.fields and new.fields:
            if canonical(old.type) == CanonicalType.UNION:
                self._compare_union(old, new, path, changes, mode)
            else:
                self._compare_fields(old.fields, new.fields, path, changes, mode)

        if self.is_required(old) != self.is_required(new):
            change_type = (
                ChangeType.REQUIRED_ADDED if self.is_required(new) else ChangeType.REQUIRED_REMOVED
            )
            changes.append(
                self._change(
                    change_type,
                    path,
                    "field became required" if self.is_required(new) else "field became optional",
                    self.required_change_impacts(old, new),
                    mode,
                    old_value=self.is_required(old),
                    new_value=self.is_required(new),
                )
            )

        if old.nullable != new.nullable:
            impacts = (
                # Old data may hold nulls the new reader refuses.
                _pair(ChangeImpact.BREAKING, ChangeImpact.COMPATIBLE)
                if old.nullable
                # New data may hold nulls the old reader has never seen.
                else _pair(ChangeImpact.COMPATIBLE, ChangeImpact.POTENTIALLY_BREAKING)
            )
            changes.append(
                self._change(
                    ChangeType.NULLABILITY_CHANGED,
                    path,
                    "field no longer accepts null" if old.nullable else "field now accepts null",
                    impacts,
                    mode,
                    old_value=old.nullable,
                    new_value=new.nullable,
                )
            )

        if old.enum_values or new.enum_values:
            old_values, new_values = set(old.enum_values), set(new.enum_values)
            for value in sorted(new_values - old_values):
                changes.append(
                    self._change(
                        ChangeType.ENUM_VALUE_ADDED,
                        path,
                        f"enum value {value!r} added",
                        self.enum_added_impacts(),
                        mode,
                        new_value=value,
                        field_type="enum",
                    )
                )
            for value in sorted(old_values - new_values):
                changes.append(
                    self._change(
                        ChangeType.ENUM_VALUE_REMOVED,
                        path,
                        f"enum value {value!r} removed",
                        self.enum_removed_impacts(),
                        mode,
                        old_value=value,
                        field_type="enum",
                    )
                )

        self._compare_constraints(old, new, path, changes, mode)

        if old.has_default != new.has_default or (
            old.has_default and new.has_default and old.default != new.default
        ):
            changes.append(
                self._change(
                    ChangeType.DEFAULT_CHANGED,
                    path,
                    "default value changed"
                    if old.has_default and new.has_default
                    else ("default value added" if new.has_default else "default value removed"),
                    _pair(ChangeImpact.COMPATIBLE, ChangeImpact.COMPATIBLE),
                    mode,
                    old_value=old.default if old.has_default else None,
                    new_value=new.default if new.has_default else None,
                )
            )

    def _compare_union(
        self,
        old: SchemaField,
        new: SchemaField,
        path: str,
        changes: list[SchemaChange],
        mode: CompatibilityMode,
    ) -> None:
        """Compare union branches as a set: their position carries no meaning."""

        def branches(field: SchemaField) -> dict[str, SchemaField]:
            return {
                ":".join((branch.type, *sorted(child.name for child in branch.fields))): branch
                for branch in field.fields
            }

        old_branches, new_branches = branches(old), branches(new)
        for key in sorted(new_branches.keys() - old_branches.keys()):
            branch_type = new_branches[key].type
            changes.append(
                self._change(
                    ChangeType.FIELD_ADDED,
                    path,
                    f"union branch {branch_type!r} added",
                    _pair(ChangeImpact.COMPATIBLE, ChangeImpact.BREAKING),
                    mode,
                    new_value=key,
                    field_type=branch_type,
                )
            )
        for key in sorted(old_branches.keys() - new_branches.keys()):
            branch_type = old_branches[key].type
            changes.append(
                self._change(
                    ChangeType.FIELD_REMOVED,
                    path,
                    f"union branch {branch_type!r} removed",
                    _pair(ChangeImpact.BREAKING, ChangeImpact.COMPATIBLE),
                    mode,
                    old_value=key,
                    field_type=branch_type,
                )
            )

    def _compare_constraints(
        self,
        old: SchemaField,
        new: SchemaField,
        path: str,
        changes: list[SchemaChange],
        mode: CompatibilityMode,
    ) -> None:
        old_constraints = {
            key: value for key, value in old.constraints.items() if key not in _IGNORED_CONSTRAINTS
        }
        new_constraints = {
            key: value for key, value in new.constraints.items() if key not in _IGNORED_CONSTRAINTS
        }
        for key in sorted(set(old_constraints) | set(new_constraints)):
            old_value = old_constraints.get(key)
            new_value = new_constraints.get(key)
            if old_value == new_value:
                continue
            verdict = self._classify_constraint(key, old_value, new_value)
            if verdict == "ignore":
                continue
            if verdict == "tightened":
                impacts = _pair(ChangeImpact.BREAKING, ChangeImpact.COMPATIBLE)
                change_type = ChangeType.CONSTRAINT_TIGHTENED
            elif verdict == "loosened":
                impacts = _pair(ChangeImpact.COMPATIBLE, ChangeImpact.POTENTIALLY_BREAKING)
                change_type = ChangeType.CONSTRAINT_LOOSENED
            else:
                impacts = _pair(ChangeImpact.BREAKING, ChangeImpact.POTENTIALLY_BREAKING)
                change_type = ChangeType.CONSTRAINT_CHANGED
            changes.append(
                self._change(
                    change_type,
                    path,
                    f"constraint {key!r} changed from {old_value!r} to {new_value!r}",
                    impacts,
                    mode,
                    old_value=old_value,
                    new_value=new_value,
                )
            )

    def _classify_constraint(self, key: str, old_value: object, new_value: object) -> str:
        if key in {"number", "label", "oneof"}:
            return "changed"
        if key == "additionalProperties":
            # Leaving the keyword out means ``true``.
            before = True if old_value is None else old_value
            after = True if new_value is None else new_value
            if before == after:
                return "ignore"
            if after is False:
                return "tightened"
            return "loosened" if before is False else "changed"
        if old_value is None or new_value is None:
            return "tightened" if new_value is not None else "loosened"
        if (
            key in _INCREASE_TIGHTENS
            and isinstance(old_value, (int, float))
            and isinstance(new_value, (int, float))
        ):
            if new_value > old_value:
                return "tightened"
            if new_value < old_value:
                return "loosened"
            return "ignore"
        if (
            key in _DECREASE_TIGHTENS
            and isinstance(old_value, (int, float))
            and isinstance(new_value, (int, float))
        ):
            if new_value < old_value:
                return "tightened"
            if new_value > old_value:
                return "loosened"
            return "ignore"
        if key == "uniqueItems":
            return "tightened" if new_value else "loosened"
        if key == "enum":
            return "changed"
        return "changed"

    def _rename_evidence(self, old_field: SchemaField, new_field: SchemaField) -> bool:
        """Whether a removed/added pair is strong enough evidence for a rename.

        Renames are only reported when there is evidence beyond "one field
        disappeared and another appeared with a similar type" — otherwise a
        legitimate removal plus an unrelated addition would be misreported as a
        rename by the tool that is supposed to warn about breaking changes.
        Accepted evidence: an Avro-style alias pointing at the other name, or a
        nested record whose field names substantially overlap.
        """
        if old_field.name in self._aliases(new_field) or new_field.name in self._aliases(old_field):
            return True
        if old_field.fields and new_field.fields:
            old_names = {child.name for child in old_field.fields}
            new_names = {child.name for child in new_field.fields}
            overlap = len(old_names & new_names)
            smallest = min(len(old_names), len(new_names))
            return overlap >= max(1, smallest // 2)
        return False

    def _pair_renames(
        self, removed: Sequence[SchemaField], added: Sequence[SchemaField]
    ) -> list[tuple[SchemaField, SchemaField]]:
        """Pair removed/added fields that are provably renames."""
        pairs: list[tuple[SchemaField, SchemaField]] = []
        used_added: set[str] = set()
        for old_field in removed:
            candidates = [
                new_field
                for new_field in added
                if new_field.name not in used_added
                and types_related(old_field.type, new_field.type)
                and self._rename_evidence(old_field, new_field)
            ]
            if len(candidates) != 1:
                continue
            pairs.append((old_field, candidates[0]))
            used_added.add(candidates[0].name)
        return pairs

    def _aliases(self, field: SchemaField) -> set[str]:
        aliases = field.constraints.get("aliases") or []
        return {str(alias) for alias in aliases} if isinstance(aliases, Iterable) else set()

    @staticmethod
    def _join(prefix: str, name: str) -> str:
        return f"{prefix}.{name}" if prefix else name

    def _change(
        self,
        change_type: ChangeType,
        path: str,
        description: str,
        impacts: _ImpactPair,
        mode: CompatibilityMode,
        *,
        old_value: object = None,
        new_value: object = None,
        field_type: str | None = None,
    ) -> SchemaChange:
        impact_map = {
            CompatibilityMode.BACKWARD.value: impacts[0],
            CompatibilityMode.FORWARD.value: impacts[1],
            CompatibilityMode.FULL.value: worst(*impacts),
        }
        return SchemaChange(
            change=change_type,
            path=path,
            description=description,
            mode=mode,
            impact=impact_map[mode.value],
            impacts=impact_map,
            old_value=old_value,
            new_value=new_value,
            field_type=field_type,
            remediation=REMEDIATION.get(change_type),
        )


class JsonSchemaComparer(StructuralComparer):
    """JSON Schema comparison.

    JSON documents are self-describing per field, so the structural rules apply
    directly: ``required`` drives presence, ``enum`` and constraint keywords
    drive the validation surface.
    """

    format: ClassVar[SchemaFormat] = SchemaFormat.JSON_SCHEMA


class OpenApiSchemaComparer(JsonSchemaComparer):
    """JSON Schema comparison for OpenAPI event payloads."""

    format: ClassVar[SchemaFormat] = SchemaFormat.OPENAPI


__all__ = [
    "CanonicalType",
    "JsonSchemaComparer",
    "OpenApiSchemaComparer",
    "StructuralComparer",
]
