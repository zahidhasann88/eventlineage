"""Explicit dependencies and event-to-event relationships."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from eventlineage.models.base import EGBaseModel
from eventlineage.models.common import (
    DependencyKind,
    RelationshipKind,
    SourceLocation,
)


class Dependency(EGBaseModel):
    """A directed, typed dependency between two graph nodes.

    Node references use the graph's canonical form: ``service:<name>``,
    ``event:<id>`` or ``topic:<name>``.
    """

    source: str
    target: str
    kind: DependencyKind
    source_location: SourceLocation | None = Field(
        default=None,
        alias="source_location",
        description="Where the dependency was declared/discovered",
    )
    event_id: str | None = None
    service: str | None = None
    topic: str | None = None
    inferred: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    def key(self) -> tuple[str, str, str]:
        """Return the ``(source, target, kind)`` identity of this edge."""
        return (self.source, self.target, self.kind.value)

    def __str__(self) -> str:
        return f"{self.source} -[{self.kind.value}]-> {self.target}"


class EventRelationship(EGBaseModel):
    """A causal relationship between two events.

    Relationships are either declared explicitly (a catalogue entry such as
    ``order.created triggers order.validated``) or inferred from service
    behaviour (service consumes A and produces B). Inferred relationships are
    what make event-level cycle detection useful in practice.
    """

    source_event: str
    target_event: str
    kind: RelationshipKind = RelationshipKind.TRIGGERS
    inferred: bool = False
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    via_service: str | None = Field(
        default=None, description="Service bridging the two events when inferred"
    )
    source_location: SourceLocation | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def key(self) -> tuple[str, str, str]:
        """Return the ``(source, target, kind)`` identity of this edge."""
        return (self.source_event, self.target_event, self.kind.value)

    def __str__(self) -> str:
        marker = " (inferred)" if self.inferred else ""
        return f"{self.source_event} -[{self.kind.value}]-> {self.target_event}{marker}"
