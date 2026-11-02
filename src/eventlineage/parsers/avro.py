"""Apache Avro schema lowering.

Supports records, nested records, enums, arrays, maps, fixed, unions and
logical types. Named-type references are resolved through an optional registry
(``_registry`` inside the definition) that discovery populates when a single
file declares several records — the shape produced by Schema Registry exports.
"""

from __future__ import annotations

import json
from typing import Any

from eventlineage.models.common import SchemaFormat
from eventlineage.models.schema import ParsedSchema, SchemaField
from eventlineage.parsers.schema_types import AVRO_LOGICAL_TYPES, AVRO_TYPES, CanonicalType

_PRIMITIVES = frozenset(
    {"string", "int", "long", "float", "double", "boolean", "bytes", "null", "void"}
)
_MAX_DEPTH = 24


def _type_of(schema: Any) -> str:
    if isinstance(schema, str):
        return AVRO_TYPES.get(
            schema, CanonicalType.RECORD if schema[:1].isupper() else CanonicalType.ANY
        ).value
    if isinstance(schema, list):
        return CanonicalType.UNION.value
    if isinstance(schema, dict):
        raw = schema.get("type")
        if isinstance(raw, list):
            return CanonicalType.UNION.value
        if isinstance(raw, str):
            base = AVRO_TYPES.get(raw, CanonicalType.ANY)
            logical = schema.get("logicalType")
            if isinstance(logical, str) and logical in AVRO_LOGICAL_TYPES:
                return AVRO_LOGICAL_TYPES[logical].value
            return base.value
    return CanonicalType.ANY.value


def _constraints(schema: dict[str, Any]) -> dict[str, Any]:
    keys = ("logicalType", "precision", "scale", "size", "aliases", "order", "doc")
    return {key: schema[key] for key in keys if key in schema}


def _apply_entry_metadata(field: SchemaField, entry: dict[str, Any]) -> SchemaField:
    """Carry record-field level metadata (``doc``, ``aliases``, ``order``) onto the field."""
    update: dict[str, Any] = {}
    if isinstance(entry.get("doc"), str):
        update["doc"] = entry["doc"]
    aliases = entry.get("aliases")
    if isinstance(aliases, list) and aliases:
        update["constraints"] = {**field.constraints, "aliases": [str(alias) for alias in aliases]}
    if "default" in entry:
        update["has_default"] = True
        update["default"] = entry["default"]
        # Avro resolves a missing writer field through its default, so such a
        # field is optional in the compatibility sense.
        update["required"] = False
    return field.model_copy(update=update) if update else field


def _field_from_type(
    name: str,
    schema: Any,
    *,
    required: bool,
    registry: dict[str, Any],
    depth: int,
) -> SchemaField:
    if depth <= 0:
        return SchemaField(name=name, type=CanonicalType.ANY.value, required=required)
    nullable = False
    if isinstance(schema, list):
        options = [option for option in schema if option != "null"]
        nullable = len(options) != len(schema)
        if len(options) == 1:
            field = _field_from_type(
                name, options[0], required=required, registry=registry, depth=depth - 1
            )
            field.nullable = nullable
            field.required = required
            return field
        children: tuple[SchemaField, ...] = tuple(
            _field_from_type(
                f"variant{position}", option, required=True, registry=registry, depth=depth - 1
            )
            for position, option in enumerate(options)
        )
        return SchemaField(
            name=name,
            type=CanonicalType.UNION.value,
            required=required,
            nullable=nullable,
            fields=children,
            raw_type=json.dumps(schema),
        )
    if isinstance(schema, str):
        if schema in registry and schema not in _PRIMITIVES:
            resolved = registry[schema]
            field = _field_from_type(
                name, resolved, required=required, registry=registry, depth=depth - 1
            )
            field.raw_type = schema
            return field
        return SchemaField(name=name, type=_type_of(schema), required=required, raw_type=schema)
    if not isinstance(schema, dict):
        return SchemaField(name=name, type=CanonicalType.ANY.value, required=required)

    raw_type = schema.get("type")
    if isinstance(raw_type, str) and raw_type in registry and raw_type not in _PRIMITIVES:
        resolved = {**registry[raw_type], **schema}
        return _field_from_type(
            name, resolved, required=required, registry=registry, depth=depth - 1
        )
    if isinstance(raw_type, list):
        return _field_from_type(
            name, raw_type, required=required, registry=registry, depth=depth - 1
        )

    field_type = _type_of(schema)
    canonical = (
        CanonicalType(field_type)
        if field_type in {t.value for t in CanonicalType}
        else CanonicalType.ANY
    )
    constraints = _constraints(schema)
    children = ()
    enum_values: tuple[str, ...] = ()
    repeated = False
    doc = schema.get("doc") if isinstance(schema.get("doc"), str) else None

    if canonical == CanonicalType.RECORD:
        record_fields = schema.get("fields") or []
        built: list[SchemaField] = []
        for entry in record_fields:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            child = _field_from_type(
                str(entry.get("name")),
                entry.get("type"),
                required="default" not in entry,
                registry=registry,
                depth=depth - 1,
            )
            built.append(_apply_entry_metadata(child, entry))
        children = tuple(built)
    elif canonical == CanonicalType.ENUM:
        enum_values = tuple(str(symbol) for symbol in schema.get("symbols") or [])
    elif canonical == CanonicalType.ARRAY:
        repeated = True
        item = schema.get("items")
        if item is not None:
            children = (
                _field_from_type("items", item, required=True, registry=registry, depth=depth - 1),
            )
    elif canonical == CanonicalType.MAP:
        values = schema.get("values")
        if values is not None:
            children = (
                _field_from_type(
                    "values", values, required=True, registry=registry, depth=depth - 1
                ),
            )
    return SchemaField(
        name=name,
        type=canonical.value,
        required=required,
        nullable=nullable,
        repeated=repeated,
        enum_values=enum_values,
        constraints=constraints,
        fields=children,
        doc=doc,
        raw_type=str(raw_type) if isinstance(raw_type, str) else None,
    )


def collect_named_types(node: Any, out: dict[str, Any] | None = None) -> dict[str, Any]:
    """Collect every named type reachable from ``node`` (records/enums/fixed)."""
    registry: dict[str, Any] = {} if out is None else out
    if isinstance(node, list):
        for entry in node:
            collect_named_types(entry, registry)
        return registry
    if not isinstance(node, dict):
        return registry
    name = node.get("name")
    if isinstance(name, str) and node.get("type") in {"record", "error", "enum", "fixed"}:
        registry[name] = node
        namespace = node.get("namespace")
        if namespace:
            registry[f"{namespace}.{name}"] = node
    for key in ("fields",):
        for entry in node.get(key) or []:
            if isinstance(entry, dict):
                collect_named_types(entry.get("type"), registry)
    for key in ("items", "values"):
        if key in node:
            collect_named_types(node[key], registry)
    for key in ("type",):
        if isinstance(node.get(key), list):
            collect_named_types(node[key], registry)
    return registry


def parse_avro(definition: Any) -> ParsedSchema:
    """Lower an Avro record definition into normalized fields."""
    if isinstance(definition, str):
        try:
            definition = json.loads(definition)
        except json.JSONDecodeError:
            return ParsedSchema(format=SchemaFormat.AVRO, root_type="record")
    registry_override: dict[str, Any] = {}
    if isinstance(definition, dict) and "_registry" in definition:
        definition = dict(definition)
        registry_override = definition.pop("_registry") or {}
    if isinstance(definition, list):
        # A bare list of named types: promote the first record to the root.
        records = [entry for entry in definition if isinstance(entry, dict)]
        definition = records[0] if records else {}
    if not isinstance(definition, dict):
        return ParsedSchema(format=SchemaFormat.AVRO, root_type="record")

    registry = collect_named_types(definition)
    registry.update(registry_override)

    node = definition
    if isinstance(node.get("type"), dict):
        node = node["type"]
    root_type = str(node.get("type") or "record")
    name = node.get("name")
    namespace = node.get("namespace")
    fields_node = node.get("fields") or node.get("values") or []
    fields: tuple[SchemaField, ...] = ()
    if isinstance(fields_node, list):
        fields = tuple(
            _apply_entry_metadata(
                _field_from_type(
                    str(entry.get("name")),
                    entry.get("type"),
                    required="default" not in entry,
                    registry=registry,
                    depth=_MAX_DEPTH,
                ),
                entry,
            )
            for entry in fields_node
            if isinstance(entry, dict) and entry.get("name") is not None
        )
    elif isinstance(fields_node, dict):
        fields = tuple(
            _field_from_type(name_key, value, required=False, registry=registry, depth=_MAX_DEPTH)
            for name_key, value in fields_node.items()
        )
    return ParsedSchema(
        format=SchemaFormat.AVRO,
        root_type=root_type,
        fields=fields,
        name=f"{namespace}.{name}" if namespace and name else name,
        metadata={
            "namespace": namespace,
            "aliases": node.get("aliases") or [],
            "doc": node.get("doc"),
        },
    )
