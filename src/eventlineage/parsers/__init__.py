"""Schema parsers: raw dialect documents -> normalized field trees."""

from __future__ import annotations

from typing import Any

from eventlineage.models.common import SchemaFormat
from eventlineage.models.schema import EventSchema, ParsedSchema
from eventlineage.parsers.avro import collect_named_types, parse_avro
from eventlineage.parsers.json_schema import parse_json_schema
from eventlineage.parsers.protobuf_parser import (
    ProtoEnum,
    ProtoFile,
    ProtoMessage,
    definition_from_file,
    event_messages,
    message_to_parsed,
    parse_proto_file,
    parse_protobuf,
)
from eventlineage.parsers.schema_types import (
    AVRO_TYPES,
    JSON_SCHEMA_TYPES,
    PROTOBUF_TYPES,
    CanonicalType,
    canonical,
    is_numeric,
    types_related,
    widening_allowed,
)


def detect_schema_format(definition: Any) -> SchemaFormat:
    """Best-effort dialect detection for definitions without an explicit format."""
    if isinstance(definition, str):
        stripped = definition.lstrip()
        if stripped.startswith("syntax") or "message " in stripped[:2000]:
            return SchemaFormat.PROTOBUF
        try:
            import json

            definition = json.loads(definition)
        except ValueError:
            return SchemaFormat.UNKNOWN
    if isinstance(definition, list):
        return SchemaFormat.AVRO
    if not isinstance(definition, dict):
        return SchemaFormat.UNKNOWN
    if "file" in definition and "message" in definition:
        return SchemaFormat.PROTOBUF
    if "syntax" in definition and ("messages" in definition or "package" in definition):
        return SchemaFormat.PROTOBUF
    if "$schema" in definition and "json-schema" in str(definition["$schema"]):
        return SchemaFormat.JSON_SCHEMA
    if "$id" in definition and ("properties" in definition or "definitions" in definition):
        return SchemaFormat.JSON_SCHEMA
    if definition.get("type") in {"record", "error", "enum", "fixed"} and (
        "fields" in definition or "symbols" in definition
    ):
        return SchemaFormat.AVRO
    if "properties" in definition or "$defs" in definition or "oneOf" in definition:
        return SchemaFormat.JSON_SCHEMA
    if "openapi" in definition or "swagger" in definition:
        return SchemaFormat.OPENAPI
    return SchemaFormat.UNKNOWN


def parse_schema(schema: EventSchema) -> ParsedSchema:
    """Lower an :class:`EventSchema` into normalized fields.

    Format inference runs when the schema does not declare one, and unsupported
    dialects degrade to an empty field tree rather than raising, so one odd file
    never breaks an entire scan.
    """
    definition = schema.definition if schema.definition is not None else schema.raw
    if definition is None:
        return ParsedSchema(format=schema.format)
    fmt = schema.format
    if fmt in {SchemaFormat.UNKNOWN, SchemaFormat.OPENAPI}:
        detected = detect_schema_format(definition)
        if detected == SchemaFormat.UNKNOWN and fmt == SchemaFormat.OPENAPI:
            detected = SchemaFormat.JSON_SCHEMA
        fmt = detected if detected != SchemaFormat.UNKNOWN else SchemaFormat.UNKNOWN

    if fmt == SchemaFormat.AVRO:
        return parse_avro(definition)
    if fmt == SchemaFormat.PROTOBUF:
        return parse_protobuf(definition)
    if fmt in {SchemaFormat.JSON_SCHEMA, SchemaFormat.OPENAPI}:
        return parse_json_schema(definition)
    detected = detect_schema_format(definition)
    if detected == SchemaFormat.UNKNOWN:
        return ParsedSchema(format=SchemaFormat.UNKNOWN)
    return parse_schema(EventSchema(format=detected, definition=definition))


def schema_format_of(schema: EventSchema | None, definition: Any) -> SchemaFormat:
    """Dialect of a schema, falling back to content detection."""
    if schema is not None and schema.format != SchemaFormat.UNKNOWN:
        return schema.format
    return detect_schema_format(definition)


__all__ = [
    "AVRO_TYPES",
    "JSON_SCHEMA_TYPES",
    "PROTOBUF_TYPES",
    "CanonicalType",
    "ProtoEnum",
    "ProtoFile",
    "ProtoMessage",
    "canonical",
    "collect_named_types",
    "definition_from_file",
    "detect_schema_format",
    "event_messages",
    "is_numeric",
    "message_to_parsed",
    "parse_avro",
    "parse_json_schema",
    "parse_proto_file",
    "parse_protobuf",
    "parse_schema",
    "schema_format_of",
    "types_related",
    "widening_allowed",
]
