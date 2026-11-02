"""Discovery plugins and the registry that selects them.

All built-in plugins register themselves on import of this module, which happens
exactly once per process. Third-party plugins can register at import time as
well, or advertise themselves through the ``eventlineage.discovery`` entry-point
group.
"""

from eventlineage.discovery.asyncapi import AsyncApiPlugin
from eventlineage.discovery.base import (
    DiscoveryContext,
    DiscoveryPlugin,
    DiscoveryResult,
    coerce_participant,
    coerce_service,
)
from eventlineage.discovery.broker_exports import KafkaTopicsPlugin, SchemaRegistryExportPlugin
from eventlineage.discovery.catalog import CatalogPlugin
from eventlineage.discovery.openapi import OpenApiPlugin
from eventlineage.discovery.protobuf import ProtobufPlugin
from eventlineage.discovery.registry import (
    ENTRY_POINT_GROUP,
    PluginRegistry,
    available_plugins,
    default_registry,
    get_plugin,
    register_plugin,
)
from eventlineage.discovery.schema_docs import AvroPlugin, JsonSchemaPlugin
from eventlineage.discovery.service_config import ServiceConfigPlugin

BUILTIN_PLUGINS: tuple[type[DiscoveryPlugin], ...] = (
    AsyncApiPlugin,
    OpenApiPlugin,
    CatalogPlugin,
    SchemaRegistryExportPlugin,
    KafkaTopicsPlugin,
    ServiceConfigPlugin,
    AvroPlugin,
    JsonSchemaPlugin,
    ProtobufPlugin,
)

for _plugin in BUILTIN_PLUGINS:
    default_registry().register(_plugin)

__all__ = [
    "BUILTIN_PLUGINS",
    "ENTRY_POINT_GROUP",
    "AsyncApiPlugin",
    "AvroPlugin",
    "CatalogPlugin",
    "DiscoveryContext",
    "DiscoveryPlugin",
    "DiscoveryResult",
    "JsonSchemaPlugin",
    "KafkaTopicsPlugin",
    "OpenApiPlugin",
    "PluginRegistry",
    "ProtobufPlugin",
    "SchemaRegistryExportPlugin",
    "ServiceConfigPlugin",
    "available_plugins",
    "coerce_participant",
    "coerce_service",
    "default_registry",
    "get_plugin",
    "register_plugin",
]
