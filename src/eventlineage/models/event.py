"""The central event model."""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator, model_validator

from eventlineage.models.base import EGBaseModel
from eventlineage.models.common import SchemaFormat, SourceLocation, VersionStatus
from eventlineage.models.participant import Consumer, Producer
from eventlineage.models.schema import EventSchema, EventVersion
from eventlineage.utils.versioning import sort_versions, version_key


class Event(EGBaseModel):
    """A normalized event definition.

    Fragments of the same event may be discovered from several places (an
    Avro file, an AsyncAPI document and a manually maintained catalogue).
    :meth:`merge` combines them deterministically so a single event can
    accumulate producers, consumers and schema information from many sources.
    """

    id: str = Field(description="Unique, stable identifier; defaults to the name as written")
    name: str
    namespace: str = "default"
    version: str = "1.0.0"
    status: VersionStatus = VersionStatus.ACTIVE
    # Named ``schema`` on purpose: it is the field users read. It shadows
    # ``BaseModel.schema()``, which is deprecated in Pydantic v2.
    schema: EventSchema | None = None  # type: ignore[assignment]
    versions: tuple[EventVersion, ...] = ()
    producers: tuple[Producer, ...] = ()
    consumers: tuple[Consumer, ...] = ()
    topics: tuple[str, ...] = ()
    description: str | None = None
    owners: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: SourceLocation | None = None
    inferred: bool = Field(
        default=False, description="True when derived from usage rather than declared explicitly"
    )

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("event id must not be empty")
        if any(ch.isspace() for ch in cleaned):
            raise ValueError(f"event id must not contain whitespace: {cleaned!r}")
        return cleaned

    @field_validator("name", "namespace")
    @classmethod
    def _validate_name_like(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("event name/namespace must not be empty")
        return cleaned

    @model_validator(mode="before")
    @classmethod
    def _derive_namespace_from_name(cls, data: Any) -> Any:
        """``name='orders.created'`` implies ``namespace='orders'``.

        Keeps catalogue files compact without forcing every author to repeat
        the namespace twice.
        """
        if not isinstance(data, dict):
            return data
        raw_name = data.get("name")
        namespace = data.get("namespace")
        if "id" not in data and isinstance(raw_name, str):
            data = {**data, "id": raw_name}
        if isinstance(raw_name, str) and (namespace is None or namespace == "default"):
            namespace_part, separator, short = raw_name.rpartition(".")
            if separator and namespace_part and short:
                data = {**data, "name": short, "namespace": namespace_part}
        return data

    @property
    def qualified_name(self) -> str:
        """``namespace.name`` — the human-facing event name."""
        return f"{self.namespace}.{self.name}"

    @property
    def ref(self) -> str:
        """``namespace.name@version``."""
        return f"{self.qualified_name}@{self.version}"

    def identifiers(self) -> frozenset[str]:
        """All strings that may be used to reference this event."""
        return frozenset(
            {
                self.id,
                self.name,
                self.qualified_name,
                self.ref,
                f"{self.id}@{self.version}",
            }
        )

    def active_schema(self) -> EventSchema | None:
        """Schema of the active version, falling back to the newest version."""
        if self.schema is not None and not self.schema.is_empty():
            return self.schema
        for entry in self.all_versions():
            if entry.schema is not None and not entry.schema.is_empty():
                return entry.schema
        return None

    def all_versions(self) -> tuple[EventVersion, ...]:
        """Declared versions, newest first (implicit active version included)."""
        return tuple(
            sorted(self.version_entries(), key=lambda v: version_key(v.version), reverse=True)
        )

    def version_entries(self) -> tuple[EventVersion, ...]:
        """Declared versions plus an implicit entry for ``self.version``."""
        entries = list(self.versions)
        known = {entry.version for entry in entries}
        if self.version not in known:
            entries.append(
                EventVersion(version=self.version, schema=self.schema, status=self.status)
            )
        elif self.schema is not None:
            for index, entry in enumerate(entries):
                if entry.version == self.version and entry.schema is None:
                    entries[index] = entry.model_copy(update={"schema": self.schema})
        return tuple(entries)

    def schema_versions(self) -> tuple[str, ...]:
        """Declared version strings, newest first."""
        return tuple(entry.version for entry in self.all_versions())

    def version_count(self) -> int:
        """Number of declared versions."""
        return len(self.version_entries())

    def has_schema(self) -> bool:
        """Whether the active version has a schema."""
        return self.active_schema() is not None

    @property
    def producer_services(self) -> tuple[str, ...]:
        """Distinct producing services, sorted."""
        return tuple(sorted({p.service for p in self.producers}))

    @property
    def consumer_services(self) -> tuple[str, ...]:
        """Distinct consuming services, sorted."""
        return tuple(sorted({c.service for c in self.consumers}))

    @property
    def has_producer(self) -> bool:
        """Whether any producer is known."""
        return bool(self.producers)

    @property
    def has_consumer(self) -> bool:
        """Whether any consumer is known."""
        return bool(self.consumers)

    @property
    def is_orphaned(self) -> bool:
        """No known producer: nothing in the project emits this event."""
        return not self.producers

    @property
    def is_unconsumed(self) -> bool:
        """No known consumer: the event is published into the void."""
        return not self.consumers

    def participant_services(self) -> tuple[str, ...]:
        """Every service that produces or consumes the event."""
        return tuple(sorted({*self.producer_services, *self.consumer_services}))

    def supports_version(self, version: str) -> bool:
        """Whether ``version`` is a declared version of the event."""
        return version in {entry.version for entry in self.version_entries()}

    def merge(self, other: Event) -> Event:
        """Combine two fragments of the same event.

        Rules (deterministic and order independent for well-formed input):

        * scalar fields: the first *explicitly set* value wins,
        * participants/topics/versions/tags/owners: union, de-duplicated,
        * metadata: shallow merge, first value wins,
        * ``inferred`` becomes False as soon as one source declared the event.
        """
        from eventlineage.core.merge import merge_events

        return merge_events(self, other)

    def summary(self) -> dict[str, Any]:
        """Compact dict used by list commands and reports."""
        active = self.active_schema()
        return {
            "id": self.id,
            "name": self.qualified_name,
            "version": self.version,
            "versions": list(self.schema_versions()),
            "schema_format": active.format.value if active else SchemaFormat.UNKNOWN.value,
            "schema_hash": active.content_hash if active else None,
            "producers": list(self.producer_services),
            "consumers": list(self.consumer_services),
            "topics": list(self.topics),
            "source": str(self.source) if self.source else None,
            "status": self.status.value,
        }

    def sort_key(self) -> str:
        """Deterministic ordering key for reports."""
        return f"{self.namespace}.{self.name}"


def event_versions(event: Event) -> tuple[str, ...]:
    """Iterate every declared version entry."""
    return tuple(sort_versions(event.schema_versions()))
