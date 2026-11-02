"""Schema models: normalized field trees plus their source definition."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Any

from pydantic import Field, PrivateAttr, field_validator

from eventlineage.models.base import EGBaseModel
from eventlineage.models.common import SchemaFormat, SourceLocation, VersionStatus


class SchemaField(EGBaseModel):
    """One normalized field in a schema, independent of the source dialect.

    Every parser (JSON Schema, Avro, Protobuf) lowers into this shape so the
    compatibility engine only has to understand one vocabulary.
    """

    name: str
    type: str = Field(
        default="any",
        description="Normalized scalar/complex type name, e.g. 'string', 'int64', 'record', 'enum'",
    )
    required: bool = True
    nullable: bool = False
    repeated: bool = False
    has_default: bool = False
    default: Any = None
    enum_values: tuple[str, ...] = ()
    constraints: dict[str, Any] = Field(
        default_factory=dict,
        description="Validation constraints such as minLength, pattern, minimum",
    )
    fields: tuple[SchemaField, ...] = ()
    doc: str | None = None
    raw_type: str | None = Field(
        default=None, description="Type expression as written in the source"
    )
    json_pointer: str | None = None

    def walk(self, prefix: str = "") -> Iterator[tuple[str, SchemaField]]:
        """Yield ``(dotted_path, field)`` for this field and all descendants."""
        path = f"{prefix}.{self.name}" if prefix else self.name
        yield path, self
        for child in self.fields:
            yield from child.walk(path)

    def child(self, name: str) -> SchemaField | None:
        """Child field by name, or ``None``."""
        for candidate in self.fields:
            if candidate.name == name:
                return candidate
        return None

    @property
    def is_container(self) -> bool:
        """Whether this field can contain children."""
        return self.type in {"record", "object", "array", "map"} or bool(self.fields)


class ParsedSchema(EGBaseModel):
    """Result of lowering a raw schema definition into normalized fields."""

    format: SchemaFormat = SchemaFormat.UNKNOWN
    root_type: str = "record"
    fields: tuple[SchemaField, ...] = ()
    name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def walk(self) -> Iterator[tuple[str, SchemaField]]:
        """Iterate every field in the tree, depth first."""
        for field in self.fields:
            yield from field.walk()

    def field_map(self) -> dict[str, SchemaField]:
        """Map dotted field paths to fields."""
        return dict(self.walk())


class EventSchema(EGBaseModel):
    """A schema attached to an event version.

    ``definition`` holds the parsed document (dict/list) or, for textual
    dialects such as ``.proto``, the raw source. Fields are lowered lazily on
    first access and cached, which keeps scans of large projects cheap: a
    project with 10k events only pays the parsing cost for schemas that are
    actually inspected (validation, comparison, reports).
    """

    format: SchemaFormat = SchemaFormat.UNKNOWN
    definition: Any = None
    raw: str | None = None
    name: str | None = None
    version: str | None = None
    schema_id: str | None = Field(default=None, description="Registry-assigned schema id")
    source: SourceLocation | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    _parsed: ParsedSchema | None = PrivateAttr(default=None)
    _content_hash: str | None = PrivateAttr(default=None)

    @field_validator("format", mode="before")
    @classmethod
    def _coerce_format(cls, value: Any) -> Any:
        return SchemaFormat.coerce(value)

    def parsed(self) -> ParsedSchema:
        """Lower the definition to normalized fields (cached, lazy)."""
        if self._parsed is None:
            from eventlineage.parsers import parse_schema  # local import: avoids a cycle

            self._parsed = parse_schema(self)
        return self._parsed

    def normalized_fields(self) -> tuple[SchemaField, ...]:
        """Parsed fields in canonical order."""
        return self.parsed().fields

    def field_paths(self) -> tuple[str, ...]:
        """Every dotted field path in the schema."""
        return tuple(sorted(path for path, _ in self.parsed().walk()))

    def is_empty(self) -> bool:
        """Whether the schema declares no fields."""
        return self.definition is None and self.raw is None

    @property
    def content_hash(self) -> str:
        """Stable hash of the schema content (used for change detection)."""
        if self._content_hash is None:
            if self.definition is not None:
                payload = json.dumps(
                    self.definition, sort_keys=True, default=str, separators=(",", ":")
                )
            else:
                payload = (self.raw or "").strip()
            self._content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
        return self._content_hash

    def canonical(self) -> dict[str, Any]:
        """Deterministic dict representation, dropping bookkeeping fields."""
        return self.model_dump(mode="json", exclude_none=True, exclude={"metadata"})


class EventVersion(EGBaseModel):
    """A released version of an event contract."""

    version: str
    status: VersionStatus = VersionStatus.ACTIVE
    # Named ``schema`` on purpose: it is the field users read. It shadows
    # ``BaseModel.schema()``, which is deprecated in Pydantic v2.
    schema: EventSchema | None = None  # type: ignore[assignment]
    released_at: str | None = None
    notes: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def __str__(self) -> str:
        return self.version
