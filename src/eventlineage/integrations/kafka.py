"""Apache Kafka integration.

Two modes, deliberately separated:

*Live* — enumerate topics and their configurations through the admin API
(requires ``confluent-kafka``).
*Offline* — read a topic export file. This is what CI uses, and it works with no
cluster at all because parsing lives in ``eventlineage.discovery``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, ClassVar

from eventlineage.config import Config
from eventlineage.discovery.base import DiscoveryContext
from eventlineage.discovery.broker_exports import KafkaTopicsPlugin
from eventlineage.exceptions import IntegrationError
from eventlineage.integrations.base import Integration, IntegrationResult
from eventlineage.models.topic import Topic


class KafkaIntegration(Integration):
    """Kafka topic discovery through the admin API or an export file."""

    name: ClassVar[str] = "kafka"
    requires: ClassVar[str | None] = "confluent_kafka"
    description: ClassVar[str] = "Apache Kafka topics, partitions and topic configuration"

    def __init__(
        self,
        bootstrap_servers: str | Sequence[str] | None = None,
        *,
        admin: Any | None = None,
        timeout: float = 10.0,
        config: dict[str, Any] | None = None,
        include_internal: bool = False,
    ) -> None:
        super().__init__(bootstrap_servers=bootstrap_servers)
        if isinstance(bootstrap_servers, (list, tuple)):
            bootstrap_servers = ",".join(bootstrap_servers)
        self.bootstrap_servers = bootstrap_servers
        self.timeout = timeout
        self._admin = admin
        self._config = config or {}
        self.include_internal = include_internal

    def admin(self) -> Any:
        """Lazy admin client (overridable for tests)."""
        if self._admin is not None:
            return self._admin
        module = self.require_dependency()
        if not self.bootstrap_servers:
            raise IntegrationError(
                "kafka integration requires 'bootstrap_servers'",
                hint="set integrations.kafka.bootstrap_servers in eventlineage.yaml",
            )
        self._admin = module.admin.AdminClient(
            {"bootstrap.servers": self.bootstrap_servers, **self._config}
        )
        return self._admin

    def describe_topics(self, names: Iterable[str] | None = None) -> tuple[Topic, ...]:
        """Return topics with partition counts and configuration."""
        admin = self.admin()
        try:
            metadata = admin.list_topics(timeout=self.timeout)
        except Exception as exc:
            raise IntegrationError(f"could not list Kafka topics: {exc}") from exc
        selected = set(names) if names else None
        topics: list[Topic] = []
        topic_map = getattr(metadata, "topics", {}) or {}
        for name, topic_metadata in sorted(topic_map.items()):
            if selected is not None and name not in selected:
                continue
            # ``__consumer_offsets`` and friends, and the Schema Registry's own log.
            if not self.include_internal and name.startswith("_"):
                continue
            if str(getattr(topic_metadata, "error", "") or ""):
                continue
            partitions = len(getattr(topic_metadata, "partitions", {}) or {})
            config = self.topic_config(name)
            topics.append(
                Topic(
                    name=name,
                    broker="kafka",
                    partitions=partitions,
                    replication_factor=_replication_factor(topic_metadata),
                    retention_ms=_int_or_none(config.get("retention.ms")),
                    cleanup_policy=config.get("cleanup.policy"),
                    config=config,
                    metadata={"cluster": self.bootstrap_servers},
                    source=self.source(f"kafka://{self.bootstrap_servers}/{name}"),
                )
            )
        return tuple(topics)

    def topic_config(self, name: str) -> dict[str, str]:
        """Topic configuration as a plain mapping."""
        admin = self.admin()
        describe = getattr(admin, "describe_configs", None)
        if describe is None:
            return {}
        try:
            futures = describe([_resource(name)])
        except Exception:  # pragma: no cover - broker-specific
            return {}
        result: dict[str, str] = {}
        for future in futures.values():
            try:
                entry = future.result(timeout=self.timeout)
            except Exception:  # pragma: no cover - broker-specific
                continue
            # ``describe_configs`` resolves to ``{name: ConfigEntry}``.
            for key, config_entry in entry.items():
                if getattr(config_entry, "is_sensitive", False):
                    continue
                value = getattr(config_entry, "value", config_entry)
                result[key] = "" if value is None else str(value)
        return dict(sorted(result.items()))

    @classmethod
    def from_export(cls, path: Path, *, config: Config | None = None) -> tuple[Topic, ...]:
        """Parse a topic export file without touching a broker."""
        config = config or Config()
        context = DiscoveryContext(root=path.parent, config=config)
        result = KafkaTopicsPlugin().discover(path, context)
        return tuple(result.topics)

    def collect(self) -> IntegrationResult:
        """Fetch topic metadata from the broker or an export file."""
        result = IntegrationResult()
        for topic in self.describe_topics():
            result.add_topic(topic)
        result.notes.append(f"discovered {len(result.topics)} Kafka topic(s)")
        return result


def _resource(name: str) -> Any:
    """Build a ``ConfigResource`` for ``describe_configs`` across client versions."""
    module = __import__("confluent_kafka.admin", fromlist=["ConfigResource"])
    resource_type = module.RESOURCE_TOPIC
    return module.ConfigResource(resource_type, name)


def _replication_factor(topic_metadata: Any) -> int | None:
    partitions = getattr(topic_metadata, "partitions", {}) or {}
    replicas = [len(getattr(partition, "replicas", []) or []) for partition in partitions.values()]
    return min(replicas) if replicas else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["KafkaIntegration"]
