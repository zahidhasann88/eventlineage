"""Canonical type vocabulary shared by parsers and the compatibility engine.

Each source dialect spells types differently (``int64`` vs ``long`` vs
``integer``); everything is lowered to the canonical names below so that a
Protobuf ``int64`` and an Avro ``long`` compare equal, and so widening rules
(``int`` -> ``long``) can be expressed once.
"""

from __future__ import annotations

from enum import StrEnum


class CanonicalType(StrEnum):
    """Normalized field types."""

    STRING = "string"
    INT = "int"
    LONG = "long"
    FLOAT = "float"
    DOUBLE = "double"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    BYTES = "bytes"
    NULL = "null"
    OBJECT = "object"
    RECORD = "record"
    ARRAY = "array"
    MAP = "map"
    ENUM = "enum"
    UNION = "union"
    DATE = "date"
    TIME = "time"
    TIMESTAMP = "timestamp"
    UUID = "uuid"
    ANY = "any"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


JSON_SCHEMA_TYPES: dict[str, CanonicalType] = {
    "string": CanonicalType.STRING,
    "integer": CanonicalType.INT,
    "number": CanonicalType.DOUBLE,
    "boolean": CanonicalType.BOOLEAN,
    "object": CanonicalType.OBJECT,
    "array": CanonicalType.ARRAY,
    "null": CanonicalType.NULL,
}

AVRO_TYPES: dict[str, CanonicalType] = {
    "string": CanonicalType.STRING,
    "int": CanonicalType.INT,
    "long": CanonicalType.LONG,
    "float": CanonicalType.FLOAT,
    "double": CanonicalType.DOUBLE,
    "boolean": CanonicalType.BOOLEAN,
    "bytes": CanonicalType.BYTES,
    "null": CanonicalType.NULL,
    "record": CanonicalType.RECORD,
    "error": CanonicalType.RECORD,
    "enum": CanonicalType.ENUM,
    "array": CanonicalType.ARRAY,
    "map": CanonicalType.MAP,
    "fixed": CanonicalType.BYTES,
    "union": CanonicalType.UNION,
}

PROTOBUF_TYPES: dict[str, CanonicalType] = {
    "double": CanonicalType.DOUBLE,
    "float": CanonicalType.FLOAT,
    "int32": CanonicalType.INT,
    "int64": CanonicalType.LONG,
    "uint32": CanonicalType.INT,
    "uint64": CanonicalType.LONG,
    "sint32": CanonicalType.INT,
    "sint64": CanonicalType.LONG,
    "fixed32": CanonicalType.INT,
    "fixed64": CanonicalType.LONG,
    "sfixed32": CanonicalType.INT,
    "sfixed64": CanonicalType.LONG,
    "bool": CanonicalType.BOOLEAN,
    "string": CanonicalType.STRING,
    "bytes": CanonicalType.BYTES,
    "google.protobuf.Timestamp": CanonicalType.TIMESTAMP,
    "google.protobuf.Duration": CanonicalType.STRING,
    "google.protobuf.Any": CanonicalType.ANY,
    "google.protobuf.Struct": CanonicalType.OBJECT,
    "google.protobuf.Empty": CanonicalType.OBJECT,
}

AVRO_LOGICAL_TYPES: dict[str, CanonicalType] = {
    "date": CanonicalType.DATE,
    "time-millis": CanonicalType.TIME,
    "time-micros": CanonicalType.TIME,
    "timestamp-millis": CanonicalType.TIMESTAMP,
    "timestamp-micros": CanonicalType.TIMESTAMP,
    "local-timestamp-millis": CanonicalType.TIMESTAMP,
    "local-timestamp-micros": CanonicalType.TIMESTAMP,
    "uuid": CanonicalType.UUID,
    "decimal": CanonicalType.DECIMAL,
}

JSON_SCHEMA_FORMATS: dict[str, CanonicalType] = {
    "date": CanonicalType.DATE,
    "time": CanonicalType.TIME,
    "date-time": CanonicalType.TIMESTAMP,
    "uuid": CanonicalType.UUID,
    "byte": CanonicalType.BYTES,
    "binary": CanonicalType.BYTES,
    "int64": CanonicalType.LONG,
}

#: Numeric widening order. Moving right is safe for readers of the older data,
#: moving left loses information.
NUMERIC_ORDER: tuple[CanonicalType, ...] = (
    CanonicalType.INT,
    CanonicalType.LONG,
    CanonicalType.FLOAT,
    CanonicalType.DOUBLE,
    CanonicalType.DECIMAL,
)

STRING_LIKE: frozenset[CanonicalType] = frozenset(
    {
        CanonicalType.STRING,
        CanonicalType.UUID,
        CanonicalType.DATE,
        CanonicalType.TIME,
        CanonicalType.TIMESTAMP,
    }
)

#: Types whose values are encoded as byte strings.
BINARY_LIKE: frozenset[CanonicalType] = frozenset({CanonicalType.BYTES, CanonicalType.STRING})

CONTAINER_TYPES: frozenset[CanonicalType] = frozenset(
    {CanonicalType.ARRAY, CanonicalType.MAP, CanonicalType.RECORD, CanonicalType.OBJECT}
)


def canonical(value: str) -> CanonicalType:
    """Best-effort mapping of an arbitrary type expression to a canonical type."""
    cleaned = value.strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        return CanonicalType.UNION
    try:
        return CanonicalType(cleaned)
    except ValueError:
        pass
    lowered = cleaned.lower()
    if lowered in {entry.value for entry in CanonicalType}:
        return CanonicalType(lowered)
    return CanonicalType.RECORD if cleaned[:1].isupper() else CanonicalType.ANY


def is_numeric(value: str) -> bool:
    """Whether a canonical type is numeric."""
    return canonical(value) in NUMERIC_ORDER


def widening_allowed(old: str, new: str) -> bool:
    """Whether changing a field type from ``old`` to ``new`` keeps compatibility."""
    old_type, new_type = canonical(old), canonical(new)
    if old_type == new_type:
        return True
    if old_type in NUMERIC_ORDER and new_type in NUMERIC_ORDER:
        return NUMERIC_ORDER.index(new_type) > NUMERIC_ORDER.index(old_type)
    return bool(old_type in STRING_LIKE and new_type in STRING_LIKE)


def types_related(old: str, new: str) -> bool:
    """Whether two types belong to the same family (potential rename detection)."""
    old_type, new_type = canonical(old), canonical(new)
    if old_type == new_type:
        return True
    if old_type in NUMERIC_ORDER and new_type in NUMERIC_ORDER:
        return True
    return old_type in STRING_LIKE and new_type in STRING_LIKE
