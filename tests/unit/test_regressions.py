"""Regression tests: one per defect found in the pre-release review.

Each test states the behaviour that used to be wrong, so a failure here names the
bug that came back.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from typer.testing import CliRunner

from eventlineage import Event, Project, compare_definitions
from eventlineage.analysis import analyze_catalog, analyze_impact
from eventlineage.cli.main import app, main
from eventlineage.compatibility import ChangeType
from eventlineage.config import Config
from eventlineage.core.catalog import build_catalog
from eventlineage.discovery.schema_docs import event_name_from_path
from eventlineage.exceptions import ConfigError, SymbolNotFoundError
from eventlineage.graph.algorithms import cycle_through, find_cycles
from eventlineage.graph.builder import build_graph
from eventlineage.graph.model import Edge, Graph, Node
from eventlineage.models import Consumer, EventRelationship, Producer, Service, Severity
from eventlineage.models.common import DependencyKind, SourceLocation
from eventlineage.parsers import parse_json_schema
from eventlineage.utils.cache import ScanCache
from eventlineage.utils.fs import matches_glob
from eventlineage.utils.versioning import latest_version, version_key

MakeProject = Callable[..., Path]
JSON_SCHEMA = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}
runner = CliRunner()


def proto(body: str) -> str:
    return f'syntax = "proto3"; message M {{ {body} }}'


def avro(*fields: dict[str, object]) -> dict[str, object]:
    return {"type": "record", "name": "A", "fields": list(fields)}


def scan(root: Path) -> Project:
    project = Project.open(root)
    project.scan(incremental=False)
    return project


class TestProtobufComparison:
    def test_renaming_the_only_field_does_not_crash(self) -> None:
        report = compare_definitions(
            proto("string a = 1;"), proto("string b = 1;"), format="protobuf"
        )
        assert report.compatible is True
        assert [change.change for change in report.changes] == [ChangeType.FIELD_RENAMED]

    def test_rename_still_compares_the_rest_of_the_field(self) -> None:
        report = compare_definitions(
            proto("repeated string a = 1;"), proto("string b = 1;"), format="protobuf"
        )
        assert ChangeType.CARDINALITY_CHANGED in {change.change for change in report.changes}

    def test_reusing_a_number_for_another_wire_type_is_breaking(self) -> None:
        report = compare_definitions(
            proto("string a = 1;"), proto("int64 b = 1;"), format="protobuf"
        )
        assert report.compatible is False
        assert ChangeType.FIELD_NUMBER_CHANGED in {change.change for change in report.changes}

    @pytest.mark.parametrize(
        ("old", "new"),
        [("int32", "sint32"), ("sint64", "uint64"), ("fixed32", "float"), ("double", "sfixed64")],
    )
    def test_same_wire_group_with_another_encoding_is_breaking(self, old: str, new: str) -> None:
        report = compare_definitions(
            proto(f"{old} a = 1;"), proto(f"{new} a = 1;"), format="protobuf"
        )
        assert report.compatible is False


class TestAvroComparison:
    def test_float_to_double_is_a_legal_promotion(self) -> None:
        report = compare_definitions(
            avro({"name": "x", "type": "float"}),
            avro({"name": "x", "type": "double"}),
            format="avro",
        )
        assert report.compatible is True

    def test_adding_an_alias_changes_nothing(self) -> None:
        report = compare_definitions(
            avro({"name": "x", "type": "string"}),
            avro({"name": "x", "type": "string", "aliases": ["y"]}),
            format="avro",
        )
        assert report.changes == ()

    def test_adding_a_union_branch_is_only_forward_breaking(self) -> None:
        old = avro({"name": "x", "type": ["string", "int"]})
        new = avro({"name": "x", "type": ["string", "int", "long"]})
        assert compare_definitions(old, new, format="avro").compatible is True
        assert compare_definitions(old, new, format="avro", mode="forward").compatible is False

    def test_reordering_a_union_is_no_change(self) -> None:
        report = compare_definitions(
            avro({"name": "x", "type": ["string", "int"]}),
            avro({"name": "x", "type": ["int", "string"]}),
            format="avro",
        )
        assert report.changes == ()

    def test_adding_an_enum_symbol_is_backward_compatible(self) -> None:
        def enum(*symbols: str) -> dict[str, object]:
            return avro({"name": "e", "type": {"type": "enum", "name": "E", "symbols": symbols}})

        change = compare_definitions(enum("A"), enum("A", "B"), format="avro").changes[0]
        assert change.impacts["backward"].value == "compatible"
        assert change.impacts["forward"].value == "breaking"


class TestJsonSchema:
    def test_recursive_reference_terminates(self) -> None:
        node = {"type": "object", "properties": {"next": {"$ref": "#/$defs/N"}}}
        document = {
            "type": "object",
            "properties": {"root": {"$ref": "#/$defs/N"}},
            "$defs": {"N": node},
        }
        assert parse_json_schema(document).fields[0].fields[0].name == "next"

    def test_branching_recursion_terminates(self) -> None:
        node = {
            "type": "object",
            "properties": {"left": {"$ref": "#/$defs/N"}, "right": {"$ref": "#/$defs/N"}},
        }
        document = {"$ref": "#/$defs/N", "$defs": {"N": node}}
        assert {field.name for field in parse_json_schema(document).fields} == {"left", "right"}

    def test_root_reference_exposes_its_fields(self) -> None:
        document = {
            "$ref": "#/$defs/O",
            "$defs": {"O": {"type": "object", "properties": {"a": {"type": "string"}}}},
        }
        assert [field.name for field in parse_json_schema(document).fields] == ["a"]

    def test_root_all_of_exposes_its_fields(self) -> None:
        document = {
            "allOf": [{"type": "object", "properties": {"a": {}}, "required": ["a"]}],
        }
        parsed = parse_json_schema(document)
        assert [(field.name, field.required) for field in parsed.fields] == [("a", True)]

    def test_breaking_change_behind_a_root_reference_is_seen(self) -> None:
        def wrapped(field: str) -> dict[str, object]:
            props = {"type": "object", "properties": {field: {"type": "string"}}}
            return {"$ref": "#/$defs/O", "$defs": {"O": {**props, "required": [field]}}}

        report = compare_definitions(wrapped("a"), wrapped("b"), format="json-schema")
        assert report.changes != ()

    def test_nullable_directions(self) -> None:
        plain = {"type": "object", "properties": {"x": {"type": "string"}}}
        nullable = {"type": "object", "properties": {"x": {"type": ["string", "null"]}}}
        widened = compare_definitions(plain, nullable, format="json-schema").changes[0]
        assert widened.impacts["backward"].value == "compatible"
        assert widened.impacts["forward"].value == "potentially_breaking"
        narrowed = compare_definitions(nullable, plain, format="json-schema").changes[0]
        assert narrowed.impacts["backward"].value == "breaking"

    def test_x_alias_marks_a_compatible_rename(self) -> None:
        old = {"type": "object", "properties": {"a": {"type": "string"}}}
        new = {"type": "object", "properties": {"b": {"type": "string", "x-alias": "a"}}}
        report = compare_definitions(old, new, format="json-schema")
        assert [change.change for change in report.changes] == [ChangeType.FIELD_RENAMED]
        assert report.compatible is True


class TestDiscovery:
    def test_human_title_falls_back_to_the_file_name(self, make_project: MakeProject) -> None:
        schema = {**JSON_SCHEMA, "title": "Order Created", "properties": {"a": {}}}
        root = make_project(files={"schemas/order.created.json": schema})
        assert "order.created" in scan(root).catalog().events

    def test_property_named_events_stays_a_json_schema(self, make_project: MakeProject) -> None:
        schema = {**JSON_SCHEMA, "title": "a.b", "properties": {"events": {"type": "array"}}}
        schemaless = {"title": "c.d", "type": "object", "properties": {"events": {}}}
        root = make_project(files={"s/a.b.json": schema, "s/c.d.json": schemaless})
        assert {"a.b", "c.d"} <= set(scan(root).catalog().events)

    def test_catalog_with_many_keys_is_still_detected(self, make_project: MakeProject) -> None:
        document = {f"x{index}": 1 for index in range(20)}
        document["events"] = [{"name": "a.b"}]
        root = make_project(files={"catalog.json": document})
        assert "a.b" in scan(root).catalog().events

    def test_byte_order_mark_is_accepted(self, make_project: MakeProject) -> None:
        root = make_project()
        schema = {**JSON_SCHEMA, "title": "a.b", "properties": {"x": {}}}
        (root / "a.b.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(schema).encode())
        assert "a.b" in scan(root).catalog().events

    def test_standalone_versions_build_a_history(self, make_project: MakeProject) -> None:
        def schema(field: str) -> dict[str, object]:
            return {**JSON_SCHEMA, "title": "a.b", "properties": {field: {"type": "string"}}}

        root = make_project(files={"a.b.v1.json": schema("x"), "a.b.v2.json": schema("y")})
        project = Project.open(root)
        result = project.scan(incremental=False)
        event = result.catalog.events["a.b"]
        assert event.schema_versions() == ("2", "1")
        assert list(event.active_schema().definition["properties"]) == ["y"]
        assert not [item for item in result.diagnostics if item.code == "schema-conflict"]
        assert len(project.version_history("a.b")) == 1

    def test_same_version_with_another_schema_is_still_a_conflict(
        self, make_project: MakeProject
    ) -> None:
        def schema(field: str) -> dict[str, object]:
            return {**JSON_SCHEMA, "title": "a.b", "properties": {field: {"type": "string"}}}

        root = make_project(files={"one/a.b.v1.json": schema("x"), "two/a.b.v1.json": schema("y")})
        result = Project.open(root).scan(incremental=False)
        assert [item.code for item in result.diagnostics] == ["schema-conflict"]

    def test_explicit_version_is_not_replaced_by_a_file_name(
        self, make_project: MakeProject
    ) -> None:
        schema = {**JSON_SCHEMA, "title": "a.b", "properties": {"x": {"type": "string"}}}
        root = make_project(
            files={
                "c.yaml": "version: 1\nevents:\n  - name: a.b\n    version: 1.0.0\n"
                "    producers: [p]\n    schema:\n      file: s/a.b.v1.json\n",
                "s/a.b.v1.json": schema,
            }
        )
        assert scan(root).catalog().events["a.b"].version == "1.0.0"

    def test_declared_events_with_the_same_schema_survive(self, make_project: MakeProject) -> None:
        body = "    schema: {type: object, properties: {i: {type: string}}}\n"
        root = make_project(
            files={"c.yaml": f"version: 1\nevents:\n  - name: a.x\n{body}  - name: a.y\n{body}"}
        )
        assert set(scan(root).catalog().events) == {"a.x", "a.y"}

    def test_schema_only_files_with_the_same_content_survive(
        self, make_project: MakeProject
    ) -> None:
        schema = {**JSON_SCHEMA, "properties": {"x": {"type": "string"}}}
        root = make_project(files={"a.x.json": schema, "a.y.json": schema})
        assert set(scan(root).catalog().events) == {"a.x", "a.y"}

    def test_a_declaration_clears_the_inferred_flag(self, make_project: MakeProject) -> None:
        schema = {**JSON_SCHEMA, "title": "a.b", "properties": {"x": {"type": "string"}}}
        root = make_project(
            files={
                "a-services.yaml": "services:\n  s:\n    consumes: [a.b]\n",
                "b.json": schema,
            }
        )
        assert scan(root).catalog().events["a.b"].inferred is False

    def test_underscore_version_in_a_file_name(self) -> None:
        assert event_name_from_path(Path("orders.created.v1_2.json")) == ("orders.created", "1.2")

    def test_catalog_export_can_be_read_back(
        self, make_project: MakeProject, tmp_path: Path
    ) -> None:
        root = make_project(
            files={"c.yaml": "version: 1\nevents:\n  - name: a.b\n    producers: [p]\n"}
        )
        exported = scan(root).catalog().to_dict(include_schemas=False)
        again = tmp_path / "again"
        again.mkdir()
        (again / "catalog.json").write_text(json.dumps(exported), encoding="utf-8")
        assert scan(again).catalog().events["a.b"].producer_services == ("p",)

    def test_avro_records_keep_references_to_each_other(self, make_project: MakeProject) -> None:
        money = {"type": "record", "name": "Money", "fields": [{"name": "c", "type": "long"}]}
        order = {"type": "record", "name": "Order", "fields": [{"name": "t", "type": "Money"}]}
        root = make_project(files={"m.avsc": [money, order]})
        field = scan(root).catalog().events["Order"].active_schema().parsed().fields[0]
        assert [child.name for child in field.fields] == ["c"]

    def test_key_subjects_are_not_value_versions(self, make_project: MakeProject) -> None:
        def subject(name: str) -> dict[str, object]:
            schema = json.dumps({"type": "record", "name": name, "fields": []})
            return {
                "subject": f"o.c-{name}",
                "versions": [{"version": 1, "schemaType": "AVRO", "schema": schema}],
            }

        root = make_project(files={"r.json": {"subjects": [subject("value"), subject("key")]}})
        assert [entry.version for entry in scan(root).catalog().events["o.c"].versions] == ["1"]

    def test_webhook_schema_is_read_from_the_path_item(self, make_project: MakeProject) -> None:
        root = make_project(
            files={
                "o.yaml": "openapi: 3.1.0\ninfo: {title: S}\nwebhooks:\n  hook.fired:\n    post:\n"
                "      requestBody:\n        content:\n          application/json:\n"
                "            schema: {type: object, properties: {a: {type: string}}}\n"
            }
        )
        event = scan(root).catalog().events["hook.fired"]
        assert event.active_schema() is not None
        assert event.qualified_name == "hook.fired"

    def test_proto_service_name_keeps_its_suffix(self, make_project: MakeProject) -> None:
        root = make_project(
            files={
                "o.proto": 'syntax = "proto3"; package shop;\n'
                "message OrderEvent { string a = 1; }\n"
                "message Req { string a = 1; }\n"
                "service OrderService { rpc Emit (Req) returns (OrderEvent); }\n"
            }
        )
        assert "order-service" in scan(root).catalog().services


class TestAsyncApi:
    def test_operations_follow_the_specification(self, make_project: MakeProject) -> None:
        root = make_project(
            files={
                "a.yaml": "asyncapi: 2.6.0\ninfo: {title: Svc}\nchannels:\n  c:\n"
                "    subscribe:\n      message: {name: a.sent}\n"
                "    publish:\n      message: {name: a.received}\n"
            }
        )
        events = scan(root).catalog().events
        assert events["a.sent"].producer_services == ("svc",)
        assert events["a.received"].consumer_services == ("svc",)

    def test_external_reference_with_a_pointer(self, make_project: MakeProject) -> None:
        root = make_project(
            files={
                "a.yaml": "asyncapi: 2.6.0\ninfo: {title: S}\nchannels:\n  c:\n    subscribe:\n"
                "      message:\n        name: a.b\n        payload:\n"
                "          $ref: 'lib.yaml#/defs/P'\n",
                "lib.yaml": "defs:\n  P: {type: object, properties: {a: {type: string}}}\n",
            }
        )
        assert scan(root).catalog().events["a.b"].active_schema() is not None

    def test_unnamed_messages_keep_their_component_key(self, make_project: MakeProject) -> None:
        root = make_project(
            files={
                "a.yaml": "asyncapi: 2.6.0\ninfo: {title: S}\nchannels:\n  orders:\n"
                "    subscribe:\n      message:\n        oneOf:\n"
                "          - $ref: '#/components/messages/Created'\n"
                "          - $ref: '#/components/messages/Paid'\n"
                "components:\n  messages:\n    Created: {payload: {type: object}}\n"
                "    Paid: {payload: {type: object}}\n"
            }
        )
        assert set(scan(root).catalog().events) == {"s.Created", "s.Paid"}


class TestScanCache:
    CATALOG = (
        "version: 1\nevents:\n  - name: a.b\n    producers: [p]\n    schema:\n"
        "      format: json-schema\n      file: s.schema\n"
    )

    def test_changing_a_referenced_file_invalidates_the_entry(
        self, make_project: MakeProject
    ) -> None:
        def schema(field: str) -> dict[str, object]:
            return {"type": "object", "properties": {field: {"type": "string"}}}

        root = make_project(files={"c.yaml": self.CATALOG, "s.schema": schema("old")})
        Project.open(root).scan()
        (root / "s.schema").write_text(json.dumps(schema("new")), encoding="utf-8")
        result = Project.open(root).scan()
        assert list(result.catalog.events["a.b"].active_schema().definition["properties"]) == [
            "new"
        ]
        assert result.summary.files_parsed >= 1

    def test_unchanged_reference_is_a_cache_hit(self, make_project: MakeProject) -> None:
        root = make_project(
            files={"c.yaml": self.CATALOG, "s.schema": {"type": "object", "properties": {}}}
        )
        Project.open(root).scan()
        assert Project.open(root).scan().summary.files_parsed == 0

    def test_creating_a_missing_reference_invalidates_the_entry(
        self, make_project: MakeProject
    ) -> None:
        root = make_project(files={"c.yaml": self.CATALOG})
        assert "a.b" not in Project.open(root).scan().catalog.events
        (root / "s.schema").write_text('{"type": "object"}', encoding="utf-8")
        assert "a.b" in Project.open(root).scan().catalog.events

    def test_warm_scan_repeats_the_diagnostics(self, make_project: MakeProject) -> None:
        root = make_project(files={"c.yaml": self.CATALOG})
        cold, warm = Project.open(root).scan(), Project.open(root).scan()
        assert warm.summary.files_cached == 1
        assert [str(item) for item in warm.diagnostics] == [str(item) for item in cold.diagnostics]

    def test_partial_scan_keeps_other_entries(self, make_project: MakeProject) -> None:
        root = make_project(
            files={
                "a.yaml": "version: 1\nevents:\n  - name: a.a\n",
                "b.yaml": "version: 1\nevents:\n  - name: b.b\n",
            }
        )
        Project.open(root).scan()
        Project.open(root).scan([root / "a.yaml"])
        assert Project.open(root).scan().summary.files_parsed == 0

    @pytest.mark.parametrize("payload", ["[]", "1", "null"])
    def test_valid_json_of_the_wrong_shape_is_ignored(self, tmp_path: Path, payload: str) -> None:
        path = tmp_path / "scan.json"
        path.write_text(payload, encoding="utf-8")
        assert ScanCache.load(path, "fp").entries == {}

    def test_binary_garbage_is_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "scan.json"
        path.write_bytes(b"\xff\xfe\x00")
        assert ScanCache.load(path, "fp").entries == {}

    def test_oversized_cache_is_discarded(self, tmp_path: Path) -> None:
        path = tmp_path / "scan.json"
        cache = ScanCache(path, "fp")
        cache.put("a.yaml", "digest", "catalog", {"events": []})
        cache.save()
        assert ScanCache.load(path, "fp", max_bytes=1).entries == {}


class TestGraph:
    def test_correlates_is_not_a_directed_edge(self) -> None:
        catalog = build_catalog(
            events=[Event(id="a.b", name="a.b"), Event(id="c.d", name="c.d")],
            relationships=[
                EventRelationship(source_event="a.b", target_event="c.d", kind="correlates"),
                EventRelationship(source_event="c.d", target_event="a.b", kind="correlates"),
            ],
        )
        assert build_graph(catalog).edge_count == 0

    def test_edge_filter_applies_to_relationships(self) -> None:
        catalog = build_catalog(
            events=[
                Event(id="a.b", name="a.b", producers=[Producer(service="s")]),
                Event(id="c.d", name="c.d"),
            ],
            relationships=[EventRelationship(source_event="a.b", target_event="c.d")],
        )
        graph = build_graph(catalog, edge_kinds=[DependencyKind.PRODUCES])
        assert graph.edge_kind_counts() == {"produces": 1}

    def test_parallel_edges_are_one_cycle(self) -> None:
        nodes = [Node(id=name, kind="event", label=name, ref=name) for name in "ab"]
        edges = [Edge(0, 1, "triggers"), Edge(0, 1, "derived_from"), Edge(1, 0, "triggers")]
        assert len(find_cycles(Graph.build(nodes, edges))) == 1

    def test_cycle_through(self) -> None:
        nodes = [Node(id=name, kind="event", label=name, ref=name) for name in "abc"]
        graph = Graph.build(
            nodes, [Edge(0, 1, "triggers"), Edge(1, 0, "triggers"), Edge(1, 2, "triggers")]
        )
        assert set(cycle_through(graph, 0)) == {0, 1}
        assert cycle_through(graph, 2) == ()

    def test_impact_finds_the_cycle_through_its_target(self) -> None:
        events = []
        for index in range(30):
            first, second = f"a{index}", f"b{index}"
            events.append(
                Event(
                    name=f"x.e{index}",
                    producers=[Producer(service=first)],
                    consumers=[Consumer(service=second)],
                )
            )
            events.append(
                Event(
                    name=f"x.f{index}",
                    producers=[Producer(service=second)],
                    consumers=[Consumer(service=first)],
                )
            )
        events.append(
            Event(
                name="z.x",
                producers=[Producer(service="p")],
                consumers=[Consumer(service="c")],
            )
        )
        events.append(
            Event(
                name="z.y",
                producers=[Producer(service="c")],
                consumers=[Consumer(service="p")],
            )
        )
        report = analyze_impact(build_catalog(events=events), "z.x")
        assert "z.x" in report.cycles_detected[0]


class TestAnalysis:
    def test_hotspot_is_the_service_others_depend_on(self) -> None:
        catalog = build_catalog(
            events=[
                Event(
                    name=f"a.e{index}",
                    producers=[Producer(service="source")],
                    consumers=[Consumer(service=f"sink{index}")],
                )
                for index in range(4)
            ]
        )
        hotspots = [item for item in analyze_catalog(catalog).hotspots if item.kind == "service"]
        assert hotspots[0].name == "source"
        assert hotspots[0].description == "4 downstream services depend on it"

    def test_longest_chain_after_many_isolated_events(self) -> None:
        events = [Event(name=f"a.i{index:02d}") for index in range(25)]
        events += [Event(name=name) for name in ("z.a", "z.b", "z.c")]
        catalog = build_catalog(
            events=events,
            relationships=[
                EventRelationship(source_event="z.a", target_event="z.b"),
                EventRelationship(source_event="z.b", target_event="z.c"),
            ],
        )
        assert analyze_catalog(catalog).longest_chains[0][-1].endswith("z.c")


class TestVersions:
    def test_short_versions_sort_with_their_long_form(self) -> None:
        assert latest_version(["2", "2.0.1"]) == "2.0.1"
        assert version_key("2") == version_key("2.0.0")
        assert version_key("2.0.0-rc1") < version_key("2.0.0") < version_key("2.0.1")

    @given(st.lists(st.integers(0, 40), min_size=3, max_size=3))
    def test_ordering_matches_numeric_tuples(self, parts: list[int]) -> None:
        other = [parts[0], parts[1], parts[2] + 1]
        left, right = ".".join(map(str, parts)), ".".join(map(str, other))
        assert version_key(left) < version_key(right)


class TestConfiguration:
    def test_fail_on_off_survives_yaml(self, make_project: MakeProject) -> None:
        root = make_project(config="version: 1\nvalidation:\n  fail_on: off\n")
        assert Config.load(root=root).validation.fail_on is Severity.OFF

    def test_unsupported_version_is_refused(self, make_project: MakeProject) -> None:
        with pytest.raises(ConfigError, match="unsupported configuration version"):
            Config.load(root=make_project(config="version: 2\n"))

    def test_bad_environment_override_is_a_config_error(
        self, make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EVENTLINEAGE_FAIL_ON", "bogus")
        with pytest.raises(ConfigError):
            Config.load(root=make_project())

    def test_cache_fingerprint_changes_with_the_release(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = Config().fingerprint()
        monkeypatch.setattr("eventlineage.__version__", "99.0.0")
        assert Config().fingerprint() != before

    def test_leading_dot_globs_match(self) -> None:
        assert matches_glob(".github/x.yaml", [".github/*"])
        assert matches_glob("./schemas/a.json", ["schemas/*"])


class TestValidation:
    def test_consumer_of_an_undeclared_name_is_eg003(self, make_project: MakeProject) -> None:
        root = make_project(
            files={
                "p.yaml": "services:\n  a:\n    produces: [o.created]\n"
                "  b:\n    consumes: [o.creatd]\n"
            }
        )
        report = Project.open(root).validate()
        assert [item.subject for item in report.findings if item.rule == "EG003"] == ["o.creatd"]

    def test_suppression_path_ignores_the_line_number(self) -> None:
        from eventlineage.validation.rule import Finding, ValidationContext

        config = Config.model_validate(
            {"validation": {"suppress": [{"rule": "EG001", "path": "a.yaml"}]}}
        )
        finding = Finding(
            rule="EG001", rule_name="x", message="m", source=SourceLocation(path="a.yaml", line=3)
        )
        context = ValidationContext(catalog=build_catalog(), config=config)
        assert context.suppression_for(finding) is not None

    def test_pinned_versions_compare_oldest_first(self) -> None:
        def schema(field: str) -> dict[str, object]:
            return {
                "format": "json-schema",
                "definition": {
                    "type": "object",
                    "properties": {field: {"type": "string"}},
                    "required": [field],
                },
            }

        event = Event.model_validate(
            {
                "name": "a.b",
                "version": "10.0.0",
                "versions": [
                    {"version": "9.0.0", "schema": schema("x")},
                    {"version": "10.0.0", "schema": schema("y")},
                ],
                "producers": [{"service": "p", "schema_version": "10.0.0"}],
                "consumers": [{"service": "c", "schema_version": "9.0.0"}],
            }
        )
        from eventlineage.validation import validate_catalog

        findings = validate_catalog(build_catalog(events=[event])).findings
        message = next(item.message for item in findings if item.rule == "EG009")
        assert "'9.0.0' and '10.0.0'" in message


class TestModels:
    def test_event_id_defaults_to_the_name(self) -> None:
        event = Event(name="orders.created")
        assert (event.id, event.namespace, event.name) == (
            "orders.created",
            "orders",
            "created",
        )

    def test_service_merge_keeps_an_explicit_kind_and_metadata(self) -> None:
        merged = Service(name="a").merge(Service(name="a", kind="external", metadata={"x": 1}))
        assert merged.kind.value == "external"
        assert merged.metadata == {"x": 1}

    def test_symbol_hint_names_real_commands(self) -> None:
        error = SymbolNotFoundError("x", "event or service")
        assert "'eventlineage events' or 'eventlineage services'" in str(error.hint)


def cli(*args: str, cwd: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", "from eventlineage.cli.main import main; main()", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=cwd,
        env={**os.environ, **env},
        check=False,
    )


def test_unexpected_failure_exits_three(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def explode() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(sys.modules["eventlineage.cli.main"], "app", explode)
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 3
    assert "boom" in capsys.readouterr().err


class TestCommandLine:
    @pytest.fixture
    def project(self, project_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.chdir(project_dir)
        return project_dir

    def test_quiet_silences_tables(self, project: Path) -> None:
        assert runner.invoke(app, ["-q", "scan"]).stdout == ""

    def test_graph_json_honours_filters(self, project: Path) -> None:
        output = runner.invoke(app, ["graph", "-f", "json", "--node-kinds", "service"]).stdout
        assert set(json.loads(output)["stats"]["node_kinds"]) == {"service"}

    def test_graph_kinds_accept_commas(self, project: Path) -> None:
        output = runner.invoke(
            app, ["graph", "-f", "json", "--node-kinds", "event,service", "--max-nodes", "2"]
        ).stdout
        assert json.loads(output)["stats"]["nodes"] == 2

    def test_events_query_accepts_a_glob(self, project: Path) -> None:
        output = runner.invoke(app, ["events", "payment.*", "-f", "json"]).stdout
        assert [event["id"] for event in json.loads(output)] == ["payment.captured"]

    def test_markup_in_a_symbol_is_printed_literally(self, project: Path) -> None:
        result = runner.invoke(app, ["impact", "foo[/bar]"])
        assert result.exit_code == 2
        assert "foo[/bar]" in result.stderr

    def test_unknown_rule_is_a_usage_error(self, project: Path) -> None:
        assert runner.invoke(app, ["validate", "--rule", "NOPE"]).exit_code == 2

    def test_fail_on_breaking_false_keeps_compare_green(
        self, make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old = {"type": "record", "name": "A", "fields": []}
        new = {**old, "fields": [{"name": "x", "type": "string"}]}
        root = make_project(
            config="version: 1\ncompatibility:\n  fail_on_breaking: false\n",
            files={"old.avsc": old, "new.avsc": new},
        )
        monkeypatch.chdir(root)
        assert runner.invoke(app, ["compare", "old.avsc", "new.avsc"]).exit_code == 0

    def test_exit_two_for_bad_comparison_input(self, project: Path) -> None:
        assert runner.invoke(app, ["compare", "nope.json", "x.json"]).exit_code == 2
        assert runner.invoke(app, ["compare", "--event", "order.created", "8", "9"]).exit_code == 2
        assert (
            runner.invoke(
                app,
                [
                    "compare",
                    "schemas/order.created.v1.json",
                    "schemas/order.created.v2.json",
                    "--schema-format",
                    "bogus",
                ],
            ).exit_code
            == 2
        )

    def test_report_title_setting_is_used(self, make_project: MakeProject) -> None:
        root = make_project(
            config="version: 1\nreport:\n  title: Custom title\n",
            files={"c.yaml": "version: 1\nevents:\n  - name: a.b\n"},
        )
        assert Project.open(root).report().startswith("# Custom title\n")

    def test_redirected_output_survives_a_legacy_code_page(self, project: Path) -> None:
        result = cli("impact", "order.created", cwd=project, PYTHONIOENCODING="cp1252")
        assert result.returncode == 0, result.stderr

    def test_bad_environment_is_a_clean_usage_error(self, project: Path) -> None:
        (project / "eventlineage.yaml").write_text("version: 1\n", encoding="utf-8")
        result = cli(
            "--config",
            "eventlineage.yaml",
            "scan",
            cwd=project,
            EVENTLINEAGE_COMPAT_MODE="bogus",
        )
        assert result.returncode == 2
        assert "Traceback" not in result.stderr


OPEN_SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}}
CLOSED_SCHEMA = {**OPEN_SCHEMA, "additionalProperties": False}


class TestAdditionalProperties:
    def test_closing_the_root_is_backward_breaking(self) -> None:
        report = compare_definitions(OPEN_SCHEMA, CLOSED_SCHEMA, format="json-schema")
        change = report.changes[0]
        assert (change.path, change.change) == ("", ChangeType.CONSTRAINT_TIGHTENED)
        assert report.compatible is False

    def test_opening_the_root_is_forward_sensitive(self) -> None:
        change = compare_definitions(CLOSED_SCHEMA, OPEN_SCHEMA, format="json-schema").changes[0]
        assert change.change is ChangeType.CONSTRAINT_LOOSENED
        assert change.impacts["forward"].value == "potentially_breaking"

    def test_explicit_true_equals_the_default(self) -> None:
        explicit = {**OPEN_SCHEMA, "additionalProperties": True}
        assert compare_definitions(OPEN_SCHEMA, explicit, format="json-schema").changes == ()

    def test_nested_object(self) -> None:
        def nested(body: dict[str, object]) -> dict[str, object]:
            return {"type": "object", "properties": {"n": body}}

        report = compare_definitions(
            nested(OPEN_SCHEMA), nested(CLOSED_SCHEMA), format="json-schema"
        )
        assert [change.path for change in report.changes] == ["n"]

    def test_behind_a_root_reference(self) -> None:
        wrapped = {"$ref": "#/$defs/O", "$defs": {"O": CLOSED_SCHEMA}}
        assert compare_definitions(OPEN_SCHEMA, wrapped, format="json-schema").compatible is False


class TestProtoFiles:
    OLD = 'syntax = "proto3"; message A { string x = 1; } message B { string y = 1; }'

    def test_change_in_the_second_message_is_seen(self) -> None:
        new = self.OLD.replace("string y = 1;", "int64 y = 1;")
        report = compare_definitions(self.OLD, new, format="protobuf")
        assert report.compatible is False
        assert {change.path for change in report.changes} == {"B.y"}

    def test_added_and_removed_messages(self) -> None:
        new = 'syntax = "proto3"; message A { string x = 1; } message C { string z = 1; }'
        report = compare_definitions(self.OLD, new, format="protobuf")
        assert {(change.path, change.change) for change in report.changes} == {
            ("B", ChangeType.FIELD_REMOVED),
            ("C", ChangeType.FIELD_ADDED),
        }
        assert report.compatible is True
        assert len(report.potentially_breaking_changes) == 1

    def test_single_message_paths_stay_short(self) -> None:
        old = 'syntax = "proto3"; message A { string x = 1; }'
        report = compare_definitions(old, old.replace("string x", "int64 x"), format="protobuf")
        assert report.changes[0].path == "x"

    def test_two_files_through_the_cli(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "old.proto").write_text(self.OLD, encoding="utf-8")
        new = self.OLD.replace("string y = 1;", "int64 y = 1;")
        (tmp_path / "new.proto").write_text(new, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert runner.invoke(app, ["compare", "old.proto", "new.proto"]).exit_code == 1


class TestRuleEntryPoints:
    @pytest.fixture
    def installed(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[object], None]]:
        from eventlineage.validation import engine

        def install(loaded: object) -> None:
            class Entry:
                name = "demo"

                def load(self) -> object:
                    if isinstance(loaded, Exception):
                        raise loaded
                    return loaded

            monkeypatch.setattr(engine.metadata, "entry_points", lambda **_: [Entry()])
            monkeypatch.setattr(engine, "_RULES", dict(engine._RULES))
            engine._load_entry_points.cache_clear()

        yield install
        engine._load_entry_points.cache_clear()

    def test_installed_rule_reaches_the_cli(
        self,
        installed: Callable[[object], None],
        project_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from eventlineage.validation import Rule

        class OwnerRule(Rule):
            id = "ACME001"
            name = "needs-owner"
            description = "Every event needs an owner."
            default_severity = Severity.ERROR

            def evaluate(self, ctx):  # type: ignore[no-untyped-def]
                for event in ctx.catalog.sorted_events():
                    yield self.finding(event.id, "no owner", subject_kind="event")

        installed(OwnerRule)
        monkeypatch.chdir(project_dir)
        assert "ACME001" in runner.invoke(app, ["rules"]).stdout
        output = runner.invoke(app, ["validate", "-f", "json"]).stdout
        assert "ACME001" in json.loads(output)["by_rule"]

    @pytest.mark.parametrize("loaded", [RuntimeError("broken"), object, ["not a rule"]])
    def test_a_broken_package_changes_nothing(
        self, installed: Callable[[object], None], loaded: object
    ) -> None:
        from eventlineage.validation import available_rules

        installed(loaded)
        assert len(available_rules()) == 17

    def test_a_clashing_id_cannot_replace_a_builtin(
        self, installed: Callable[[object], None]
    ) -> None:
        from eventlineage.validation import Rule, get_rule

        class Clash(Rule):
            id = "EG001"
            name = "clash"

            def evaluate(self, ctx):  # type: ignore[no-untyped-def]
                return ()

        installed(Clash)
        assert get_rule("EG001").name == "event-without-producer"
