"""Optional infrastructure integrations.

Nothing in this package is imported by the core engine; adapters are loaded on
demand by the CLI (``eventlineage integrations``) or explicitly by SDK users.
"""

from eventlineage.integrations.base import (
    Integration,
    IntegrationResult,
    IntegrationStatus,
)
from eventlineage.integrations.kafka import KafkaIntegration
from eventlineage.integrations.nats import NatsIntegration
from eventlineage.integrations.rabbitmq import RabbitMqIntegration
from eventlineage.integrations.schema_registry import (
    SchemaRegistryIntegration,
    strip_subject_suffix,
)

INTEGRATIONS: dict[str, type[Integration]] = {
    KafkaIntegration.name: KafkaIntegration,
    SchemaRegistryIntegration.name: SchemaRegistryIntegration,
    RabbitMqIntegration.name: RabbitMqIntegration,
    NatsIntegration.name: NatsIntegration,
}


def available_integrations() -> tuple[IntegrationStatus, ...]:
    """Status of every known integration, for ``eventlineage integrations``."""
    return tuple(INTEGRATIONS[name].status() for name in sorted(INTEGRATIONS))


def get_integration(name: str) -> type[Integration]:
    """Return the adapter class registered under ``name``."""
    if name not in INTEGRATIONS:
        raise KeyError(name)
    return INTEGRATIONS[name]


__all__ = [
    "INTEGRATIONS",
    "Integration",
    "IntegrationResult",
    "IntegrationStatus",
    "KafkaIntegration",
    "NatsIntegration",
    "RabbitMqIntegration",
    "SchemaRegistryIntegration",
    "available_integrations",
    "get_integration",
    "strip_subject_suffix",
]
