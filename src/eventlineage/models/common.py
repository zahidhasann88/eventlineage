"""Enumerations and small value objects shared across the model layer."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from eventlineage.models.base import EGBaseModel


class SchemaFormat(StrEnum):
    """Schema dialects EventLineage understands natively."""

    JSON_SCHEMA = "json-schema"
    AVRO = "avro"
    PROTOBUF = "protobuf"
    OPENAPI = "openapi"
    UNKNOWN = "unknown"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @classmethod
    def coerce(cls, value: str | SchemaFormat | None) -> SchemaFormat:
        """Parse a schema format from configuration, CLI input or a file name.

        Unknown spellings degrade to :attr:`UNKNOWN` instead of raising: a
        partially understood repository should still be scannable.
        """
        if isinstance(value, SchemaFormat):
            return value
        if value is None:
            return cls.UNKNOWN
        key = str(value).strip().lower().replace("_", "-")
        aliases = {
            "jsonschema": cls.JSON_SCHEMA,
            "json": cls.JSON_SCHEMA,
            "avro": cls.AVRO,
            "avsc": cls.AVRO,
            "protobuf": cls.PROTOBUF,
            "proto": cls.PROTOBUF,
            "openapi": cls.OPENAPI,
            "unknown": cls.UNKNOWN,
        }
        if key in aliases:
            return aliases[key]
        try:
            return cls(key)
        except ValueError:
            return cls.UNKNOWN


class VersionStatus(StrEnum):
    """Lifecycle status of a specific event version."""

    ACTIVE = "active"
    DEPRECATED = "deprecated"
    RETIRED = "retired"


class Severity(StrEnum):
    """Severity of a validation finding."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    OFF = "off"

    @property
    def rank(self) -> int:
        """Ordering used for thresholds (``--fail-on``)."""
        return _SEVERITY_RANK[self]

    @classmethod
    def coerce(cls, value: str | Severity | bool | None) -> Severity:
        """Parse a severity from configuration or CLI input.

        ``off``/``false``/``none`` are all accepted for "disabled", because YAML
        parses a bare ``off`` as a boolean.
        """
        if isinstance(value, Severity):
            return value
        if value is None:
            return cls.OFF
        if isinstance(value, bool):
            return cls.ERROR if value else cls.OFF
        text = str(value).strip().lower()
        if text in {"off", "false", "no", "none", "disabled", "ignore"}:
            return cls.OFF
        try:
            return cls(text)
        except ValueError as exc:
            raise ValueError(f"unknown severity: {value!r}") from exc


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.OFF: 0,
    Severity.INFO: 1,
    Severity.WARNING: 2,
    Severity.ERROR: 3,
}


class NodeKind(StrEnum):
    """Kinds of vertices in the dependency graph."""

    SERVICE = "service"
    EVENT = "event"
    TOPIC = "topic"


class ParticipantKind(StrEnum):
    """What sort of actor produces or consumes an event."""

    SERVICE = "service"
    APPLICATION = "application"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


class ServiceKind(StrEnum):
    """What kind of participant a service is."""

    SERVICE = "service"
    GATEWAY = "gateway"
    WORKER = "worker"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


class DependencyKind(StrEnum):
    """Semantic edge kinds in the dependency graph."""

    PRODUCES = "produces"
    CONSUMES = "consumes"
    PUBLISHES_TO = "publishes_to"
    SUBSCRIBES_TO = "subscribes_to"
    ROUTES_VIA = "routes_via"
    TRIGGERS = "triggers"
    DERIVED_FROM = "derived_from"
    DEPENDS_ON = "depends_on"

    @property
    def is_event_flow(self) -> bool:
        """Whether the edge describes a causal event-to-event flow."""
        return self in {DependencyKind.TRIGGERS, DependencyKind.DERIVED_FROM}


class RelationshipKind(StrEnum):
    """How two events relate to one another."""

    TRIGGERS = "triggers"
    DERIVED_FROM = "derived_from"
    CORRELATES = "correlates"


class SourceLocation(EGBaseModel):
    """Where a definition was found, used in every diagnostic for navigation."""

    path: str
    line: int | None = None
    column: int | None = None
    pointer: str | None = Field(
        default=None,
        description="JSON-pointer-like locator inside the document (e.g. '/channels/orders')",
    )
    plugin: str | None = Field(
        default=None, description="Discovery plugin that produced the record"
    )

    @field_validator("path")
    @classmethod
    def _normalise_path(cls, value: str) -> str:
        return value.replace("\\", "/")

    def __str__(self) -> str:
        rendered = self.path
        if self.line is not None:
            rendered += f":{self.line}"
            if self.column is not None:
                rendered += f":{self.column}"
        if self.pointer:
            rendered += f"#{self.pointer}"
        return rendered

    @property
    def is_external(self) -> bool:
        """True for pseudo-locations that do not point at a real file."""
        return self.path.startswith("<") and self.path.endswith(">")

    @classmethod
    def synthetic(cls, label: str, plugin: str | None = None) -> SourceLocation:
        """Location for records that were not read from a real file."""
        return cls(path=f"<{label}>", plugin=plugin)

    def as_dict(self) -> dict[str, Any]:
        """Return the value as a plain mapping."""
        return self.to_dict()


class Diagnostic(EGBaseModel):
    """A non-fatal problem encountered while scanning or analyzing."""

    severity: Severity = Severity.WARNING
    message: str
    source: SourceLocation | None = None
    code: str | None = None

    def __str__(self) -> str:
        location = f"{self.source}: " if self.source else ""
        code = f"[{self.code}] " if self.code else ""
        return f"{self.severity.value}: {location}{code}{self.message}"
