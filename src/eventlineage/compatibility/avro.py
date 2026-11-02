"""Apache Avro compatibility rules.

Avro compares schemas using the reader/writer resolution rules: a reader needs
a default to fill fields the writer did not provide, and promotions are limited
to ``int -> long -> float -> double`` plus ``string <-> bytes``. Renames are
safe when the new schema declares an ``aliases`` entry.
"""

from __future__ import annotations

from typing import ClassVar

from eventlineage.compatibility.base import StructuralComparer, _ImpactPair, _pair
from eventlineage.compatibility.result import ChangeImpact
from eventlineage.models.common import SchemaFormat
from eventlineage.models.schema import SchemaField
from eventlineage.parsers.schema_types import CanonicalType, canonical, widening_allowed


class AvroComparer(StructuralComparer):
    """Avro field comparison (aliases, defaults, unions)."""

    format: ClassVar[SchemaFormat] = SchemaFormat.AVRO

    def is_required(self, field: SchemaField) -> bool:
        # In Avro a field with a default is optional from the resolver's point of
        # view; a field without default must be present in the writer's data.
        """Whether ``field`` must be present in every message."""
        return not field.has_default

    def added_field_impacts(self, field: SchemaField) -> _ImpactPair:
        """Impact of a field that exists only in the new schema."""
        if self.is_required(field):
            # A new reader cannot resolve a writer record that lacks the field.
            return _pair(ChangeImpact.BREAKING, ChangeImpact.COMPATIBLE)
        return _pair(ChangeImpact.COMPATIBLE, ChangeImpact.COMPATIBLE)

    def removed_field_impacts(self, field: SchemaField) -> _ImpactPair:
        """Impact of a field that exists only in the old schema."""
        if self.is_required(field):
            return _pair(ChangeImpact.COMPATIBLE, ChangeImpact.BREAKING)
        return _pair(ChangeImpact.COMPATIBLE, ChangeImpact.COMPATIBLE)

    def required_change_impacts(self, old: SchemaField, new: SchemaField) -> _ImpactPair:
        """Impact of a field becoming required or optional."""
        if not self.is_required(old) and self.is_required(new):
            return _pair(ChangeImpact.BREAKING, ChangeImpact.COMPATIBLE)
        return _pair(ChangeImpact.COMPATIBLE, ChangeImpact.COMPATIBLE)

    def type_change_impacts(self, old: SchemaField, new: SchemaField) -> _ImpactPair | None:
        """Impact of a type change, or ``None`` to compare structurally."""
        old_type, new_type = canonical(old.type), canonical(new.type)
        if old_type == new_type:
            return None
        if widening_allowed(old.type, new.type):
            return _pair(ChangeImpact.COMPATIBLE, ChangeImpact.POTENTIALLY_BREAKING)
        if widening_allowed(new.type, old.type):
            return _pair(ChangeImpact.BREAKING, ChangeImpact.COMPATIBLE)
        # Avro promotes between string and bytes in both directions.
        if {old_type, new_type} == {CanonicalType.STRING, CanonicalType.BYTES}:
            return _pair(ChangeImpact.COMPATIBLE, ChangeImpact.POTENTIALLY_BREAKING)
        return _pair(ChangeImpact.BREAKING, ChangeImpact.BREAKING)

    def enum_added_impacts(self) -> _ImpactPair:
        # A new reader knows every old symbol; an old reader fails on the new one
        # unless its enum declares a default.
        """Impact of an enum value added in the new schema."""
        return _pair(ChangeImpact.COMPATIBLE, ChangeImpact.BREAKING)

    def enum_removed_impacts(self) -> _ImpactPair:
        """Impact of an enum value removed in the new schema."""
        return _pair(ChangeImpact.BREAKING, ChangeImpact.COMPATIBLE)


__all__ = ["AvroComparer"]
