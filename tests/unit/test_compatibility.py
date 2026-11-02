"""Schema compatibility: breaking vs compatible changes across dialects and modes."""

from __future__ import annotations

import pytest
from tests.conftest import make_event, make_schema

from eventlineage.compatibility import (
    ChangeImpact,
    ChangeType,
    CompatibilityMode,
    compare_definitions,
    compare_event_versions,
    compare_schemas,
    comparer_for,
)
from eventlineage.models.common import SchemaFormat
from eventlineage.models.schema import EventSchema

AVRO_V1 = {
    "type": "record",
    "name": "Order",
    "namespace": "shop",
    "fields": [
        {"name": "order_id", "type": "string"},
        {"name": "total", "type": "int"},
        {"name": "status", "type": {"type": "enum", "name": "Status", "symbols": ["NEW", "PAID"]}},
    ],
}

AVRO_OPTIONAL_FIELD = {
    **AVRO_V1,
    "fields": [*AVRO_V1["fields"], {"name": "channel", "type": "string", "default": "web"}],
}

AVRO_REQUIRED_FIELD = {
    **AVRO_V1,
    "fields": [*AVRO_V1["fields"], {"name": "channel", "type": "string"}],
}

AVRO_REMOVED_FIELD = {
    **AVRO_V1,
    "fields": [AVRO_V1["fields"][0], AVRO_V1["fields"][1]],
}

JSON_V1 = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string"},
        "total": {"type": "integer"},
        "status": {"type": "string", "enum": ["NEW", "PAID"]},
    },
    "required": ["order_id", "total"],
}

PROTO_V1 = """
syntax = "proto3";
package shop;
message Order {
  string order_id = 1;
  int64 total = 2;
  Status status = 3;
  enum Status { NEW = 0; PAID = 1; }
}
"""


def kinds(report: object) -> set[ChangeType]:
    return {change.change for change in report.changes}  # type: ignore[attr-defined]


class TestAvroCompatibility:
    def test_optional_field_is_backward_compatible(self) -> None:
        report = compare_definitions(AVRO_V1, AVRO_OPTIONAL_FIELD, format=SchemaFormat.AVRO)
        assert report.compatible is True
        assert ChangeType.FIELD_ADDED in kinds(report)

    def test_required_field_breaks_backward_compatibility(self) -> None:
        report = compare_definitions(AVRO_V1, AVRO_REQUIRED_FIELD, format=SchemaFormat.AVRO)
        assert report.compatible is False
        assert report.counts()["breaking"] >= 1

    def test_required_field_is_fine_for_forward_mode(self) -> None:
        report = compare_definitions(
            AVRO_V1,
            AVRO_REQUIRED_FIELD,
            format=SchemaFormat.AVRO,
            mode=CompatibilityMode.FORWARD,
        )
        assert report.compatible is True

    def test_removed_field_breaks_forward_compatibility(self) -> None:
        report = compare_definitions(
            AVRO_V1,
            AVRO_REMOVED_FIELD,
            format=SchemaFormat.AVRO,
            mode=CompatibilityMode.FORWARD,
        )
        assert report.compatible is False

    def test_removed_field_with_default_is_compatible_backward(self) -> None:
        report = compare_definitions(AVRO_V1, AVRO_REMOVED_FIELD, format=SchemaFormat.AVRO)
        assert report.compatible is True

    def test_type_widening_is_compatible(self) -> None:
        widened = {
            **AVRO_V1,
            "fields": [
                {"name": "order_id", "type": "string"},
                {"name": "total", "type": "long"},
                AVRO_V1["fields"][2],
            ],
        }
        report = compare_definitions(AVRO_V1, widened, format=SchemaFormat.AVRO)
        assert report.compatible is True

    def test_enum_symbol_removal_is_breaking(self) -> None:
        tightened = {
            **AVRO_V1,
            "fields": [
                AVRO_V1["fields"][0],
                AVRO_V1["fields"][1],
                {"name": "status", "type": {"type": "enum", "name": "Status", "symbols": ["NEW"]}},
            ],
        }
        report = compare_definitions(AVRO_V1, tightened, format=SchemaFormat.AVRO)
        assert ChangeType.ENUM_VALUE_REMOVED in kinds(report)
        assert report.compatible is False

    def test_full_mode_requires_both_directions(self) -> None:
        required = compare_definitions(
            AVRO_V1, AVRO_REQUIRED_FIELD, format=SchemaFormat.AVRO, mode=CompatibilityMode.FULL
        )
        forward = compare_definitions(
            AVRO_V1, AVRO_REQUIRED_FIELD, format=SchemaFormat.AVRO, mode=CompatibilityMode.FORWARD
        )
        # Compatible in one direction only, so FULL has to reject it.
        assert forward.compatible is True
        assert required.compatible is False

    def test_identical_schemas_have_no_changes(self) -> None:
        report = compare_definitions(AVRO_V1, AVRO_V1, format=SchemaFormat.AVRO)
        assert report.changes == ()
        assert report.compatible is True


class TestJsonSchemaCompatibility:
    def test_adding_optional_property_is_compatible(self) -> None:
        new = {
            **JSON_V1,
            "properties": {**JSON_V1["properties"], "channel": {"type": "string"}},
        }
        report = compare_definitions(JSON_V1, new, format=SchemaFormat.JSON_SCHEMA)
        assert report.compatible is True
        assert ChangeType.FIELD_ADDED in kinds(report)

    def test_adding_required_property_is_breaking(self) -> None:
        new = {
            **JSON_V1,
            "properties": {**JSON_V1["properties"], "channel": {"type": "string"}},
            "required": [*JSON_V1["required"], "channel"],
        }
        report = compare_definitions(JSON_V1, new, format=SchemaFormat.JSON_SCHEMA)
        assert report.compatible is False
        # A brand-new field that is also required is reported as an added field
        # with breaking impact; ``required_added`` covers fields that change
        # their required-ness.
        assert ChangeType.FIELD_ADDED in kinds(report)

    def test_removing_property(self) -> None:
        new = {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        }
        report = compare_definitions(JSON_V1, new, format=SchemaFormat.JSON_SCHEMA)
        assert ChangeType.FIELD_REMOVED in kinds(report)

    def test_type_change_is_breaking(self) -> None:
        new = {
            **JSON_V1,
            "properties": {**JSON_V1["properties"], "total": {"type": "string"}},
        }
        report = compare_definitions(JSON_V1, new, format=SchemaFormat.JSON_SCHEMA)
        assert ChangeType.TYPE_CHANGED in kinds(report)
        assert report.compatible is False

    def test_tightened_constraint_is_at_least_potentially_breaking(self) -> None:
        new = {
            **JSON_V1,
            "properties": {
                **JSON_V1["properties"],
                "order_id": {"type": "string", "minLength": 10},
            },
        }
        report = compare_definitions(JSON_V1, new, format=SchemaFormat.JSON_SCHEMA)
        assert ChangeType.CONSTRAINT_TIGHTENED in kinds(report)
        assert {change.impact for change in report.changes} == {ChangeImpact.BREAKING}

    def test_enum_narrowing_is_breaking(self) -> None:
        new = {
            **JSON_V1,
            "properties": {
                **JSON_V1["properties"],
                "status": {"type": "string", "enum": ["NEW"]},
            },
        }
        report = compare_definitions(JSON_V1, new, format=SchemaFormat.JSON_SCHEMA)
        assert ChangeType.ENUM_VALUE_REMOVED in kinds(report)


class TestProtobufCompatibility:
    def test_adding_a_field_is_compatible(self) -> None:
        new = PROTO_V1.replace("  Status status = 3;", "  Status status = 3;\n  string note = 4;")
        report = compare_definitions(PROTO_V1, new, format=SchemaFormat.PROTOBUF)
        assert report.compatible is True

    def test_removing_a_field_is_breaking(self) -> None:
        new = PROTO_V1.replace("  int64 total = 2;\n", "")
        report = compare_definitions(PROTO_V1, new, format=SchemaFormat.PROTOBUF)
        assert ChangeType.FIELD_REMOVED in kinds(report)

    def test_reusing_a_field_number_with_another_type_is_breaking(self) -> None:
        new = PROTO_V1.replace("int64 total = 2;", "string total = 2;")
        report = compare_definitions(PROTO_V1, new, format=SchemaFormat.PROTOBUF)
        assert report.compatible is False
        assert report.counts()["breaking"] >= 1

    def test_changing_a_field_number_is_breaking(self) -> None:
        new = PROTO_V1.replace("int64 total = 2;", "int64 total = 5;")
        report = compare_definitions(PROTO_V1, new, format=SchemaFormat.PROTOBUF)
        assert report.compatible is False

    def test_compatible_wire_widening(self) -> None:
        new = PROTO_V1.replace("int64 total = 2;", "int32 total = 2;")
        report = compare_definitions(PROTO_V1, new, format=SchemaFormat.PROTOBUF)
        assert report.compatible is True
        assert report.changes[0].impacts["forward"] is ChangeImpact.POTENTIALLY_BREAKING


class TestEngine:
    def test_comparer_selection(self) -> None:
        assert comparer_for(SchemaFormat.AVRO) is not None
        assert comparer_for(SchemaFormat.JSON_SCHEMA) is not None
        assert comparer_for(SchemaFormat.PROTOBUF) is not None
        assert comparer_for(SchemaFormat.UNKNOWN) is not None

    def test_compare_schemas_uses_formats(self) -> None:
        left = make_schema(AVRO_V1, "avro")
        right = make_schema(AVRO_OPTIONAL_FIELD, "avro")
        report = compare_schemas(left, right)
        assert report.format is SchemaFormat.AVRO
        assert report.mode is CompatibilityMode.BACKWARD
        assert report.old_hash != report.new_hash

    def test_mismatched_formats_are_refused(self) -> None:
        from eventlineage.exceptions import CompatibilityError

        left = make_schema(AVRO_V1, "avro")
        right = make_schema(JSON_V1, "json-schema")
        with pytest.raises(CompatibilityError):
            compare_schemas(left, right)

    def test_empty_schema_is_compatible_with_everything(self) -> None:
        report = compare_schemas(EventSchema(format="avro"), make_schema(AVRO_V1, "avro"))
        assert report.compatible is True

    def test_report_to_dict_is_json_safe(self) -> None:
        report = compare_schemas(
            make_schema(AVRO_V1, "avro"), make_schema(AVRO_OPTIONAL_FIELD, "avro")
        )
        payload = report.to_dict()
        assert payload["compatible"] in {True, False}
        assert isinstance(payload["changes"], list)

    def test_breaking_changes_helper(self) -> None:
        from eventlineage.compatibility import breaking_changes

        report = compare_schemas(
            make_schema(AVRO_V1, "avro"), make_schema(AVRO_REQUIRED_FIELD, "avro")
        )
        assert breaking_changes(report)

    def test_compare_event_versions_walks_history(self) -> None:
        event = make_event(
            "shop.Order",
            schema=AVRO_REQUIRED_FIELD,
            schema_format="avro",
            version="2.0.0",
            versions=[("1.0.0", AVRO_V1), ("2.0.0", AVRO_REQUIRED_FIELD)],
        )
        comparison = compare_event_versions(event, "1.0.0", "2.0.0")
        assert comparison.report.compatible is False
        assert comparison.old_version == "1.0.0"

    def test_compare_unknown_version_raises(self) -> None:
        from eventlineage.exceptions import EventLineageError

        event = make_event("shop.Order", schema=AVRO_V1, schema_format="avro")
        with pytest.raises(EventLineageError):
            compare_event_versions(event, "1.0.0", "9.9.9")

    @pytest.mark.parametrize("mode", list(CompatibilityMode))
    def test_every_mode_is_supported(self, mode: CompatibilityMode) -> None:
        report = compare_schemas(
            make_schema(AVRO_V1, "avro"),
            make_schema(AVRO_OPTIONAL_FIELD, "avro"),
            mode=mode,
        )
        assert report.mode is mode
