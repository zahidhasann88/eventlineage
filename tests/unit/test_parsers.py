"""Schema parsers: Avro, JSON Schema and Protobuf lowered to one field model."""

from __future__ import annotations

import pytest

from eventlineage.models.common import SchemaFormat
from eventlineage.parsers import (
    definition_from_file,
    detect_schema_format,
    parse_avro,
    parse_json_schema,
    parse_proto_file,
    parse_protobuf,
    parse_schema,
)

AVRO_ORDER = {
    "type": "record",
    "name": "OrderCreated",
    "namespace": "shop.orders",
    "fields": [
        {"name": "order_id", "type": "string"},
        {"name": "total_cents", "type": "long"},
        {"name": "note", "type": ["null", "string"], "default": None},
        {"name": "lines", "type": {"type": "array", "items": "string"}},
        {"name": "tags", "type": {"type": "map", "values": "string"}, "default": {}},
        {
            "name": "channel",
            "type": {"type": "enum", "name": "Channel", "symbols": ["WEB", "MOBILE"]},
        },
        {"name": "created_at", "type": {"type": "long", "logicalType": "timestamp-millis"}},
    ],
}

JSON_ORDER = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "order.created",
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "minLength": 1},
        "total_cents": {"type": "integer", "minimum": 0},
        "channel": {"type": "string", "enum": ["web", "mobile"]},
        "lines": {"type": "array", "items": {"type": "string"}},
        "shipping": {
            "type": "object",
            "properties": {"carrier": {"type": "string"}},
            "required": ["carrier"],
        },
    },
    "required": ["order_id", "total_cents"],
}

PROTO_ORDER = """
syntax = "proto3";
package shop.orders;

message OrderCreated {
  string order_id = 1;
  int64 total_cents = 2;
  repeated Line lines = 3;
  Channel channel = 4;
  optional string note = 5;

  message Line {
    string sku = 1;
    int32 quantity = 2;
  }

  enum Channel {
    CHANNEL_UNSPECIFIED = 0;
    CHANNEL_WEB = 1;
  }
}

message Unrelated {
  string x = 1;
}

service OrderService {
  rpc Publish (OrderCreated) returns (OrderCreated);
}
"""


def field_map(parsed: object) -> dict[str, object]:
    return {field.name: field for field in parsed.fields}  # type: ignore[attr-defined]


class TestFormatDetection:
    def test_avro_document(self) -> None:
        assert detect_schema_format(AVRO_ORDER) is SchemaFormat.AVRO

    def test_json_schema_document(self) -> None:
        assert detect_schema_format(JSON_ORDER) is SchemaFormat.JSON_SCHEMA

    def test_yaml_fragment_is_unknown(self) -> None:
        assert detect_schema_format("just a string") is SchemaFormat.UNKNOWN

    def test_openapi_keywords(self) -> None:
        document = {"openapi": "3.1.0", "info": {"title": "x"}, "paths": {}}
        assert detect_schema_format(document) is SchemaFormat.OPENAPI


class TestAvroParser:
    def test_parses_fields(self) -> None:
        parsed = parse_avro(AVRO_ORDER)
        fields = field_map(parsed)
        assert set(fields) == {
            "order_id",
            "total_cents",
            "note",
            "lines",
            "tags",
            "channel",
            "created_at",
        }
        assert parsed.name == "shop.orders.OrderCreated"
        assert parsed.root_type == "record"

    def test_required_and_nullable(self) -> None:
        fields = field_map(parse_avro(AVRO_ORDER))
        assert fields["order_id"].required is True
        assert fields["note"].required is False
        assert fields["note"].nullable is True

    def test_collections_and_enums(self) -> None:
        fields = field_map(parse_avro(AVRO_ORDER))
        assert fields["lines"].repeated is True
        assert fields["lines"].type == "array"
        assert fields["tags"].type == "map"
        assert fields["channel"].enum_values == ("WEB", "MOBILE")

    def test_logical_types_are_mapped(self) -> None:
        fields = field_map(parse_avro(AVRO_ORDER))
        assert fields["created_at"].type == "timestamp"

    def test_nested_record_fields(self) -> None:
        document = {
            "type": "record",
            "name": "Outer",
            "fields": [
                {
                    "name": "inner",
                    "type": {
                        "type": "record",
                        "name": "Inner",
                        "fields": [{"name": "value", "type": "string"}],
                    },
                }
            ],
        }
        parsed = parse_avro(document)
        inner = field_map(parsed)["inner"]
        assert inner.fields
        assert inner.fields[0].name == "value"

    def test_tolerates_incomplete_documents(self) -> None:
        # A malformed fragment must not crash a scan; it degrades to an empty
        # field list and is reported by validation instead.
        parsed = parse_avro({"type": "record"})
        assert parsed.fields == ()
        assert parsed.name is None


class TestJsonSchemaParser:
    def test_parses_properties(self) -> None:
        parsed = parse_json_schema(JSON_ORDER)
        fields = field_map(parsed)
        assert set(fields) == {"order_id", "total_cents", "channel", "lines", "shipping"}
        assert fields["order_id"].required is True
        assert fields["channel"].required is False

    def test_constraints_are_kept(self) -> None:
        fields = field_map(parse_json_schema(JSON_ORDER))
        assert fields["order_id"].constraints.get("minLength") == 1
        assert fields["total_cents"].constraints.get("minimum") == 0
        assert fields["channel"].enum_values == ("web", "mobile")

    def test_nested_objects_are_recursive(self) -> None:
        fields = field_map(parse_json_schema(JSON_ORDER))
        shipping = fields["shipping"]
        assert shipping.fields[0].name == "carrier"
        assert shipping.fields[0].required is True

    def test_type_arrays_are_nullable(self) -> None:
        document = {
            "type": "object",
            "properties": {"note": {"type": ["string", "null"]}},
        }
        fields = field_map(parse_json_schema(document))
        assert fields["note"].nullable is True

    def test_definitions_are_resolved(self) -> None:
        document = {
            "type": "object",
            "properties": {"shipping": {"$ref": "#/$defs/Shipping"}},
            "$defs": {
                "Shipping": {
                    "type": "object",
                    "properties": {"carrier": {"type": "string"}},
                }
            },
        }
        fields = field_map(parse_json_schema(document))
        assert fields["shipping"].fields[0].name == "carrier"


class TestProtobufParser:
    def test_parses_messages_and_services(self) -> None:
        proto = parse_proto_file(PROTO_ORDER, path="order.proto")
        assert [message.name for message in proto.messages] == ["OrderCreated", "Unrelated"]
        assert proto.services[0].name == "OrderService"
        assert proto.find_message("shop.orders.OrderCreated") is not None

    def test_message_fields(self) -> None:
        proto = parse_proto_file(PROTO_ORDER)
        message = proto.find_message("OrderCreated")
        assert message is not None
        assert [field.name for field in message.fields] == [
            "order_id",
            "total_cents",
            "lines",
            "channel",
            "note",
        ]
        assert message.field("lines").label == "repeated"
        assert message.field("channel").type == "Channel"

    def test_parsed_fields_are_normalized(self) -> None:
        parsed = parse_protobuf(PROTO_ORDER, message_name="shop.orders.OrderCreated")
        fields = field_map(parsed)
        assert fields["order_id"].type == "string"
        assert fields["total_cents"].type == "long"
        assert fields["lines"].repeated is True
        assert fields["channel"].enum_values == ("CHANNEL_UNSPECIFIED", "CHANNEL_WEB")

    def test_nested_message_fields(self) -> None:
        proto = parse_proto_file(PROTO_ORDER)
        message = proto.find_message("shop.orders.OrderCreated")
        assert message is not None
        nested = message.find_nested("Line")
        assert nested is not None
        assert [field.name for field in nested.fields] == ["sku", "quantity"]
        assert message.field_by_number(1).name == "order_id"

    def test_definition_envelope_is_path_independent(self) -> None:
        first = parse_proto_file(PROTO_ORDER, path="/tmp/a/order.proto")
        second = parse_proto_file(PROTO_ORDER, path="/home/elsewhere/order.proto")
        left = definition_from_file(first, first.find_message("OrderCreated"))  # type: ignore[arg-type]
        right = definition_from_file(second, second.find_message("OrderCreated"))  # type: ignore[arg-type]
        assert left == right
        assert "path" not in left["file"]

    def test_invalid_proto_raises(self) -> None:
        from eventlineage.exceptions import ParseError

        with pytest.raises(ParseError):
            parse_proto_file("syntax = 'proto3'; message {")


class TestParseSchemaFacade:
    def test_dispatches_on_format(self) -> None:
        from eventlineage.models.schema import EventSchema

        parsed = parse_schema(EventSchema(format="avro", definition=AVRO_ORDER))
        assert len(parsed.fields) == 7

    def test_caches_parsed_result(self) -> None:
        from eventlineage.models.schema import EventSchema

        schema = EventSchema(format="json-schema", definition=JSON_ORDER)
        assert schema.parsed() is schema.parsed()

    def test_field_paths(self) -> None:
        from eventlineage.models.schema import EventSchema

        schema = EventSchema(format="json-schema", definition=JSON_ORDER)
        paths = schema.field_paths()
        assert "order_id" in paths
        assert any(path.startswith("shipping") for path in paths)
