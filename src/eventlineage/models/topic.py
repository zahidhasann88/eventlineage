"""Topic model: a broker-level routing destination."""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from eventlineage.models.base import EGBaseModel
from eventlineage.models.common import SourceLocation


class Topic(EGBaseModel):
    """A messaging destination (Kafka topic, RabbitMQ exchange, NATS subject)."""

    name: str
    broker: str | None = Field(default=None, description="kafka | rabbitmq | nats | custom")
    partitions: int | None = None
    replication_factor: int | None = None
    retention_ms: int | None = None
    cleanup_policy: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    producers: tuple[str, ...] = Field(
        default=(), description="Service names publishing to the topic"
    )
    consumers: tuple[str, ...] = Field(
        default=(), description="Service names subscribing to the topic"
    )
    events: tuple[str, ...] = Field((), description="Event identifiers routed over the topic")
    description: str | None = None
    source: SourceLocation | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("topic name must not be empty")
        return cleaned

    def merge(self, other: Topic) -> Topic:
        """Merge another declaration of the same topic into this one."""
        data = self.model_dump()
        incoming = other.model_dump(exclude_unset=True)
        for key, value in incoming.items():
            if key in {"name", "source"}:
                continue
            current = data.get(key)
            if isinstance(value, (list, tuple)) and current:
                data[key] = sorted({*current, *value})
            elif isinstance(value, dict) and isinstance(current, dict):
                data[key] = {**value, **current}
            elif current in (None, (), [], "") and value not in (None, (), [], ""):
                data[key] = value
        return Topic.model_validate(data)
