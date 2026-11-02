"""Service-centric configuration files.

Teams often document their platform as a list of services with the events they
publish and subscribe to, leaving the event definitions themselves to schemas or
a catalog. This plugin turns those declarations into events plus producer and
consumer links; events created here are marked ``inferred`` so validation can
tell them apart from declared contracts.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from eventlineage.discovery.base import (
    DiscoveryContext,
    DiscoveryPlugin,
    DiscoveryResult,
    first_json_keys,
    yaml_top_level_keys,
)
from eventlineage.discovery.catalog import parse_service_specs
from eventlineage.exceptions import DiscoveryError
from eventlineage.models.common import ServiceKind
from eventlineage.models.participant import Service


class ServiceConfigPlugin(DiscoveryPlugin):
    """Turns ``services: {name: {produces: [...], consumes: [...]}}`` into events."""

    name: ClassVar[str] = "service-config"
    patterns: ClassVar[tuple[str, ...]] = ("*.yaml", "*.yml", "*.json")
    priority: ClassVar[int] = 90
    description: ClassVar[str] = "Service-centric config: services with produces/consumes lists"
    extensions: ClassVar[tuple[str, ...]] = (".yaml", ".yml", ".json")

    def detect(self, path: Path, head: str) -> int:
        """Return the detection confidence (0-100) for ``path``."""
        keys = (
            first_json_keys(head)
            if head.lstrip().startswith(("{", "["))
            else yaml_top_level_keys(head)
        )
        if "services" not in keys:
            return 0
        if "asyncapi" in keys or "openapi" in keys or "events" in keys:
            return 0
        lowered = head.lower()
        if "produces" in lowered or "consumes" in lowered or "publishes" in lowered:
            return 70
        return 0

    def discover(self, path: Path, ctx: DiscoveryContext) -> DiscoveryResult:
        """Parse ``path`` and return the catalog fragments it declares."""
        document = self.load_json_or_yaml(path, ctx)
        if not isinstance(document, Mapping):
            raise DiscoveryError("service configuration root must be a mapping")
        result = DiscoveryResult()
        source = self.source(path, ctx)

        services, events, _links = parse_service_specs(
            document.get("services"), source_for=lambda _name: source
        )
        for service in services:
            result.add_service(service)
        for event in events:
            result.add_event(event)

        # Optional top-level inventories let a platform file declare services that
        # do not (yet) produce or consume anything, e.g. external systems.
        for name in _names(document.get("external_services")):
            result.add_service(
                Service(
                    name=name,
                    kind=ServiceKind.EXTERNAL,
                    metadata={"external": True},
                    source=source,
                )
            )
        for name in _names(document.get("producers")):
            result.add_service(Service(name=name, metadata={"declared": True}, source=source))
        for name in _names(document.get("consumers")):
            result.add_service(Service(name=name, metadata={"declared": True}, source=source))
        return result


def _names(node: Any) -> list[str]:
    if node is None:
        return []
    if isinstance(node, Mapping):
        return [str(key) for key in node]
    if isinstance(node, (list, tuple)):
        names: list[str] = []
        for item in node:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, Mapping) and "name" in item:
                names.append(str(item["name"]))
        return names
    return []


__all__ = ["ServiceConfigPlugin"]
