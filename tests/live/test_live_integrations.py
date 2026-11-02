"""Adapters against real brokers: `pytest -m live`, with tests/live/docker-compose.yml up.

Each test creates what it expects to find, reads it back through `eventlineage pull`,
and scans the file, so the whole route from broker to catalog is covered.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from eventlineage import Project
from eventlineage.cli.main import app

pytestmark = pytest.mark.live

KAFKA = os.environ.get("EVENTLINEAGE_TEST_KAFKA", "localhost:9092")
REGISTRY = os.environ.get("EVENTLINEAGE_TEST_REGISTRY", "http://localhost:8081")
RABBIT = os.environ.get("EVENTLINEAGE_TEST_RABBITMQ", "http://localhost:15672/api")
NATS = os.environ.get("EVENTLINEAGE_TEST_NATS", "nats://localhost:4222")
runner = CliRunner()


def require(host: str, port: int) -> None:
    try:
        socket.create_connection((host, port), timeout=1).close()
    except OSError:
        pytest.skip(f"nothing listening on {host}:{port}; start tests/live/docker-compose.yml")


def pull(tmp_path: Path, name: str, config: str) -> tuple[Project, dict[str, Any]]:
    """Run the real CLI command, then scan what it wrote."""
    (tmp_path / "eventlineage.yaml").write_text(
        f"version: 1\nintegrations:\n{config}", encoding="utf-8"
    )
    target = tmp_path / "exports" / f"{name}.json"
    result = runner.invoke(app, ["pull", name, "--root", str(tmp_path), "--output", str(target)])
    assert result.exit_code == 0, result.output + str(result.exception)
    project = Project.open(tmp_path)
    project.scan(incremental=False)
    return project, json.loads(target.read_text(encoding="utf-8"))


def test_kafka(tmp_path: Path) -> None:
    require("localhost", 9092)
    from confluent_kafka.admin import AdminClient, NewTopic

    name = f"orders.{uuid.uuid4().hex[:8]}"
    admin = AdminClient({"bootstrap.servers": KAFKA})
    topic = NewTopic(name, num_partitions=3, replication_factor=1, config={"retention.ms": "60000"})
    admin.create_topics([topic])[name].result(timeout=20)
    try:
        project, document = pull(tmp_path, "kafka", f'  kafka:\n    bootstrap_servers: "{KAFKA}"\n')
    finally:
        admin.delete_topics([name])[name].result(timeout=20)

    assert not [item["name"] for item in document["topics"] if item["name"].startswith("_")]
    discovered = project.catalog().topics[name]
    assert discovered.broker == "kafka"
    assert (discovered.partitions, discovered.replication_factor) == (3, 1)
    assert discovered.retention_ms == 60000
    assert discovered.config["retention.ms"] == "60000"


def test_schema_registry(tmp_path: Path) -> None:
    require("localhost", 8081)
    subject = f"orders.{uuid.uuid4().hex[:8]}"
    value = f"{subject}-value"
    record = {"type": "record", "name": "Order", "fields": [{"name": "id", "type": "string"}]}
    wider = {
        **record,
        "fields": [*record["fields"], {"name": "note", "type": "string", "default": ""}],
    }
    headers = {"Content-Type": "application/vnd.schemaregistry.v1+json"}
    with httpx.Client(base_url=REGISTRY, headers=headers) as client:
        for schema in (record, wider):
            response = client.post(
                f"/subjects/{value}/versions", json={"schema": json.dumps(schema)}
            )
            response.raise_for_status()
        try:
            project, _ = pull(
                tmp_path, "schema-registry", f'  schema_registry:\n    url: "{REGISTRY}"\n'
            )
        finally:
            client.delete(f"/subjects/{value}")

    event = project.catalog().events[subject]
    assert event.version == "2"
    assert event.schema_versions() == ("2", "1")
    history = project.version_history(subject)
    assert history[0].report.compatible is True
    assert "note" in event.active_schema().field_paths()


def test_rabbitmq(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    require("localhost", 15672)
    suffix = uuid.uuid4().hex[:8]
    exchange, queue = f"orders-{suffix}", f"billing-{suffix}"
    with httpx.Client(base_url=RABBIT, auth=("guest", "guest")) as client:
        client.put(f"/exchanges/%2F/{exchange}", json={"type": "topic"}).raise_for_status()
        client.put(f"/queues/%2F/{queue}", json={"durable": True}).raise_for_status()
        client.post(
            f"/bindings/%2F/e/{exchange}/q/{queue}", json={"routing_key": "#"}
        ).raise_for_status()
        monkeypatch.setenv("EVENTLINEAGE_TEST_RABBIT_PASSWORD", "guest")
        try:
            project, _ = pull(
                tmp_path,
                "rabbitmq",
                f'  rabbitmq:\n    url: "{RABBIT}"\n    username: guest\n'
                "    password: ${EVENTLINEAGE_TEST_RABBIT_PASSWORD}\n",
            )
        finally:
            client.delete(f"/exchanges/%2F/{exchange}")
            client.delete(f"/queues/%2F/{queue}")

    topics = project.catalog().topics
    assert topics[exchange].broker == "rabbitmq"
    assert topics[exchange].consumers == (queue,)
    assert topics[queue].config["kind"] == "queue"


def test_nats(tmp_path: Path) -> None:
    require("localhost", 4222)
    import nats

    stream = f"ORDERS_{uuid.uuid4().hex[:8]}"

    async def manage(create: bool) -> None:
        client = await nats.connect(NATS)
        jetstream = client.jetstream()
        if create:
            await jetstream.add_stream(name=stream, subjects=[f"{stream}.>"])
        else:
            await jetstream.delete_stream(stream)
        await client.close()

    asyncio.run(manage(create=True))
    try:
        project, _ = pull(tmp_path, "nats", f'  nats:\n    servers: "{NATS}"\n')
    finally:
        asyncio.run(manage(create=False))

    topic = project.catalog().topics[stream]
    assert topic.broker == "nats"
    assert topic.config["subjects"] == [f"{stream}.>"]


def test_unreachable_broker_is_a_clean_failure(tmp_path: Path) -> None:
    (tmp_path / "eventlineage.yaml").write_text(
        'version: 1\nintegrations:\n  schema_registry:\n    url: "http://127.0.0.1:9"\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["pull", "schema-registry", "--root", str(tmp_path)])
    assert result.exit_code == 3
    assert "schema registry request failed" in result.stderr
