"""Producers, consumers and services."""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from eventlineage.models.base import EGBaseModel
from eventlineage.models.common import ParticipantKind, ServiceKind, SourceLocation


class _ParticipantBase(EGBaseModel):
    """Common fields for anything that publishes or subscribes to an event."""

    service: str = Field(description="Name of the producing/consuming service")
    kind: ParticipantKind = ParticipantKind.SERVICE
    event_id: str | None = Field(
        default=None,
        description=(
            "Event identifier; lets a consumer reference an event that is not declared elsewhere"
        ),
    )
    schema_version: str | None = Field(
        default=None,
        description="Event version this participant declares support for",
    )
    topics: tuple[str, ...] = ()
    description: str | None = None
    source: SourceLocation | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("service")
    @classmethod
    def _validate_service(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("participant service name must not be empty")
        return cleaned

    def identity(self) -> tuple[str, ...]:
        """Stable identity used for de-duplication during merging."""
        return (self.__class__.__name__, self.service, self.schema_version or "", str(self.kind))


class Producer(_ParticipantBase):
    """A service that emits an event."""

    def identity(self) -> tuple[str, ...]:
        """Return the ``(service, version)`` identity of this participant."""
        return (*super().identity(), ",".join(sorted(self.topics)))


class Consumer(_ParticipantBase):
    """A service that reacts to an event."""

    group: str | None = Field(default=None, description="Consumer group / queue name")

    def identity(self) -> tuple[str, ...]:
        """Return the ``(service, version)`` identity of this participant."""
        return (*super().identity(), self.group or "")


class Service(EGBaseModel):
    """A deployable unit that produces and/or consumes events."""

    name: str
    kind: ServiceKind = ServiceKind.SERVICE
    description: str | None = None
    repository: str | None = None
    language: str | None = None
    owners: tuple[str, ...] = ()
    tier: str | None = Field(
        default=None,
        description="Criticality tier (e.g. 'tier-1'); feeds impact risk classification",
    )
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: SourceLocation | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("service name must not be empty")
        return cleaned

    def merge(self, other: Service) -> Service:
        """Merge a partially specified duplicate definition into this one."""
        data = self.model_dump()
        incoming = other.model_dump(exclude_unset=True)
        for key, value in incoming.items():
            if key in {"name", "source"}:
                continue
            current = data.get(key)
            if isinstance(value, (list, tuple)) and current:
                merged = list({*current, *value})
                data[key] = sorted(merged)
            elif key == "metadata":
                data[key] = {**value, **(current or {})}
            elif key == "kind":
                # ``service`` is the default, so anything else was stated on purpose.
                data[key] = value if current is ServiceKind.SERVICE else current
            elif current in (None, (), [], "") and value not in (None, (), [], ""):
                data[key] = value
        return Service.model_validate(data)
