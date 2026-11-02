"""Validation engine and built-in rules."""

from eventlineage.validation.engine import (
    ValidationReport,
    Validator,
    available_rules,
    get_rule,
    register_rule,
    rule_catalog,
    validate_catalog,
)
from eventlineage.validation.rule import (
    Finding,
    Rule,
    SuppressedFinding,
    ValidationContext,
    describe,
)
from eventlineage.validation.rules import BUILTIN_RULES

__all__ = [
    "BUILTIN_RULES",
    "Finding",
    "Rule",
    "SuppressedFinding",
    "ValidationContext",
    "ValidationReport",
    "Validator",
    "available_rules",
    "describe",
    "get_rule",
    "register_rule",
    "rule_catalog",
    "validate_catalog",
]
