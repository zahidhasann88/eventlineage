"""Built-in validation rules.

Each rule is independent and stateless; the engine decides which ones run, what
severity they report at, and whether a finding is suppressed. Rule ids are
stable and part of the public contract (they appear in configuration files and
in CI gate settings).
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from typing import ClassVar, cast

from eventlineage.compatibility.engine import compare_event_versions, compare_schemas
from eventlineage.models.common import SchemaFormat, Severity, VersionStatus
from eventlineage.models.schema import EventSchema
from eventlineage.utils.versioning import version_key
from eventlineage.validation.rule import Finding, Rule, ValidationContext


class OrphanEventRule(Rule):
    """An event nobody produces cannot actually happen."""

    id: ClassVar[str] = "EG001"
    name: ClassVar[str] = "event-without-producer"
    description: ClassVar[str] = "An event has no known producer, so it can never be emitted."
    default_severity: ClassVar[Severity] = Severity.WARNING
    remediation: ClassVar[str] = (
        "Declare the producing service in the event catalog or in the service that emits it; "
        "if the event comes from outside this repository, mark it as external."
    )
    tags: ClassVar[tuple[str, ...]] = ("graph", "completeness")

    def evaluate(self, ctx: ValidationContext) -> Iterable[Finding]:
        """Yield every finding this rule detects in the catalog."""
        for event in ctx.catalog.sorted_events():
            if event.producers or not event.consumers:
                continue
            yield self.finding(
                event.id,
                f"event {event.qualified_name!r} has {len(event.consumers)} consumer(s) "
                "but no known producer",
                subject_kind="event",
                source=event.source,
                details={"consumers": list(event.consumer_services)},
            )


class EventWithoutConsumerRule(Rule):
    """Events published into the void are usually a wiring mistake."""

    id: ClassVar[str] = "EG002"
    name: ClassVar[str] = "event-without-consumer"
    description: ClassVar[str] = (
        "An event has no known consumer; it is published but never handled."
    )
    default_severity: ClassVar[Severity] = Severity.INFO
    remediation: ClassVar[str] = (
        "Add the consuming service, or mark the event as intentionally broadcast "
        "(for example audit data consumed outside the tracked services)."
    )
    tags: ClassVar[tuple[str, ...]] = ("graph", "completeness")

    def evaluate(self, ctx: ValidationContext) -> Iterable[Finding]:
        """Yield every finding this rule detects in the catalog."""
        for event in ctx.catalog.sorted_events():
            if event.consumers or not event.producers:
                continue
            yield self.finding(
                event.id,
                f"event {event.qualified_name!r} has {len(event.producers)} producer(s) "
                "but no known consumer",
                subject_kind="event",
                source=event.source,
                details={"producers": list(event.producer_services)},
            )


class UnknownEventReferenceRule(Rule):
    """Dangling references break the graph silently, so they are errors."""

    id: ClassVar[str] = "EG003"
    name: ClassVar[str] = "unknown-event-reference"
    description: ClassVar[str] = (
        "A consumer, relationship or topic references an event that is not defined."
    )
    default_severity: ClassVar[Severity] = Severity.ERROR
    remediation: ClassVar[str] = (
        "Fix the reference (usually a typo or a stale name) or add the missing event definition."
    )
    tags: ClassVar[tuple[str, ...]] = ("graph", "correctness")

    def evaluate(self, ctx: ValidationContext) -> Iterable[Finding]:
        """Yield every finding this rule detects in the catalog."""
        catalog = ctx.catalog
        for event in catalog.sorted_events():
            for consumer in event.consumers:
                if consumer.event_id and consumer.event_id not in catalog.events:
                    yield self.finding(
                        consumer.event_id,
                        f"consumer service {consumer.service!r} references unknown event "
                        f"{consumer.event_id!r}",
                        subject_kind="event",
                        source=consumer.source or event.source,
                        details={"service": consumer.service},
                    )
            # A name that only ever appeared in a ``consumes`` list: nothing declares,
            # describes or emits it, which is what a typo looks like.
            if event.inferred and not event.producers and not event.has_schema():
                for consumer in event.consumers:
                    yield self.finding(
                        event.id,
                        f"consumer service {consumer.service!r} references unknown event "
                        f"{event.id!r}",
                        subject_kind="event",
                        source=consumer.source or event.source,
                        details={"service": consumer.service},
                    )
        known = set(catalog.events)
        for relationship in catalog.relationships:
            missing = [
                reference
                for reference in (relationship.source_event, relationship.target_event)
                if reference not in known
            ]
            if missing:
                yield self.finding(
                    missing[0],
                    f"relationship {relationship} references undefined event(s): "
                    f"{', '.join(missing)}",
                    subject_kind="event",
                    source=relationship.source_location,
                    details={"missing": missing},
                )
        for topic in catalog.sorted_topics():
            for event_id in topic.events:
                if event_id not in known:
                    yield self.finding(
                        topic.name,
                        f"topic {topic.name!r} lists undefined event {event_id!r}",
                        subject_kind="topic",
                        source=topic.source,
                        details={"event": event_id},
                    )


class DuplicateEventRule(Rule):
    """The same event id is declared in several files."""

    id: ClassVar[str] = "EG004"
    name: ClassVar[str] = "duplicate-event-definition"
    description: ClassVar[str] = "The same event id is declared in more than one place."
    default_severity: ClassVar[Severity] = Severity.INFO
    remediation: ClassVar[str] = (
        "Merging declarations is supported and often intended, but two definitions that "
        "disagree should be consolidated into one source of truth."
    )
    tags: ClassVar[tuple[str, ...]] = ("hygiene",)

    def evaluate(self, ctx: ValidationContext) -> Iterable[Finding]:
        """Yield every finding this rule detects in the catalog."""
        for duplicate in ctx.catalog.duplicates:
            if duplicate.kind != "event":
                continue
            event = ctx.catalog.events.get(duplicate.key)
            yield self.finding(
                duplicate.key,
                f"event {duplicate.key!r} is declared {duplicate.occurrences} times "
                f"({', '.join(duplicate.sources) or 'unknown sources'})",
                subject_kind="event",
                source=event.source if event else None,
                details={"sources": list(duplicate.sources)},
            )


class DuplicateTopicRule(Rule):
    """The same topic is configured in several files."""

    id: ClassVar[str] = "EG005"
    name: ClassVar[str] = "duplicate-topic-definition"
    description: ClassVar[str] = "The same topic is configured in more than one file."
    default_severity: ClassVar[Severity] = Severity.INFO
    remediation: ClassVar[str] = (
        "Keep topic configuration (partitions, retention) in one place to avoid "
        "conflicting broker settings."
    )
    tags: ClassVar[tuple[str, ...]] = ("hygiene",)

    def evaluate(self, ctx: ValidationContext) -> Iterable[Finding]:
        """Yield every finding this rule detects in the catalog."""
        for duplicate in ctx.catalog.duplicates:
            if duplicate.kind != "topic":
                continue
            topic = ctx.catalog.topics.get(duplicate.key)
            yield self.finding(
                duplicate.key,
                f"topic {duplicate.key!r} is defined {duplicate.occurrences} times "
                f"({', '.join(duplicate.sources) or 'unknown sources'})",
                subject_kind="topic",
                source=topic.source if topic else None,
                details={"sources": list(duplicate.sources)},
            )


class CircularEventDependencyRule(Rule):
    """A cycle in the event flow means no stable ordering exists."""

    id: ClassVar[str] = "EG006"
    name: ClassVar[str] = "circular-event-dependency"
    description: ClassVar[str] = "Events trigger each other in a cycle."
    default_severity: ClassVar[Severity] = Severity.ERROR
    remediation: ClassVar[str] = (
        "Break the cycle by introducing an intermediate event, moving one step to a "
        "scheduled job, or making one consumer idempotent and asynchronous."
    )
    tags: ClassVar[tuple[str, ...]] = ("graph", "correctness")

    def evaluate(self, ctx: ValidationContext) -> Iterable[Finding]:
        """Yield every finding this rule detects in the catalog."""
        from eventlineage.graph.algorithms import find_cycle_groups

        flow = ctx.event_flow()
        for group in find_cycle_groups(flow, max_groups=25):
            members = sorted(flow.node(index).ref for index in group.nodes)
            example = [flow.node(index).ref for index in group.example] or members
            rendered = " -> ".join([*example, example[0]]) if example else ", ".join(members)
            subject = example[0] if example else members[0]
            yield self.finding(
                subject,
                f"circular event dependency: {rendered}"
                + (
                    f" (cycle involves {len(members)} events)"
                    if len(members) > len(example)
                    else ""
                ),
                subject_kind="event",
                source=(
                    ctx.catalog.events[subject].source if subject in ctx.catalog.events else None
                ),
                details={"cycle": example, "members": members, "length": len(example)},
            )


class MissingSchemaVersionRule(Rule):
    """A declared version carries no schema."""

    id: ClassVar[str] = "EG007"
    name: ClassVar[str] = "missing-schema-version"
    description: ClassVar[str] = "An event version is declared without a schema."
    default_severity: ClassVar[Severity] = Severity.WARNING
    remediation: ClassVar[str] = (
        "Attach the schema for every declared version so compatibility can be checked "
        "and consumers can validate payloads."
    )
    tags: ClassVar[tuple[str, ...]] = ("schema", "completeness")

    def evaluate(self, ctx: ValidationContext) -> Iterable[Finding]:
        """Yield every finding this rule detects in the catalog."""
        for event in ctx.catalog.sorted_events():
            for entry in event.all_versions():
                if entry.schema is not None and not entry.schema.is_empty():
                    continue
                if entry.status is VersionStatus.RETIRED:
                    continue
                yield self.finding(
                    event.id,
                    f"event {event.qualified_name!r} version {entry.version!r} has no schema",
                    subject_kind="event",
                    source=event.source,
                    details={"version": entry.version},
                )


class UnusedEventRule(Rule):
    """An event has neither producers nor consumers."""

    id: ClassVar[str] = "EG008"
    name: ClassVar[str] = "unused-event-definition"
    description: ClassVar[str] = "An event has neither producers nor consumers."
    default_severity: ClassVar[Severity] = Severity.WARNING
    remediation: ClassVar[str] = "Remove the definition, or document how the event is used."
    tags: ClassVar[tuple[str, ...]] = ("hygiene",)

    def evaluate(self, ctx: ValidationContext) -> Iterable[Finding]:
        """Yield every finding this rule detects in the catalog."""
        for event in ctx.catalog.sorted_events():
            if event.producers or event.consumers:
                continue
            yield self.finding(
                event.id,
                f"event {event.qualified_name!r} is defined but has no producers or consumers",
                subject_kind="event",
                source=event.source,
            )


class ParticipantVersionMismatchRule(Rule):
    """Producers and consumers must agree on an event version."""

    id: ClassVar[str] = "EG009"
    name: ClassVar[str] = "participant-schema-incompatibility"
    description: ClassVar[str] = (
        "A producer/consumer declares an unknown version, or two participants are pinned "
        "to versions that are not compatible."
    )
    default_severity: ClassVar[Severity] = Severity.ERROR
    remediation: ClassVar[str] = (
        "Upgrade the participant to a compatible version, or publish the change as a new "
        "event version and migrate consumers first."
    )
    tags: ClassVar[tuple[str, ...]] = ("schema", "compatibility")

    def evaluate(self, ctx: ValidationContext) -> Iterable[Finding]:
        """Yield every finding this rule detects in the catalog."""
        for event in ctx.catalog.sorted_events():
            known = set(event.schema_versions())
            participating = [*event.producers, *event.consumers]
            for participant in participating:
                if not participant.schema_version:
                    continue
                if participant.schema_version not in known:
                    yield self.finding(
                        event.id,
                        f"{type(participant).__name__.lower()} {participant.service!r} declares "
                        f"unknown version {participant.schema_version!r} of "
                        f"event {event.qualified_name!r} (known: {', '.join(sorted(known))})",
                        subject_kind="event",
                        source=participant.source or event.source,
                        details={
                            "service": participant.service,
                            "declared": participant.schema_version,
                            "known": sorted(known),
                        },
                    )
            pinned = {
                participant.schema_version
                for participant in participating
                if participant.schema_version and participant.schema_version in known
            }
            if len(pinned) < 2:
                continue
            versions = sorted(pinned, key=version_key)
            base = versions[0]
            for other in versions[1:]:
                try:
                    comparison = compare_event_versions(
                        event, base, other, mode=ctx.compatibility_mode
                    )
                except Exception:  # pragma: no cover - incomparable dialects
                    continue
                if comparison.report.compatible:
                    continue
                yield self.finding(
                    event.id,
                    f"producers/consumers of {event.qualified_name!r} are pinned to "
                    f"incompatible versions {base!r} and {other!r}: "
                    f"{comparison.report.summary()}",
                    subject_kind="event",
                    source=event.source,
                    details={
                        "versions": [base, other],
                        "changes": [
                            {
                                "change": change.change.value,
                                "path": change.path,
                                "impact": change.impact.value,
                            }
                            for change in comparison.report.breaking_changes
                        ],
                    },
                )


class IncompatibleVersionHistoryRule(Rule):
    """The version history of an event itself contains a breaking change."""

    id: ClassVar[str] = "EG010"
    name: ClassVar[str] = "incompatible-schema-change"
    description: ClassVar[str] = (
        "Two consecutive versions of an event are incompatible under the configured mode."
    )
    default_severity: ClassVar[Severity] = Severity.ERROR
    remediation: ClassVar[str] = (
        "Ship breaking changes as a new event (or a major version) and migrate consumers "
        "before removing the old one."
    )
    tags: ClassVar[tuple[str, ...]] = ("schema", "compatibility")

    def evaluate(self, ctx: ValidationContext) -> Iterable[Finding]:
        """Yield every finding this rule detects in the catalog."""
        from eventlineage.compatibility.result import CompatibilityMode

        # ``ValidationContext`` is a dataclass, so a caller may hand us the
        # mode as a plain string even though the annotation says otherwise.
        declared_mode: object = ctx.compatibility_mode
        mode = (
            declared_mode
            if isinstance(declared_mode, CompatibilityMode)
            else CompatibilityMode.coerce(declared_mode)  # type: ignore[arg-type]
        )
        for event in ctx.catalog.sorted_events():
            entries = [
                entry
                for entry in event.all_versions()
                if entry.schema is not None and not entry.schema.is_empty()
            ]
            if len(entries) < 2:
                continue
            ordered = sorted(entries, key=lambda entry: version_key(entry.version))
            for previous, current in itertools.pairwise(ordered):
                try:
                    report = compare_schemas(
                        cast(EventSchema, previous.schema),
                        cast(EventSchema, current.schema),
                        mode,
                    )
                except Exception:
                    continue
                if report.compatible:
                    continue
                yield self.finding(
                    event.id,
                    f"version {current.version!r} of {event.qualified_name!r} is not "
                    f"{mode.value}-compatible with {previous.version!r}: {report.summary()}",
                    subject_kind="event",
                    source=event.source,
                    details={
                        "old_version": previous.version,
                        "new_version": current.version,
                        "mode": mode.value,
                        "breaking": [change.render() for change in report.breaking_changes],
                    },
                )


class MissingSchemaRule(Rule):
    """An event has no schema at all."""

    id: ClassVar[str] = "EG011"
    name: ClassVar[str] = "event-without-schema"
    description: ClassVar[str] = "An event has no schema at all."
    default_severity: ClassVar[Severity] = Severity.WARNING
    remediation: ClassVar[str] = (
        "Attach a JSON Schema, Avro or Protobuf definition so payload changes can be "
        "analysed and consumers can validate messages."
    )
    tags: ClassVar[tuple[str, ...]] = ("schema", "completeness")

    def evaluate(self, ctx: ValidationContext) -> Iterable[Finding]:
        """Yield every finding this rule detects in the catalog."""
        for event in ctx.catalog.sorted_events():
            if event.has_schema():
                continue
            yield self.finding(
                event.id,
                f"event {event.qualified_name!r} has no schema definition",
                subject_kind="event",
                source=event.source,
                details={"inferred": event.inferred},
            )


class ServiceWithoutEventsRule(Rule):
    """A declared service neither publishes nor consumes."""

    id: ClassVar[str] = "EG012"
    name: ClassVar[str] = "service-without-events"
    description: ClassVar[str] = "A declared service neither produces nor consumes events."
    default_severity: ClassVar[Severity] = Severity.INFO
    remediation: ClassVar[str] = (
        "Either the service is not event-driven (remove it from the catalog) or its "
        "produces/consumes lists are incomplete."
    )
    tags: ClassVar[tuple[str, ...]] = ("hygiene",)

    def evaluate(self, ctx: ValidationContext) -> Iterable[Finding]:
        """Yield every finding this rule detects in the catalog."""
        index = ctx.catalog.service_event_index()
        for service in ctx.catalog.sorted_services():
            if service.metadata.get("external") or service.metadata.get("inferred"):
                continue
            produced = index.produced_by(service.name)
            consumed = index.consumed_by(service.name)
            if produced or consumed:
                continue
            yield self.finding(
                service.name,
                f"service {service.name!r} is declared but has no event involvement",
                subject_kind="service",
                source=service.source,
            )


class OrphanTopicRule(Rule):
    """A topic has no events, producers or consumers."""

    id: ClassVar[str] = "EG013"
    name: ClassVar[str] = "orphan-topic"
    description: ClassVar[str] = "A topic has no events, producers or consumers."
    default_severity: ClassVar[Severity] = Severity.INFO
    remediation: ClassVar[str] = "Link the topic to its events or delete it from the configuration."
    tags: ClassVar[tuple[str, ...]] = ("hygiene",)

    def evaluate(self, ctx: ValidationContext) -> Iterable[Finding]:
        """Yield every finding this rule detects in the catalog."""
        for topic in ctx.catalog.sorted_topics():
            if topic.events or topic.producers or topic.consumers:
                continue
            yield self.finding(
                topic.name,
                f"topic {topic.name!r} has no events, producers or consumers",
                subject_kind="topic",
                source=topic.source,
            )


class UndeclaredServiceRule(Rule):
    """A service is referenced but never declared."""

    id: ClassVar[str] = "EG014"
    name: ClassVar[str] = "service-undeclared"
    description: ClassVar[str] = (
        "A service appears as a producer or consumer but is never declared anywhere."
    )
    default_severity: ClassVar[Severity] = Severity.INFO
    remediation: ClassVar[str] = (
        "Declare the service (with owner and tier) so impact analysis can report realistic risk."
    )
    tags: ClassVar[tuple[str, ...]] = ("hygiene", "completeness")

    def evaluate(self, ctx: ValidationContext) -> Iterable[Finding]:
        """Yield every finding this rule detects in the catalog."""
        for name, service in ctx.catalog.services.items():
            if service.metadata.get("declared") is False or service.metadata.get("inferred"):
                yield self.finding(
                    name,
                    f"service {name!r} is referenced by events but never declared",
                    subject_kind="service",
                    source=service.source,
                )


class CircularServiceDependencyRule(Rule):
    """Services depend on each other through events."""

    id: ClassVar[str] = "EG015"
    name: ClassVar[str] = "circular-service-dependency"
    description: ClassVar[str] = "Services depend on each other in a cycle through events."
    default_severity: ClassVar[Severity] = Severity.WARNING
    remediation: ClassVar[str] = (
        "Introduce an asynchronous boundary or extract the shared step into a separate "
        "consumer to remove the cycle."
    )
    tags: ClassVar[tuple[str, ...]] = ("graph", "architecture")

    def evaluate(self, ctx: ValidationContext) -> Iterable[Finding]:
        """Yield every finding this rule detects in the catalog."""
        from eventlineage.graph.algorithms import find_cycle_groups

        services = ctx.service_flow()
        for group in find_cycle_groups(services, max_groups=25):
            members = sorted(services.node(index).ref for index in group.nodes)
            example = [services.node(index).ref for index in group.example] or members
            rendered = " -> ".join([*example, example[0]]) if example else ", ".join(members)
            subject = example[0] if example else members[0]
            yield self.finding(
                subject,
                f"circular service dependency: {rendered}"
                + (
                    f" (cycle involves {len(members)} services)"
                    if len(members) > len(example)
                    else ""
                ),
                subject_kind="service",
                source=(
                    ctx.catalog.services[subject].source
                    if subject in ctx.catalog.services
                    else None
                ),
                details={"cycle": example, "members": members},
            )


class InvalidSchemaDocumentRule(Rule):
    """Catch schemas that are not valid for their own dialect."""

    id: ClassVar[str] = "EG016"
    name: ClassVar[str] = "invalid-schema-definition"
    description: ClassVar[str] = "A JSON Schema document fails meta-schema validation."
    default_severity: ClassVar[Severity] = Severity.ERROR
    remediation: ClassVar[str] = (
        "Fix the schema so it validates against its declared meta-schema; invalid "
        "schemas cannot be used for payload validation."
    )
    tags: ClassVar[tuple[str, ...]] = ("schema", "correctness")

    def evaluate(self, ctx: ValidationContext) -> Iterable[Finding]:
        """Yield every finding this rule detects in the catalog."""
        try:
            import jsonschema
        except ImportError:  # pragma: no cover - jsonschema is a hard dependency
            return
        for event in ctx.catalog.sorted_events():
            schema = event.active_schema()
            if schema is None or schema.format != SchemaFormat.JSON_SCHEMA:
                continue
            definition = schema.definition
            if not isinstance(definition, dict):
                continue
            try:
                jsonschema.validators.validator_for(definition).check_schema(definition)
            except jsonschema.exceptions.SchemaError as exc:
                yield self.finding(
                    event.id,
                    f"schema of event {event.qualified_name!r} is invalid: {exc.message}",
                    subject_kind="event",
                    source=schema.source or event.source,
                    details={"pointer": list(exc.absolute_path)},
                )
            except Exception:  # pragma: no cover - defensive
                continue


class SchemaTypeDriftRule(Rule):
    """Different schema dialects for the same logical event confuse consumers."""

    id: ClassVar[str] = "EG017"
    name: ClassVar[str] = "mixed-schema-formats"
    description: ClassVar[str] = "Version history of an event mixes schema dialects."
    default_severity: ClassVar[Severity] = Severity.INFO
    remediation: ClassVar[str] = (
        "Keep one dialect per event; cross-dialect comparisons cannot be validated automatically."
    )
    tags: ClassVar[tuple[str, ...]] = ("schema", "hygiene")

    def evaluate(self, ctx: ValidationContext) -> Iterable[Finding]:
        """Yield every finding this rule detects in the catalog."""
        for event in ctx.catalog.sorted_events():
            formats = {
                entry.schema.format
                for entry in event.all_versions()
                if entry.schema is not None and not entry.schema.is_empty()
            }
            if len(formats) > 1:
                yield self.finding(
                    event.id,
                    f"event {event.qualified_name!r} uses multiple schema formats: "
                    f"{', '.join(sorted(fmt.value for fmt in formats))}",
                    subject_kind="event",
                    source=event.source,
                    details={"formats": sorted(fmt.value for fmt in formats)},
                )


BUILTIN_RULES: tuple[type[Rule], ...] = (
    OrphanEventRule,
    EventWithoutConsumerRule,
    UnknownEventReferenceRule,
    DuplicateEventRule,
    DuplicateTopicRule,
    CircularEventDependencyRule,
    MissingSchemaVersionRule,
    UnusedEventRule,
    ParticipantVersionMismatchRule,
    IncompatibleVersionHistoryRule,
    MissingSchemaRule,
    ServiceWithoutEventsRule,
    OrphanTopicRule,
    UndeclaredServiceRule,
    CircularServiceDependencyRule,
    InvalidSchemaDocumentRule,
    SchemaTypeDriftRule,
)

__all__ = [
    "BUILTIN_RULES",
    "CircularEventDependencyRule",
    "CircularServiceDependencyRule",
    "DuplicateEventRule",
    "DuplicateTopicRule",
    "EventWithoutConsumerRule",
    "IncompatibleVersionHistoryRule",
    "InvalidSchemaDocumentRule",
    "MissingSchemaRule",
    "MissingSchemaVersionRule",
    "OrphanEventRule",
    "OrphanTopicRule",
    "ParticipantVersionMismatchRule",
    "SchemaTypeDriftRule",
    "ServiceWithoutEventsRule",
    "UndeclaredServiceRule",
    "UnknownEventReferenceRule",
    "UnusedEventRule",
]
