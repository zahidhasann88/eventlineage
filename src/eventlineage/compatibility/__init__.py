"""Schema compatibility engine (JSON Schema, Avro, Protobuf)."""

from eventlineage.compatibility.avro import AvroComparer
from eventlineage.compatibility.base import (
    JsonSchemaComparer,
    OpenApiSchemaComparer,
    StructuralComparer,
)
from eventlineage.compatibility.engine import (
    breaking_changes,
    compare_definitions,
    compare_event_history,
    compare_event_versions,
    compare_schemas,
    comparer_for,
)
from eventlineage.compatibility.protobuf import ProtobufComparer, wire_group
from eventlineage.compatibility.result import (
    REMEDIATION,
    ChangeImpact,
    ChangeType,
    CompatibilityMode,
    CompatibilityReport,
    EventVersionComparison,
    SchemaChange,
)

__all__ = [
    "REMEDIATION",
    "AvroComparer",
    "ChangeImpact",
    "ChangeType",
    "CompatibilityMode",
    "CompatibilityReport",
    "EventVersionComparison",
    "JsonSchemaComparer",
    "OpenApiSchemaComparer",
    "ProtobufComparer",
    "SchemaChange",
    "StructuralComparer",
    "breaking_changes",
    "compare_definitions",
    "compare_event_history",
    "compare_event_versions",
    "compare_schemas",
    "comparer_for",
    "wire_group",
]
