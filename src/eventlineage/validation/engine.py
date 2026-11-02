"""The validation engine: runs rules, applies policy, sorts findings."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Sequence
from functools import cache
from importlib import metadata
from typing import Any

from eventlineage.compatibility.result import CompatibilityMode
from eventlineage.config import Config
from eventlineage.core.catalog import EventCatalog
from eventlineage.exceptions import SymbolNotFoundError
from eventlineage.models.base import EGBaseModel
from eventlineage.models.common import Severity
from eventlineage.validation.rule import (
    Finding,
    Rule,
    SuppressedFinding,
    ValidationContext,
    describe,
)
from eventlineage.validation.rules import BUILTIN_RULES

logger = logging.getLogger(__name__)

#: Installed packages advertise custom rules here, which is how they reach the CLI.
ENTRY_POINT_GROUP = "eventlineage.rules"

_RULES: dict[str, type[Rule]] = {}


def register_rule(rule: type[Rule], *, replace: bool = False) -> type[Rule]:
    """Register a validation rule (usable as a class decorator)."""
    for attribute in ("id", "name"):
        if not getattr(rule, attribute, ""):
            raise ValueError(f"{rule.__name__} must define a non-empty {attribute!r}")
    key = rule.id.lower()
    if key in _RULES and not replace:
        if _RULES[key] is rule:
            return rule
        raise ValueError(f"a validation rule with id {rule.id!r} is already registered")
    _RULES[key] = rule
    return rule


@cache
def _load_entry_points() -> None:
    """Register rules from installed packages; a broken package never stops validation."""
    for entry_point in metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            loaded: Any = entry_point.load()
        except Exception:
            logger.warning("failed to load validation rule %r", entry_point.name, exc_info=True)
            continue
        for candidate in loaded if isinstance(loaded, (list, tuple)) else [loaded]:
            if not (isinstance(candidate, type) and issubclass(candidate, Rule)):
                logger.warning("entry point %r is not a Rule subclass", entry_point.name)
                continue
            try:
                register_rule(candidate)
            except ValueError as exc:
                logger.warning("skipping validation rule %r: %s", entry_point.name, exc)


def _bootstrap() -> None:
    if not _RULES:
        for rule in BUILTIN_RULES:
            register_rule(rule)
    _load_entry_points()


def available_rules() -> tuple[type[Rule], ...]:
    """All registered rules, ordered by id."""
    _bootstrap()
    return tuple(_RULES[key] for key in sorted(_RULES))


def get_rule(reference: str) -> type[Rule]:
    """Look up a rule by id (``EG001``) or name (``event-without-producer``)."""
    _bootstrap()
    lowered = reference.strip().lower()
    if lowered in _RULES:
        return _RULES[lowered]
    for rule in _RULES.values():
        if rule.name.lower() == lowered:
            return rule
    known = tuple(rule.id for rule in sorted(_RULES.values(), key=lambda item: item.id))
    raise SymbolNotFoundError(reference, "rule", candidates=known)


def rule_catalog() -> tuple[dict[str, str], ...]:
    """Every rule that ran, with its metadata."""
    return tuple(describe(rule) for rule in available_rules())


class ValidationReport(EGBaseModel):
    """The result of running the validation engine."""

    findings: tuple[Finding, ...] = ()
    suppressed: tuple[SuppressedFinding, ...] = ()
    rules_run: tuple[str, ...] = ()
    skipped_rules: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    fail_on: Severity = Severity.ERROR
    mode: CompatibilityMode = CompatibilityMode.BACKWARD
    duration_ms: float = 0.0
    catalog_fingerprint: str | None = None

    def counts(self) -> dict[str, int]:
        """Return counts grouped by severity."""
        counts = {severity.value: 0 for severity in Severity if severity is not Severity.OFF}
        for finding in self.findings:
            counts[finding.severity.value] = counts.get(finding.severity.value, 0) + 1
        return counts

    def by_rule(self) -> dict[str, int]:
        """Findings grouped by rule id."""
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.rule] = counts.get(finding.rule, 0) + 1
        return dict(sorted(counts.items()))

    def by_severity(self, severity: Severity) -> tuple[Finding, ...]:
        """Findings grouped by severity."""
        return tuple(finding for finding in self.findings if finding.severity is severity)

    @property
    def worst_severity(self) -> Severity | None:
        """Highest severity among the findings."""
        ranked = [
            finding.severity for finding in self.findings if finding.severity is not Severity.OFF
        ]
        if not ranked:
            return None
        return max(ranked, key=lambda severity: severity.rank)

    def threshold(self) -> Severity:
        """The severity threshold applied to this report."""
        return self.fail_on

    @property
    def passed(self) -> bool:
        """Whether the run satisfies the configured failure threshold."""
        if self.fail_on is Severity.OFF:
            return True
        return all(finding.severity.rank < self.fail_on.rank for finding in self.findings)

    @property
    def failed(self) -> bool:
        """Whether the report breached its threshold."""
        return not self.passed

    def exit_code(self, fail_on: Severity | None = None) -> int:
        """Process exit code implied by this report."""
        threshold = self.fail_on if fail_on is None else fail_on
        if threshold is Severity.OFF:
            return 0
        for finding in self.findings:
            if finding.severity.rank >= threshold.rank:
                return 1
        return 0

    def summary(self) -> str:
        """One-line summary of counts and the applied threshold."""
        counts = self.counts()
        parts = [
            f"{counts.get(severity.value, 0)} {severity.value}"
            for severity in (Severity.ERROR, Severity.WARNING, Severity.INFO)
            if counts.get(severity.value, 0)
        ]
        detail = ", ".join(parts) if parts else "no findings"
        suppressed = f" ({len(self.suppressed)} suppressed)" if self.suppressed else ""
        verdict = "FAILED" if self.failed else "PASSED"
        return f"{verdict}: {detail}{suppressed} across {len(self.rules_run)} rules"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready representation of the report."""
        return {
            "passed": self.passed,
            "fail_on": self.fail_on.value,
            "mode": self.mode.value,
            "counts": self.counts(),
            "by_rule": self.by_rule(),
            "rules_run": list(self.rules_run),
            "skipped_rules": list(self.skipped_rules),
            "errors": list(self.errors),
            "duration_ms": round(self.duration_ms, 2),
            "catalog_fingerprint": self.catalog_fingerprint,
            "findings": [finding.to_dict() for finding in self.findings],
            "suppressed": [
                {
                    "rule": item.rule,
                    "subject": item.subject,
                    "message": item.message,
                    "severity": item.severity.value,
                    "reason": item.reason,
                }
                for item in self.suppressed
            ],
        }


class Validator:
    """Runs validation rules against a catalog."""

    def __init__(
        self,
        config: Config | None = None,
        rules: Sequence[type[Rule]] | None = None,
    ) -> None:
        self._config = config or Config()
        self._rules = tuple(rules) if rules is not None else available_rules()

    @property
    def rules(self) -> tuple[type[Rule], ...]:
        """The rule classes that were evaluated."""
        return self._rules

    def validate(
        self,
        catalog: EventCatalog,
        *,
        config: Config | None = None,
        mode: CompatibilityMode | None = None,
    ) -> ValidationReport:
        """Evaluate every enabled rule and return a report."""
        effective = config or self._config
        compatibility_mode = mode or effective.compatibility.mode
        started = time.perf_counter()
        ctx = ValidationContext(
            catalog=catalog,
            config=effective,
            compatibility_mode=CompatibilityMode.coerce(compatibility_mode),
        )
        findings: list[Finding] = []
        suppressed: list[SuppressedFinding] = []
        rules_run: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []
        seen: set[tuple[str, str, str]] = set()

        for rule_class in self._rules:
            rule = rule_class()
            if not ctx.enabled(rule):
                skipped.append(rule.id)
                continue
            severity = ctx.severity_for(rule)
            if severity is Severity.OFF:
                skipped.append(rule.id)
                continue
            rules_run.append(rule.id)
            try:
                produced = list(rule.evaluate(ctx))
            except Exception as exc:  # a broken rule must not abort the run
                logger.warning("validation rule %s failed: %s", rule.id, exc, exc_info=True)
                errors.append(f"{rule.id} ({rule.name}) failed: {type(exc).__name__}: {exc}")
                continue
            for produced_finding in produced:
                finding = produced_finding
                if finding.severity is not severity:
                    finding = finding.model_copy(update={"severity": severity})
                identity = finding.identity()
                if identity in seen:
                    continue
                seen.add(identity)
                suppression = ctx.suppression_for(finding)
                if suppression is not None:
                    suppressed.append(
                        SuppressedFinding(
                            rule=finding.rule,
                            subject=finding.subject,
                            message=finding.message,
                            severity=finding.severity,
                            reason=suppression.reason,
                        )
                    )
                    continue
                findings.append(finding)

        findings.sort(
            key=lambda finding: (
                -finding.severity.rank,
                finding.rule,
                finding.subject or "",
                finding.message,
            )
        )
        return ValidationReport(
            findings=tuple(findings),
            suppressed=tuple(sorted(suppressed, key=lambda item: (item.rule, item.subject or ""))),
            rules_run=tuple(rules_run),
            skipped_rules=tuple(sorted(skipped)),
            errors=tuple(errors),
            fail_on=effective.validation.fail_on,
            mode=CompatibilityMode.coerce(compatibility_mode),
            duration_ms=(time.perf_counter() - started) * 1000.0,
            catalog_fingerprint=catalog.fingerprint(),
        )


def validate_catalog(
    catalog: EventCatalog,
    config: Config | None = None,
    *,
    rules: Iterable[type[Rule]] | None = None,
    mode: CompatibilityMode | None = None,
) -> ValidationReport:
    """Convenience wrapper used by the SDK."""
    return Validator(config, list(rules) if rules is not None else None).validate(
        catalog, config=config, mode=mode
    )


__all__ = [
    "ENTRY_POINT_GROUP",
    "ValidationReport",
    "Validator",
    "available_rules",
    "get_rule",
    "register_rule",
    "rule_catalog",
    "validate_catalog",
]
