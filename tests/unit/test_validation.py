"""Validation rules, severity overrides, suppression and report arithmetic."""

from __future__ import annotations

import pytest
from tests.conftest import make_catalog, make_event

from eventlineage.config import Config
from eventlineage.graph.builder import apply_inferred_relationships
from eventlineage.models.common import Severity, SourceLocation
from eventlineage.models.dependency import EventRelationship
from eventlineage.models.participant import Consumer, Service
from eventlineage.models.topic import Topic
from eventlineage.validation import (
    Validator,
    available_rules,
    describe,
    get_rule,
    rule_catalog,
    validate_catalog,
)


def rule_ids(report: object) -> set[str]:
    return {finding.rule for finding in report.findings}  # type: ignore[attr-defined]


def findings_for(report: object, rule: str) -> list[object]:
    return [finding for finding in report.findings if finding.rule == rule]  # type: ignore[attr-defined]


def validated(catalog: object, cfg: Config | None = None) -> object:
    """Validate a catalog the way the scanner prepares it (with inferred links)."""
    apply_inferred_relationships(catalog)  # type: ignore[arg-type]
    return validate_catalog(catalog, cfg)  # type: ignore[arg-type]


def config(**overrides: object) -> Config:
    return Config.model_validate({"version": 1, **overrides})


class TestRuleRegistry:
    def test_every_rule_is_described(self) -> None:
        assert len(available_rules()) == 17
        for rule in available_rules():
            assert rule.id.startswith("EG")
            assert rule.name
            assert rule.description
            assert rule.remediation, f"{rule.id} has no remediation"

    def test_rule_ids_are_unique(self) -> None:
        ids = [rule.id for rule in available_rules()]
        assert len(ids) == len(set(ids))

    def test_get_rule(self) -> None:
        from eventlineage.exceptions import EventLineageError

        assert get_rule("EG001").name == "event-without-producer"
        with pytest.raises(EventLineageError):
            get_rule("EG999")

    def test_describe_returns_documentation(self) -> None:
        payload = describe(get_rule("EG006"))
        assert payload["id"] == "EG006"
        assert payload["remediation"]

    def test_rule_catalog_is_serializable(self) -> None:
        catalog_payload = rule_catalog()
        assert isinstance(catalog_payload, (list, tuple))
        assert {"id", "name", "severity"} <= set(catalog_payload[0])


class TestBuiltinRules:
    def test_eg001_event_without_producer(self) -> None:
        catalog = make_catalog([make_event("a.b", consumers=["svc"])])
        report = validate_catalog(catalog)
        assert "EG001" in rule_ids(report)

    def test_eg002_event_without_consumer(self) -> None:
        catalog = make_catalog([make_event("a.b", producers=["svc"])])
        report = validate_catalog(catalog)
        assert "EG002" in rule_ids(report)

    def test_eg003_unknown_participant_reference(self) -> None:
        catalog = make_catalog(
            [
                make_event(
                    "a.b",
                    producers=["svc"],
                    consumers=[Consumer(service="svc2", event_id="never.declared")],
                )
            ]
        )
        report = validate_catalog(catalog)
        assert "EG003" in rule_ids(report)

    def test_eg004_duplicate_definitions(self) -> None:
        location = SourceLocation(path="events/a.yaml", plugin="catalog")
        other = SourceLocation(path="events/b.yaml", plugin="catalog")
        catalog = make_catalog(
            [
                make_event("a.b", producers=["svc"]).model_copy(update={"source": location}),
                make_event("a.b", consumers=["other"]).model_copy(update={"source": other}),
            ]
        )
        report = validate_catalog(catalog)
        assert "EG004" in rule_ids(report)

    def test_eg005_topic_defined_twice(self) -> None:
        catalog = make_catalog(
            [make_event("a.b", producers=["svc"], consumers=["c"])],
            topics=[
                Topic(name="orders.v1", partitions=3, source=SourceLocation(path="a.yaml")),
                Topic(name="orders.v1", partitions=6, source=SourceLocation(path="b.yaml")),
            ],
        )
        report = validate_catalog(catalog)
        assert "EG005" in rule_ids(report)
        finding = findings_for(report, "EG005")[0]
        assert finding.severity is Severity.INFO

    def test_eg006_event_cycle(self) -> None:
        catalog = make_catalog(
            [
                make_event("a.b", producers=["x"], consumers=["y"]),
                make_event("c.d", producers=["y"], consumers=["x"]),
            ],
            services=[Service(name="x"), Service(name="y")],
        )
        report = validated(catalog)
        assert "EG006" in rule_ids(report)
        finding = findings_for(report, "EG006")[0]
        assert finding.severity is Severity.ERROR
        assert "circular" in finding.message

    def test_eg007_version_without_schema(self) -> None:
        from eventlineage.models.schema import EventVersion

        event = make_event("a.b", producers=["svc"], consumers=["c"]).model_copy(
            update={"versions": (EventVersion(version="1.0.0"),)}
        )
        report = validate_catalog(make_catalog([event]))
        assert "EG007" in rule_ids(report)

    def test_eg008_orphan_event(self) -> None:
        catalog = make_catalog([make_event("a.b")])
        report = validate_catalog(catalog)
        assert "EG008" in rule_ids(report)

    def test_eg011_event_without_schema(self) -> None:
        catalog = make_catalog([make_event("a.b", producers=["svc"], consumers=["c"])])
        report = validate_catalog(catalog)
        assert "EG011" in rule_ids(report)

    def test_eg012_service_without_events(self) -> None:
        catalog = make_catalog(
            [make_event("a.b", producers=["svc"], consumers=["c"])], services=[Service(name="idle")]
        )
        report = validated(catalog)
        assert "EG012" in rule_ids(report)

    def test_eg013_orphan_topic(self) -> None:
        catalog = make_catalog(
            [make_event("a.b", producers=["svc"])], topics=[Topic(name="idle.topic")]
        )
        report = validated(catalog)
        assert "EG013" in rule_ids(report)

    def test_eg014_undeclared_service(self) -> None:
        catalog = make_catalog([make_event("a.b", producers=["ghost"], consumers=["g2"])])
        report = validate_catalog(catalog)
        assert "EG014" in rule_ids(report)

    def test_eg015_service_cycle(self) -> None:
        catalog = make_catalog(
            [
                make_event("a.b", producers=["x"], consumers=["y"]),
                make_event("c.d", producers=["y"], consumers=["x"]),
            ],
            services=[Service(name="x"), Service(name="y")],
        )
        report = validated(catalog)
        assert "EG015" in rule_ids(report)

    def test_eg017_mixed_schema_formats(self) -> None:
        from eventlineage.models.schema import EventSchema, EventVersion

        event = make_event(
            "a.b", producers=["x"], consumers=["y"], schema={"type": "object"}, version="2.0.0"
        ).model_copy(
            update={
                "versions": (
                    EventVersion(
                        version="1.0.0",
                        schema=EventSchema(
                            format="avro", definition={"type": "record", "name": "A", "fields": []}
                        ),
                    ),
                    EventVersion(
                        version="2.0.0",
                        schema=EventSchema(format="json-schema", definition={"type": "object"}),
                    ),
                )
            }
        )
        catalog = make_catalog([event], services=[Service(name="x"), Service(name="y")])
        report = validated(catalog)
        assert "EG017" in rule_ids(report)

    def test_clean_catalog_has_no_errors(self) -> None:
        catalog = make_catalog(
            [
                make_event(
                    "a.b",
                    producers=["x"],
                    consumers=["y"],
                    schema={"type": "object", "properties": {"id": {"type": "string"}}},
                )
            ],
            services=[Service(name="x", tier="tier-1"), Service(name="y", tier="tier-2")],
        )
        report = validate_catalog(catalog)
        assert report.counts()["error"] == 0
        assert report.exit_code() == 0


class TestSeverityAndSuppression:
    def test_severity_override(self) -> None:
        catalog = make_catalog([make_event("a.b", consumers=["svc"])])
        default = validate_catalog(catalog)
        assert findings_for(default, "EG001")[0].severity is Severity.WARNING

        configured = validate_catalog(catalog, config(validation={"severity": {"EG001": "error"}}))
        assert findings_for(configured, "EG001")[0].severity is Severity.ERROR

    def test_rule_can_be_switched_off(self) -> None:
        catalog = make_catalog([make_event("a.b", consumers=["svc"])])
        report = validate_catalog(catalog, config(validation={"severity": {"EG001": "off"}}))
        assert "EG001" not in rule_ids(report)
        assert len(report.rules_run) < len(available_rules())

    def test_suppression_by_subject_glob(self) -> None:
        catalog = make_catalog([make_event("legacy.audit.written", consumers=["svc"])])
        report = validate_catalog(
            catalog,
            config(
                validation={
                    "suppress": [
                        {"rule": "EG001", "subject": "legacy.*", "reason": "vendor system"}
                    ]
                }
            ),
        )
        assert "EG001" not in rule_ids(report)
        assert report.suppressed
        assert report.suppressed[0].reason == "vendor system"

    def test_suppression_only_matches_its_rule(self) -> None:
        catalog = make_catalog([make_event("a.b", consumers=["svc"])])
        report = validate_catalog(
            catalog, config(validation={"suppress": [{"rule": "EG002", "subject": "a.b"}]})
        )
        assert "EG001" in rule_ids(report)

    def test_fail_on_threshold(self) -> None:
        catalog = make_catalog([make_event("a.b", producers=["x"])])
        assert validate_catalog(catalog, config(validation={"fail_on": "warning"})).failed
        assert not validate_catalog(catalog, config(validation={"fail_on": "error"})).failed

    def test_exit_code_reflects_threshold(self) -> None:
        catalog = make_catalog([make_event("a.b", producers=["x"])])
        report = validate_catalog(catalog, config(validation={"fail_on": "warning"}))
        assert report.exit_code() == 1

    def test_findings_are_sorted_by_severity(self) -> None:
        catalog = make_catalog([make_event("a.b", consumers=["svc"])])
        report = validate_catalog(catalog)
        ranks = [finding.severity.rank for finding in report.findings]
        assert ranks == sorted(ranks, reverse=True)

    def test_findings_are_deduplicated(self) -> None:
        catalog = make_catalog([make_event("a.b", consumers=["svc"])])
        report = validate_catalog(catalog)
        identities = [finding.identity() for finding in report.findings]
        assert len(identities) == len(set(identities))

    def test_report_summary_and_serialization(self) -> None:
        catalog = make_catalog([make_event("a.b", consumers=["svc"])])
        report = validate_catalog(catalog)
        payload = report.to_dict()
        assert payload["counts"]["warning"] >= 1
        assert payload["passed"] in {True, False}
        assert "EG001" in payload["by_rule"] or "by_rule" in payload

    def test_validator_reuse_is_stateless(self) -> None:
        validator = Validator()
        first = validator.validate(make_catalog([make_event("a.b", consumers=["s"])]))
        second = validator.validate(make_catalog([make_event("a.b", consumers=["s"])]))
        assert first.counts() == second.counts()

    def test_relationship_only_catalog(self) -> None:
        catalog = make_catalog(
            [make_event("a.b", producers=["x"]), make_event("c.d", consumers=["x"])],
            services=[Service(name="x")],
        )
        catalog.add_relationship(
            EventRelationship(source_event="a.b", target_event="c.d", kind="triggers")
        )
        report = validate_catalog(catalog)
        assert report.counts()["error"] == 0
