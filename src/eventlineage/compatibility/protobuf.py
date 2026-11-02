"""Protobuf compatibility rules.

Protobuf compatibility is a *wire* question, not a logical-type question:
field numbers identify fields, and only types in the same wire group can be
changed safely. Renaming a field is harmless (unless JSON encoding is used),
while reusing a field number is a cardinal sin that silently corrupts data.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from eventlineage.compatibility.base import StructuralComparer, _ImpactPair, _pair
from eventlineage.compatibility.result import (
    ChangeImpact,
    ChangeType,
    CompatibilityMode,
    CompatibilityReport,
    SchemaChange,
)
from eventlineage.models.common import SchemaFormat
from eventlineage.models.schema import ParsedSchema, SchemaField
from eventlineage.parsers.protobuf_parser import (
    ProtoFile,
    ProtoMessage,
    message_to_parsed,
    parse_proto_file,
)
from eventlineage.parsers.schema_types import PROTOBUF_TYPES, CanonicalType, canonical

#: Types grouped by protobuf wire representation.
WIRE_GROUPS: dict[str, str] = {
    "int32": "varint",
    "int64": "varint",
    "uint32": "varint",
    "uint64": "varint",
    "sint32": "varint",
    "sint64": "varint",
    "bool": "varint",
    "fixed32": "fixed32",
    "sfixed32": "fixed32",
    "float": "fixed32",
    "fixed64": "fixed64",
    "sfixed64": "fixed64",
    "double": "fixed64",
    "string": "bytes",
    "bytes": "bytes",
}

_SAFE_RENUMBERLESS = frozenset({"bool", "int32", "int64", "uint32", "uint64", "sint32", "sint64"})
#: ``sint*`` is zigzag encoded, so it shares a wire group with ``int*`` but not a value.
_ZIGZAG = frozenset({"sint32", "sint64"})
#: Same width as ``fixed*``, but the bits mean something else entirely.
_FLOATING = frozenset({"float", "double"})


def wire_group(field: SchemaField) -> str:
    """Wire representation of a field type."""
    if field.type in {"record", "map", "enum"}:
        if field.type == "enum":
            return "varint"
        return "bytes"
    raw = (field.raw_type or "").strip()
    if raw in WIRE_GROUPS:
        return WIRE_GROUPS[raw]
    canonical_type = canonical(field.type)
    if canonical_type in {CanonicalType.INT, CanonicalType.LONG, CanonicalType.BOOLEAN}:
        return "varint"
    if canonical_type in {CanonicalType.FLOAT, CanonicalType.DOUBLE, CanonicalType.DECIMAL}:
        return "fixed64"
    if canonical_type in {CanonicalType.STRING, CanonicalType.BYTES}:
        return "bytes"
    if canonical_type in {CanonicalType.RECORD, CanonicalType.OBJECT, CanonicalType.MAP}:
        return "bytes"
    return "unknown"


class ProtobufComparer(StructuralComparer):
    """Protobuf comparison on the wire representation."""

    format: ClassVar[SchemaFormat] = SchemaFormat.PROTOBUF

    def is_required(self, field: SchemaField) -> bool:
        """Whether ``field`` must be present in every message."""
        return str(field.constraints.get("label", "")).lower() == "required"

    def added_field_impacts(self, field: SchemaField) -> _ImpactPair:
        """Impact of a field that exists only in the new schema."""
        if self.is_required(field):
            return _pair(ChangeImpact.BREAKING, ChangeImpact.COMPATIBLE)
        # Unknown fields are preserved, adding a field is safe in both directions.
        return _pair(ChangeImpact.COMPATIBLE, ChangeImpact.COMPATIBLE)

    def removed_field_impacts(self, field: SchemaField) -> _ImpactPair:
        """Impact of a field that exists only in the old schema."""
        if self.is_required(field):
            return _pair(ChangeImpact.COMPATIBLE, ChangeImpact.BREAKING)
        return _pair(ChangeImpact.COMPATIBLE, ChangeImpact.POTENTIALLY_BREAKING)

    def rename_impacts(self, old: SchemaField, new: SchemaField) -> _ImpactPair:
        # Protobuf fields are identified by number, names only matter for JSON.
        """Impact of a rename, honouring declared aliases."""
        if old.constraints.get("number") == new.constraints.get("number"):
            return _pair(ChangeImpact.POTENTIALLY_BREAKING, ChangeImpact.POTENTIALLY_BREAKING)
        return _pair(ChangeImpact.BREAKING, ChangeImpact.BREAKING)

    def type_change_impacts(self, old: SchemaField, new: SchemaField) -> _ImpactPair | None:
        """Impact of a type change, or ``None`` to compare structurally."""
        if canonical(old.type) == canonical(new.type) and old.raw_type == new.raw_type:
            return None
        old_group, new_group = wire_group(old), wire_group(new)
        if (old.raw_type in _ZIGZAG) != (new.raw_type in _ZIGZAG) and old_group == "varint":
            return _pair(ChangeImpact.BREAKING, ChangeImpact.BREAKING)
        if (old.raw_type in _FLOATING) != (new.raw_type in _FLOATING):
            return _pair(ChangeImpact.BREAKING, ChangeImpact.BREAKING)
        if old_group == new_group and old_group != "unknown":
            if old.raw_type in _SAFE_RENUMBERLESS and new.raw_type in _SAFE_RENUMBERLESS:
                return _pair(ChangeImpact.COMPATIBLE, ChangeImpact.POTENTIALLY_BREAKING)
            return _pair(ChangeImpact.POTENTIALLY_BREAKING, ChangeImpact.POTENTIALLY_BREAKING)
        return _pair(ChangeImpact.BREAKING, ChangeImpact.BREAKING)

    def enum_added_impacts(self) -> _ImpactPair:
        # proto3 enums are open: unknown values are preserved by generated code.
        """Impact of an enum value added in the new schema."""
        return _pair(ChangeImpact.COMPATIBLE, ChangeImpact.POTENTIALLY_BREAKING)

    def enum_removed_impacts(self) -> _ImpactPair:
        """Impact of an enum value removed in the new schema."""
        return _pair(ChangeImpact.POTENTIALLY_BREAKING, ChangeImpact.POTENTIALLY_BREAKING)

    def compare_files(
        self,
        old_text: str,
        new_text: str,
        mode: CompatibilityMode = CompatibilityMode.BACKWARD,
        *,
        old_name: str | None = None,
        new_name: str | None = None,
        old_hash: str | None = None,
        new_hash: str | None = None,
    ) -> CompatibilityReport:
        """Compare every top-level message of two ``.proto`` files, matched by name.

        Nested messages are compared where a field uses them. Paths gain the message
        name only when a file holds several, so a single-message file reads as before.
        """
        old_file, new_file = parse_proto_file(old_text), parse_proto_file(new_text)
        old_messages = {message.name: message for message in old_file.messages}
        new_messages = {message.name: message for message in new_file.messages}
        qualify = len(old_messages.keys() | new_messages.keys()) > 1
        changes: list[SchemaChange] = []

        for name in sorted(old_messages.keys() | new_messages.keys()):
            if name not in old_messages:
                changes.append(self._message_change(ChangeType.FIELD_ADDED, name, mode))
            elif name not in new_messages:
                changes.append(self._message_change(ChangeType.FIELD_REMOVED, name, mode))
            else:
                report = self.compare(
                    self._lowered(old_messages[name], old_file),
                    self._lowered(new_messages[name], new_file),
                    mode,
                )
                for change in report.changes:
                    path = f"{name}.{change.path}".rstrip(".") if qualify else change.path
                    changes.append(change.model_copy(update={"path": path}))

        return CompatibilityReport(
            format=self.format,
            mode=mode,
            old_name=old_name,
            new_name=new_name,
            old_hash=old_hash,
            new_hash=new_hash,
            changes=tuple(sorted(changes, key=lambda change: (change.path, change.change.value))),
        )

    @staticmethod
    def _lowered(message: ProtoMessage, proto_file: ProtoFile) -> ParsedSchema:
        return message_to_parsed(message, proto_file)

    def _message_change(
        self, change: ChangeType, name: str, mode: CompatibilityMode
    ) -> SchemaChange:
        if change is ChangeType.FIELD_ADDED:
            return self._change(
                change,
                name,
                f"message {name!r} was added",
                _pair(ChangeImpact.COMPATIBLE, ChangeImpact.COMPATIBLE),
                mode,
                new_value=name,
                field_type="record",
            )
        # Fields that used the message report their own change; on its own a removal
        # only matters to code generated from the file.
        return self._change(
            change,
            name,
            f"message {name!r} was removed",
            _pair(ChangeImpact.POTENTIALLY_BREAKING, ChangeImpact.POTENTIALLY_BREAKING),
            mode,
            old_value=name,
            field_type="record",
        )

    def _compare_fields(
        self,
        old_fields: Sequence[SchemaField],
        new_fields: Sequence[SchemaField],
        prefix: str,
        changes: list[SchemaChange],
        mode: CompatibilityMode,
    ) -> None:
        old_by_name = {field.name: field for field in old_fields}
        new_by_name = {field.name: field for field in new_fields}
        old_numbers = {
            field.constraints.get("number"): field
            for field in old_fields
            if field.constraints.get("number") is not None
        }
        new_numbers = {
            field.constraints.get("number"): field
            for field in new_fields
            if field.constraints.get("number") is not None
        }

        for name in sorted(set(old_by_name) & set(new_by_name)):
            old_field, new_field = old_by_name[name], new_by_name[name]
            old_number = old_field.constraints.get("number")
            new_number = new_field.constraints.get("number")
            if old_number is not None and new_number is not None and old_number != new_number:
                path = self._join(prefix, name)
                changes.append(
                    self._change(
                        ChangeType.FIELD_NUMBER_CHANGED,
                        path,
                        f"field {name!r} changed number from {old_number} to {new_number}",
                        _pair(ChangeImpact.BREAKING, ChangeImpact.BREAKING),
                        mode,
                        old_value=old_number,
                        new_value=new_number,
                    )
                )

        # The same number under a new name is a rename when the wire format still
        # agrees, and a reused number when it does not.
        renames: dict[str, str] = {}
        for number, old_field in sorted(old_numbers.items(), key=lambda item: str(item[0])):
            renamed = new_numbers.get(number)
            if renamed is None or renamed.name == old_field.name:
                continue
            path = self._join(prefix, renamed.name)
            impacts = self.type_change_impacts(old_field, renamed)
            names_free = renamed.name not in old_by_name and old_field.name not in new_by_name
            if names_free and (impacts is None or ChangeImpact.BREAKING not in impacts):
                renames[old_field.name] = renamed.name
                changes.append(
                    self._change(
                        ChangeType.FIELD_RENAMED,
                        path,
                        f"field number {number} renamed from {old_field.name!r} to "
                        f"{renamed.name!r}; harmless on the wire, visible in JSON encoding",
                        _pair(ChangeImpact.POTENTIALLY_BREAKING, ChangeImpact.POTENTIALLY_BREAKING),
                        mode,
                        old_value=old_field.name,
                        new_value=renamed.name,
                        field_type=renamed.type,
                    )
                )
                continue
            changes.append(
                self._change(
                    ChangeType.FIELD_NUMBER_CHANGED,
                    path,
                    f"field number {number} reused: was {old_field.name!r}, now {renamed.name!r}",
                    _pair(ChangeImpact.BREAKING, ChangeImpact.BREAKING),
                    mode,
                    old_value=old_field.name,
                    new_value=renamed.name,
                )
            )

        # Numbers are validated above; strip them so the structural pass does not
        # report them again as generic constraint changes. Renamed fields take
        # their new name so the rest of the field is still compared.
        stripped_old = [
            self._without_number(field).model_copy(
                update={"name": renames.get(field.name, field.name)}
            )
            for field in old_fields
        ]
        stripped_new = [self._without_number(field) for field in new_fields]
        super()._compare_fields(stripped_old, stripped_new, prefix, changes, mode)

    @staticmethod
    def _without_number(field: SchemaField) -> SchemaField:
        if "number" not in field.constraints:
            return field
        return field.model_copy(
            update={
                "constraints": {
                    key: value for key, value in field.constraints.items() if key != "number"
                }
            }
        )


__all__ = ["PROTOBUF_TYPES", "WIRE_GROUPS", "ProtobufComparer", "wire_group"]
