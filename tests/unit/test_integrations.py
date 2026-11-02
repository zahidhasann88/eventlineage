"""Integration adapters, exercised with injected fakes: no broker, no network."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from eventlineage import Project
from eventlineage.cli.main import app
from eventlineage.exceptions import IntegrationError, IntegrationUnavailableError
from eventlineage.integrations import (
    KafkaIntegration,
    NatsIntegration,
    RabbitMqIntegration,
    SchemaRegistryIntegration,
    available_integrations,
    get_integration,
    strip_subject_suffix,
)


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload


class FakeHttp:
    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes

    def get(self, path: str) -> FakeResponse:
        if path not in self.routes:
            return FakeResponse(None, 404)
        return FakeResponse(self.routes[path])


class TestRegistry:
    def test_every_adapter_reports_a_status(self) -> None:
        names = {status.name for status in available_integrations()}
        assert names == {"kafka", "nats", "rabbitmq", "schema-registry"}

    def test_unknown_integration(self) -> None:
        with pytest.raises(KeyError):
            get_integration("mqtt")

    def test_install_hint_names_a_real_extra(self) -> None:
        error = IntegrationUnavailableError("schema-registry", "httpx")
        pyproject = (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")
        assert error.hint == "pip install 'eventlineage[schema-registry]'"
        assert "\nschema-registry = [" in pyproject


class TestSchemaRegistry:
    def test_collect_builds_versioned_events(self) -> None:
        schema = {"type": "record", "name": "V", "fields": []}
        client = FakeHttp(
            {
                "/subjects": ["orders.created-value", "orders.created-key"],
                "/subjects/orders.created-value/versions": [1, 2],
                "/subjects/orders.created-value/versions/1": {
                    "version": 1,
                    "id": 10,
                    "schema": json.dumps(schema),
                },
                "/subjects/orders.created-value/versions/2": {
                    "version": 2,
                    "id": 11,
                    "schema": json.dumps({**schema, "doc": "v2"}),
                },
                "/subjects/orders.created-value/versions/latest": {
                    "version": 2,
                    "id": 11,
                    "schema": json.dumps({**schema, "doc": "v2"}),
                },
            }
        )
        result = SchemaRegistryIntegration("http://registry", client=client).collect()
        assert [event.id for event in result.events] == ["orders.created"]
        assert result.events[0].version == "2"
        assert [entry.version for entry in result.events[0].versions] == ["1", "2"]

    def test_http_error_is_an_integration_error(self) -> None:
        class Failing:
            def get(self, path: str) -> FakeResponse:
                return FakeResponse({}, 500)

        with pytest.raises(IntegrationError):
            SchemaRegistryIntegration("http://registry", client=Failing()).subjects()

    def test_strip_subject_suffix(self) -> None:
        assert strip_subject_suffix("a.b-value") == "a.b"
        assert strip_subject_suffix("a.b") == "a.b"


class TestRabbitMq:
    def test_collect_reads_bindings_and_skips_builtin_exchanges(self) -> None:
        client = FakeHttp(
            {
                "/exchanges/%2F": [{"name": "orders", "type": "topic"}, {"name": "amq.direct"}],
                "/exchanges/%2F/orders/bindings/source": [{"destination": "billing"}],
                "/queues/%2F": [{"name": "billing", "messages": 3}],
            }
        )
        result = RabbitMqIntegration("http://rabbit/api", client=client).collect()
        topics = {topic.name: topic for topic in result.topics}
        assert set(topics) == {"orders", "billing"}
        assert topics["orders"].consumers == ("billing",)


class FakeFuture:
    def __init__(self, value: Any) -> None:
        self.value = value

    def result(self, timeout: float | None = None) -> Any:
        return self.value


class FakeAdmin:
    def list_topics(self, timeout: float) -> Any:
        partition = SimpleNamespace(replicas=[1, 2, 3])
        metadata = SimpleNamespace(partitions={0: partition, 1: partition}, error=None)
        return SimpleNamespace(topics={"orders.v1": metadata})

    def describe_configs(self, resources: list[Any]) -> dict[str, FakeFuture]:
        entries = {
            "retention.ms": SimpleNamespace(value="1000", is_sensitive=False),
            "secret": SimpleNamespace(value="x", is_sensitive=True),
        }
        return {"resource": FakeFuture(entries)}


class TestKafka:
    def test_describe_topics_reads_config_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("eventlineage.integrations.kafka._resource", lambda name: name)
        result = KafkaIntegration("broker:9092", admin=FakeAdmin()).collect()
        topic = result.topics[0]
        assert (topic.partitions, topic.replication_factor) == (2, 3)
        assert topic.retention_ms == 1000
        assert topic.config == {"retention.ms": "1000"}

    def test_offline_export_needs_no_client(self, tmp_path: Path) -> None:
        path = tmp_path / "topics.json"
        path.write_text(json.dumps({"topics": [{"name": "a", "partitions": 1}]}), encoding="utf-8")
        assert [topic.name for topic in KafkaIntegration.from_export(path)] == ["a"]


class TestNats:
    def test_collect_maps_streams_to_topics(self) -> None:
        stream = SimpleNamespace(config=SimpleNamespace(name="ORDERS", subjects=["orders.*"]))

        class JetStream:
            async def streams_info(self) -> list[Any]:
                await asyncio.sleep(0)
                return [stream]

        class Client:
            def jetstream(self) -> JetStream:
                return JetStream()

        result = NatsIntegration(client=Client()).collect()
        assert result.topics[0].name == "ORDERS"
        assert result.topics[0].config["subjects"] == ["orders.*"]


class TestPull:
    runner = CliRunner()

    def test_environment_references_are_expanded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BROKER_PASSWORD", "s3cret")
        adapter = RabbitMqIntegration.from_settings(
            {"url": "http://rabbit/api", "password": "${BROKER_PASSWORD}"}
        )
        assert adapter.password == "s3cret"

    def test_missing_environment_variable_is_named(self) -> None:
        with pytest.raises(IntegrationError, match="EVENTLINEAGE_NOT_SET"):
            RabbitMqIntegration.from_settings({"password": "${EVENTLINEAGE_NOT_SET}"})

    def test_unknown_setting_is_an_integration_error(self) -> None:
        with pytest.raises(IntegrationError, match="invalid settings"):
            KafkaIntegration.from_settings({"bootstrap": "x"})

    def test_unknown_integration_is_a_usage_error(self) -> None:
        assert self.runner.invoke(app, ["pull", "mqtt"]).exit_code == 2

    def test_missing_settings_explain_themselves(self, tmp_path: Path) -> None:
        (tmp_path / "eventlineage.yaml").write_text("version: 1\n", encoding="utf-8")
        result = self.runner.invoke(app, ["pull", "rabbitmq", "--root", str(tmp_path)])
        assert result.exit_code == 3
        assert "integrations.rabbitmq.url" in result.stderr

    def test_topic_export_scans_back_with_no_broker(self, tmp_path: Path) -> None:
        client = FakeHttp(
            {
                "/exchanges/%2F": [{"name": "orders", "type": "topic"}],
                "/exchanges/%2F/orders/bindings/source": [{"destination": "billing"}],
                "/queues/%2F": [],
            }
        )
        adapter = RabbitMqIntegration("http://rabbit/api", client=client)
        document = adapter.export(adapter.collect())
        (tmp_path / "eventlineage.yaml").write_text("version: 1\n", encoding="utf-8")
        (tmp_path / "rabbit.json").write_text(json.dumps(document), encoding="utf-8")
        topic = Project.open(tmp_path).catalog().topics["orders"]
        assert (topic.broker, topic.consumers) == ("rabbitmq", ("billing",))

    def test_registry_export_scans_back_with_no_broker(self, tmp_path: Path) -> None:
        schema = json.dumps({"type": "record", "name": "V", "fields": []})
        client = FakeHttp(
            {
                "/subjects": ["o.c-value"],
                "/subjects/o.c-value/versions": [1],
                "/subjects/o.c-value/versions/1": {"version": 1, "id": 5, "schema": schema},
                "/subjects/o.c-value/versions/latest": {"version": 1, "id": 5, "schema": schema},
            }
        )
        adapter = SchemaRegistryIntegration("http://registry", client=client)
        (tmp_path / "eventlineage.yaml").write_text("version: 1\n", encoding="utf-8")
        (tmp_path / "subjects.json").write_text(
            json.dumps(adapter.export(adapter.collect())), encoding="utf-8"
        )
        event = Project.open(tmp_path).catalog().events["o.c"]
        assert event.active_schema().schema_id == "5"

    def test_include_keys_keeps_key_subjects_apart(self) -> None:
        schema = json.dumps({"type": "record", "name": "V", "fields": []})
        routes: dict[str, Any] = {"/subjects": ["o-key", "o-value"]}
        for subject in ("o-key", "o-value"):
            routes[f"/subjects/{subject}/versions"] = [1]
            for version in ("1", "latest"):
                routes[f"/subjects/{subject}/versions/{version}"] = {"version": 1, "schema": schema}
        client = FakeHttp(routes)
        adapter = SchemaRegistryIntegration("http://registry", client=client, include_keys=True)
        assert {event.id for event in adapter.collect().events} == {"o", "o-key"}

    def test_internal_kafka_topics_are_hidden(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("eventlineage.integrations.kafka._resource", lambda name: name)

        class Admin(FakeAdmin):
            def list_topics(self, timeout: float) -> Any:
                metadata = SimpleNamespace(partitions={}, error=None)
                return SimpleNamespace(topics={"__consumer_offsets": metadata, "a": metadata})

        names = [topic.name for topic in KafkaIntegration("b", admin=Admin()).describe_topics()]
        assert names == ["a"]
