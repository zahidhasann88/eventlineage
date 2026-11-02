"""Validation rule API.

A rule is a small, independent unit of analysis. Rules receive an immutable
:class:`ValidationContext` and yield :class:`Finding` objects; they never print,
never exit, and never mutate the catalog. That constraint is what makes them
safe to run in parallel, easy to test individually, and simple to extend.

Custom rules register with the same decorator as built-ins::

    from eventlineage import Severity
    from eventlineage.validation import Rule, ValidationContext, register_rule

    class TeamOwnershipRule(Rule):
        id = "CUSTOM001"
        name = "team-ownership"
        description = "Every event must declare an owning team"
        default_severity = Severity.WARNING

        def evaluate(self, ctx: ValidationContext):
            for event in ctx.catalog.sorted_events():
                if not event.owners:
                    yield self.finding(event.id, f"event {event.qualified_name!r} has no owners")

    register_rule(TeamOwnershipRule)
"""

from __future__ import annotations

import fnmatch
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, ClassVar

from pydantic import Field

from eventlineage.compatibility.result import CompatibilityMode
from eventlineage.config import Config, SuppressionConfig
from eventlineage.core.catalog import EventCatalog
from eventlineage.graph.builder import build_graph
from eventlineage.graph.model import Graph, event_node_id, service_node_id, topic_node_id
from eventlineage.models.base import EGBaseModel
from eventlineage.models.common import Severity, SourceLocation, VersionStatus


class Finding(EGBaseModel):
    """One validation result.

    Every finding carries the rule that produced it, a severity, a human
    readable message, the source location a developer should open, and — most
    importantly for a tool meant to be acted on — remediation guidance.
    """

    rule: str
    rule_name: str
    severity: Severity = Severity.WARNING
    message: str
    subject: str | None = Field(default=None, description="Event id, service or topic name")
    subject_kind: str | None = Field(default=None, description="event | service | topic")
    source: SourceLocation | None = None
    description: str | None = None
    remediation: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    tags: tuple[str, ...] = ()

    def identity(self) -> tuple[str, str, str]:
        """Return a stable identity string for this record."""
        return (self.rule, self.subject or "", self.message)

    def render(self) -> str:
        """Render the record as a single line of text."""
        location = f"{self.source}: " if self.source else ""
        return f"{self.severity.value.upper():7} {self.rule} {location}{self.message}"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready representation."""
        return {
            "rule": self.rule,
            "rule_name": self.rule_name,
            "severity": self.severity.value,
            "message": self.message,
            "subject": self.subject,
            "subject_kind": self.subject_kind,
            "source": str(self.source) if self.source else None,
            "description": self.description,
            "remediation": self.remediation,
            "details": self.details,
        }


class SuppressedFinding(EGBaseModel):
    """A finding hidden by configuration, kept for transparency."""

    rule: str
    subject: str | None = None
    message: str
    severity: Severity
    reason: str | None = None


@dataclass
class ValidationContext:
    """Everything a rule may look at.

    Graphs are built lazily: ``eventlineage validate`` on a 10k-event project only
    builds the views the enabled rules actually need.
    """

    catalog: EventCatalog
    config: Config = field(default_factory=Config)
    compatibility_mode: CompatibilityMode = CompatibilityMode.BACKWARD
    _graphs: dict[str, Graph] = field(default_factory=dict)

    def graph(self) -> Graph:
        """Full dependency graph (services, events, topics)."""
        if "full" not in self._graphs:
            self._graphs["full"] = build_graph(
                self.catalog,
                include_topics=self.config.analysis.include_topics,
            )
        return self._graphs["full"]

    def event_flow(self) -> Graph:
        """Event-to-event causal graph (explicit and inferred relationships)."""
        if "flow" not in self._graphs:
            self._graphs["flow"] = self.graph().filtered(kinds=("triggers", "derived_from"))
        return self._graphs["flow"]

    def service_flow(self) -> Graph:
        """Service-to-service dependency graph derived from event flows."""
        if "services" not in self._graphs:
            from eventlineage.graph.builder import service_flow_graph

            self._graphs["services"] = service_flow_graph(self.graph())
        return self._graphs["services"]

    def event_node(self, event_id: str) -> str:
        """Graph node id for an event reference."""
        return event_node_id(event_id)

    def service_node(self, service: str) -> str:
        """Graph node id for a service reference."""
        return service_node_id(service)

    def topic_node(self, topic: str) -> str:
        """Graph node id for a topic reference."""
        return topic_node_id(topic)

    def iter_events(self) -> Iterator[Any]:
        """Iterate catalog events in deterministic order."""
        return iter(self.catalog.sorted_events())

    def enabled(self, rule: Rule) -> bool:
        """Whether ``rule`` should run under the current configuration."""
        validation = self.config.validation
        if validation.only_rules and not self._matches(validation.only_rules, rule.id, rule.name):
            return False
        return not (
            validation.disabled_rules
            and self._matches(validation.disabled_rules, rule.id, rule.name)
        )

    def severity_for(self, rule: Rule) -> Severity:
        """Resolve the effective severity for a rule."""
        validation = self.config.validation
        for key, severity in validation.severity.items():
            if key.lower() in {rule.id.lower(), rule.name.lower()}:
                return Severity.coerce(severity)
        return rule.default_severity

    def suppression_for(self, finding: Finding) -> SuppressionConfig | None:
        """Return the suppression entry covering ``finding``, if any."""
        for suppression in self.config.validation.suppress:
            if not self._matches((suppression.rule,), finding.rule, finding.rule_name):
                continue
            if suppression.subject:
                subject = finding.subject or ""
                if not (
                    fnmatch.fnmatch(subject, suppression.subject) or subject == suppression.subject
                ):
                    continue
            if suppression.path:
                path = finding.source.path if finding.source else ""
                if not fnmatch.fnmatch(path, suppression.path):
                    continue
            return suppression
        return None

    @staticmethod
    def _matches(candidates: Iterable[str], *values: str | None) -> bool:
        normalised = {value.lower() for value in values if value}
        return any(str(candidate).lower() in normalised for candidate in candidates)

    @cached_property
    def tags(self) -> Mapping[str, int]:
        """Convenience index of tag -> event count (used by a few rules)."""
        counts: dict[str, int] = {}
        for event in self.catalog.sorted_events():
            for tag in event.tags:
                counts[tag] = counts.get(tag, 0) + 1
        return counts


class Rule(ABC):
    """Base class for validation rules."""

    #: Stable identifier, e.g. ``EG001``. Referenced by suppressions and severity maps.
    id: ClassVar[str] = ""
    #: Kebab-case name, also accepted in configuration.
    name: ClassVar[str] = ""
    #: One-sentence explanation shown by ``eventlineage rules``.
    description: ClassVar[str] = ""
    #: Severity used unless configuration overrides it.
    default_severity: ClassVar[Severity] = Severity.WARNING
    #: Guidance shown with every finding from this rule.
    remediation: ClassVar[str] = ""
    #: Free-form grouping tags (``graph``, ``schema``, ``hygiene``).
    tags: ClassVar[tuple[str, ...]] = ()

    @abstractmethod
    def evaluate(self, ctx: ValidationContext) -> Iterable[Finding]:
        """Yield findings for ``ctx``."""

    def finding(
        self,
        subject: str | None,
        message: str,
        *,
        subject_kind: str | None = None,
        source: SourceLocation | None = None,
        severity: Severity | None = None,
        details: dict[str, Any] | None = None,
        remediation: str | None = None,
    ) -> Finding:
        """Build a finding attributed to this rule."""
        return Finding(
            rule=self.id,
            rule_name=self.name,
            severity=severity or self.default_severity,
            message=message,
            subject=subject,
            subject_kind=subject_kind,
            source=source,
            description=self.description,
            remediation=remediation or self.remediation or None,
            details=details or {},
            tags=self.tags,
        )


def describe(rule: type[Rule]) -> dict[str, str]:
    """Metadata about a rule, used by ``eventlineage rules`` and docs."""
    return {
        "id": rule.id,
        "name": rule.name,
        "severity": rule.default_severity.value,
        "description": rule.description,
        "remediation": rule.remediation,
        "tags": ",".join(rule.tags),
    }


def severity_or_off(value: str | Severity | None) -> Severity:
    """Effective severity, or ``None`` when the rule is disabled."""
    return Severity.OFF if value is None else Severity.coerce(value)


__all__ = [
    "Finding",
    "Rule",
    "SuppressedFinding",
    "ValidationContext",
    "VersionStatus",
    "describe",
    "severity_or_off",
]
