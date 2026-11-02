"""Normalized internal models for EventLineage.

Every discovery plugin, parser and integration lowers its input into these
models; nothing downstream of discovery ever sees format-specific structures.
"""

from eventlineage.models.base import EGBaseModel
from eventlineage.models.common import (
    DependencyKind,
    Diagnostic,
    NodeKind,
    ParticipantKind,
    RelationshipKind,
    SchemaFormat,
    ServiceKind,
    Severity,
    SourceLocation,
    VersionStatus,
)
from eventlineage.models.dependency import Dependency, EventRelationship
from eventlineage.models.event import Event
from eventlineage.models.participant import Consumer, Producer, Service
from eventlineage.models.schema import EventSchema, EventVersion, ParsedSchema, SchemaField
from eventlineage.models.topic import Topic

__all__ = [
    "Consumer",
    "Dependency",
    "DependencyKind",
    "Diagnostic",
    "EGBaseModel",
    "Event",
    "EventRelationship",
    "EventSchema",
    "EventVersion",
    "NodeKind",
    "ParsedSchema",
    "ParticipantKind",
    "Producer",
    "RelationshipKind",
    "SchemaField",
    "SchemaFormat",
    "Service",
    "ServiceKind",
    "Severity",
    "SourceLocation",
    "Topic",
    "VersionStatus",
]
