"""JSON Schema lowering.

Handles the subset of JSON Schema that event contracts actually use: object
properties, ``required``, ``enum``, arrays/``items``, ``$ref`` targets resolved
against local ``$defs``/``definitions``, ``allOf`` merging and ``oneOf``/
``anyOf`` variants. Constraints are preserved verbatim so the compatibility
engine can report narrowed/loosened validation rules.
"""

from __future__ import annotations

from typing import Any

from eventlineage.models.common import SchemaFormat
from eventlineage.models.schema import ParsedSchema, SchemaField
from eventlineage.parsers.schema_types import (
    JSON_SCHEMA_FORMATS,
    JSON_SCHEMA_TYPES,
    CanonicalType,
)

#: Validation keywords carried into ``SchemaField.constraints``.
CONSTRAINT_KEYWORDS: tuple[str, ...] = (
    "minLength",
    "maxLength",
    "pattern",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
    "const",
    "contentEncoding",
    "contentMediaType",
)

_REF_PREFIX = "#/"


def _resolve_ref(ref: str, root: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve a local JSON pointer reference; remote refs are left unresolved."""
    if not ref.startswith(_REF_PREFIX):
        return None
    node: Any = root
    for token in ref[2:].split("/"):
        if not token:
            continue
        segment = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            try:
                node = node[int(segment)]
            except (ValueError, IndexError):
                return None
        elif isinstance(node, dict) and segment in node:
            node = node[segment]
        else:
            return None
    return node if isinstance(node, dict) else None


def _merge_all_of(schema: dict[str, Any], root: dict[str, Any], depth: int) -> dict[str, Any]:
    if "allOf" not in schema or depth <= 0:
        return schema
    merged = {key: value for key, value in schema.items() if key != "allOf"}
    properties = dict(merged.get("properties") or {})
    required = list(merged.get("required") or [])
    for part in schema["allOf"]:
        candidate = _resolve_ref(part["$ref"], root) if "$ref" in part else part
        if not isinstance(candidate, dict):
            continue
        resolved = _merge_all_of(candidate, root, depth - 1)
        properties.update(resolved.get("properties") or {})
        for name in resolved.get("required") or []:
            if name not in required:
                required.append(name)
        for key, value in resolved.items():
            if key not in {"properties", "required"} and key not in merged:
                merged[key] = value
    if properties:
        merged["properties"] = properties
    if required:
        merged["required"] = required
    return merged


def _field_from_schema(
    name: str,
    schema: Any,
    *,
    root: dict[str, Any],
    required: bool,
    depth: int,
    seen: frozenset[str] = frozenset(),
) -> SchemaField:
    if not isinstance(schema, dict) or depth <= 0:
        return SchemaField(name=name, type=CanonicalType.ANY.value, required=required)

    if "$ref" in schema:
        reference = str(schema["$ref"])
        # A reference back into its own expansion (trees, linked lists) stops
        # here, exactly like an unresolved one.
        resolved = None if reference in seen else _resolve_ref(reference, root)
        if resolved is not None:
            seen = seen | {reference}
            merged = {**resolved, **{k: v for k, v in schema.items() if k != "$ref"}}
            schema = merged
        else:
            ref_name = reference.rsplit("/", 1)[-1]
            return SchemaField(
                name=name,
                type=CanonicalType.RECORD.value,
                required=required,
                raw_type=ref_name,
            )

    schema = _merge_all_of(schema, root, depth)
    raw_types = schema.get("type")
    type_names: list[str] = []
    if isinstance(raw_types, str):
        type_names = [raw_types]
    elif isinstance(raw_types, list):
        type_names = [str(entry) for entry in raw_types]

    nullable = "null" in type_names or schema.get("nullable") is True  # OpenAPI 3.0
    non_null = [entry for entry in type_names if entry != "null"]
    constraints: dict[str, Any] = {}
    for keyword in CONSTRAINT_KEYWORDS:
        if keyword in schema:
            constraints[keyword] = schema[keyword]
    if "format" in schema:
        constraints["format"] = schema["format"]
    if "additionalProperties" in schema:
        constraints["additionalProperties"] = schema["additionalProperties"]
    for keyword in ("x-alias", "x-aliases"):
        if keyword in schema:
            aliases = schema[keyword]
            constraints["aliases"] = [aliases] if isinstance(aliases, str) else list(aliases)

    enum_values: tuple[str, ...] = ()
    if isinstance(schema.get("enum"), list):
        enum_values = tuple(str(value) for value in schema["enum"])
    if "const" in schema and not enum_values:
        enum_values = (str(schema["const"]),)

    if "oneOf" in schema or "anyOf" in schema:
        variants = schema.get("oneOf") or schema.get("anyOf") or []
        children: tuple[SchemaField, ...] = tuple(
            _field_from_schema(
                f"variant{position}",
                variant,
                root=root,
                required=True,
                depth=depth - 1,
                seen=seen,
            )
            for position, variant in enumerate(variants)
            if isinstance(variant, dict)
        )
        return SchemaField(
            name=name,
            type=CanonicalType.UNION.value,
            required=required,
            nullable=nullable,
            enum_values=enum_values,
            constraints=constraints,
            fields=children,
            raw_type="oneOf" if "oneOf" in schema else "anyOf",
        )

    if enum_values:
        field_type = CanonicalType.ENUM
    elif not type_names:
        field_type = (
            CanonicalType.ARRAY
            if "items" in schema
            else (CanonicalType.OBJECT if "properties" in schema else CanonicalType.ANY)
        )
    else:
        field_type = JSON_SCHEMA_TYPES.get(non_null[0] if non_null else "null", CanonicalType.ANY)
        if field_type == CanonicalType.STRING and "format" in schema:
            field_type = JSON_SCHEMA_FORMATS.get(str(schema["format"]), CanonicalType.STRING)

    children = ()
    repeated = False
    if field_type == CanonicalType.ARRAY:
        items = schema.get("items")
        repeated = True
        if isinstance(items, dict):
            item_field = _field_from_schema(
                "items", items, root=root, required=True, depth=depth - 1, seen=seen
            )
            children = (item_field,)
        elif isinstance(items, list) and items:
            children = tuple(
                _field_from_schema(
                    f"items{position}",
                    option,
                    root=root,
                    required=True,
                    depth=depth - 1,
                    seen=seen,
                )
                for position, option in enumerate(items)
                if isinstance(option, dict)
            )
    elif field_type in {CanonicalType.OBJECT, CanonicalType.RECORD} or "properties" in schema:
        field_type = CanonicalType.OBJECT if field_type != CanonicalType.RECORD else field_type
        properties = schema.get("properties") or {}
        if isinstance(properties, dict):
            required_names = set(schema.get("required") or [])
            children = tuple(
                _field_from_schema(
                    str(prop_name),
                    prop_schema,
                    root=root,
                    required=str(prop_name) in required_names,
                    depth=depth - 1,
                    seen=seen,
                )
                for prop_name, prop_schema in properties.items()
            )

    default_present = "default" in schema
    return SchemaField(
        name=name,
        type=field_type.value,
        required=required,
        nullable=nullable,
        repeated=repeated,
        has_default=default_present,
        default=schema.get("default"),
        enum_values=enum_values,
        constraints=constraints,
        fields=children,
        doc=schema.get("description"),
        raw_type=",".join(type_names) or None,
    )


def parse_json_schema(definition: Any, *, root: dict[str, Any] | None = None) -> ParsedSchema:
    """Lower a JSON Schema document into normalized fields.

    ``definition`` may be the schema document itself or a wrapper
    ``{"schema": {...}}`` as produced by some schema-registry exports.
    """
    if isinstance(definition, str):
        import json

        try:
            definition = json.loads(definition)
        except json.JSONDecodeError:
            return ParsedSchema(format=SchemaFormat.JSON_SCHEMA, root_type="object")
    if (
        isinstance(definition, dict)
        and "schema" in definition
        and isinstance(definition["schema"], dict)
    ):
        candidate = definition["schema"]
        if "$schema" in candidate or "properties" in candidate or "type" in candidate:
            definition = candidate
    if not isinstance(definition, dict):
        return ParsedSchema(format=SchemaFormat.JSON_SCHEMA, root_type="object")

    root_document = root if root is not None else definition
    title = definition.get("title")
    # Generators routinely emit ``{"$ref": "#/$defs/Order", "$defs": {...}}`` or a
    # root ``allOf``: the fields live behind them, not at the top level.
    seen: frozenset[str] = frozenset()
    while "$ref" in definition and str(definition["$ref"]) not in seen:
        reference = str(definition["$ref"])
        target = _resolve_ref(reference, root_document)
        if target is None:
            break
        seen = seen | {reference}
        definition = {**target, **{k: v for k, v in definition.items() if k != "$ref"}}
    definition = _merge_all_of(definition, root_document, 32)
    root_type = str(definition.get("type") or ("object" if "properties" in definition else "any"))
    required_names = set(definition.get("required") or [])
    properties = definition.get("properties") or {}
    fields: tuple[SchemaField, ...] = ()
    if isinstance(properties, dict):
        fields = tuple(
            _field_from_schema(
                str(prop_name),
                prop_schema,
                root=root_document,
                required=str(prop_name) in required_names,
                depth=32,
                seen=seen,
            )
            for prop_name, prop_schema in properties.items()
        )
    return ParsedSchema(
        format=SchemaFormat.JSON_SCHEMA,
        root_type=root_type,
        fields=fields,
        name=title or definition.get("title"),
        metadata={
            "schema_uri": definition.get("$schema"),
            "required": sorted(required_names),
            "additional_properties": definition.get("additionalProperties"),
        },
    )
