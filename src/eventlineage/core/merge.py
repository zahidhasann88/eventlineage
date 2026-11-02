"""Deterministic merging of partially discovered model fragments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypeVar, overload

from eventlineage.models.common import SourceLocation
from eventlineage.models.event import Event
from eventlineage.models.participant import Consumer, Producer
from eventlineage.models.schema import EventSchema, EventVersion
from eventlineage.models.topic import Topic
from eventlineage.utils.versioning import version_key

ModelT = TypeVar("ModelT", Event, Topic)

_SEQUENCE_FIELDS: dict[str, frozenset[str]] = {
    "Event": frozenset({"producers", "consumers", "topics", "versions", "owners", "tags"}),
    "Topic": frozenset({"producers", "consumers", "events"}),
}
_MAPPING_FIELDS = frozenset({"metadata", "config"})


def _model_defaults(model: type[Any]) -> dict[str, Any]:
    return {name: field.default for name, field in model.model_fields.items()}


def _looks_default(value: Any, default: Any) -> bool:
    """Whether ``value`` carries no information (equal to the field default)."""
    if value == default:
        return True
    if default is None and value in (None, "", (), [], {}):
        return True
    return isinstance(default, (list, tuple, dict)) and not default and not value


def _as_dicts(items: Any) -> list[dict[str, Any]]:
    """Normalize a sequence of models/dicts into plain dicts."""
    normalized: list[dict[str, Any]] = []
    for item in items or []:
        normalized.append(item.model_dump() if hasattr(item, "model_dump") else dict(item))
    return normalized


def _merge_participants(
    left: Any, right: Any, cls: type[Producer] | type[Consumer]
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, ...], dict[str, Any]] = {}
    for item in [*_as_dicts(left), *_as_dicts(right)]:
        participant = cls.model_validate(item)
        identity = participant.identity()
        existing = merged.get(identity)
        if existing is None:
            merged[identity] = item
        else:
            # Keep the richer record: fill blanks, merge topics/metadata.
            combined = cls.model_validate(existing).model_dump()
            for key, value in participant.model_dump(exclude_unset=True).items():
                if key in _MAPPING_FIELDS and isinstance(value, dict):
                    combined[key] = {**value, **combined.get(key, {})}
                elif key == "topics":
                    combined["topics"] = sorted({*combined.get("topics", []), *value})
                elif combined.get(key) in (None, "", [], {}):
                    combined[key] = value
            merged[identity] = combined
    return sorted(merged.values(), key=lambda item: (item.get("service", ""), str(item)))


def _merge_versions(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_version: dict[str, dict[str, Any]] = {}
    for item in [*left, *right]:
        entry = EventVersion.model_validate(item)
        existing = by_version.get(entry.version)
        if existing is None:
            by_version[entry.version] = item
            continue
        current = EventVersion.model_validate(existing)
        schema = current.schema
        if schema is None and entry.schema is not None:
            schema = entry.schema
        by_version[entry.version] = current.model_copy(
            update={
                "schema": schema,
                "status": current.status if current.status.value != "active" else entry.status,
            }
        ).model_dump()
    return [by_version[key] for key in sorted(by_version)]


def _merge_schema(
    left: EventSchema | dict[str, Any] | None, right: EventSchema | dict[str, Any] | None
) -> Any:
    """Prefer the richer schema; two conflicting schemas keep the first."""
    if left is None and right is None:
        return None
    if left is None:
        return right
    if right is None:
        return left
    left_model = left if isinstance(left, EventSchema) else EventSchema.model_validate(left)
    right_model = right if isinstance(right, EventSchema) else EventSchema.model_validate(right)
    if left_model.is_empty():
        return right_model
    if right_model.is_empty():
        return left_model
    return left_model


@dataclass(frozen=True, slots=True)
class SchemaConflict:
    """A fragment whose schema contradicted the definition already stored."""

    event_id: str
    kept: SourceLocation | None = None
    rejected: SourceLocation | None = None


def _schemas_agree(base: Event, base_hash: str, incoming_hash: str) -> bool:
    """True when the incoming schema is one the base event already declares."""
    if base_hash == incoming_hash:
        return True
    return any(
        entry.schema is not None and entry.schema.content_hash == incoming_hash
        for entry in base.version_entries()
    )


def _is_new_version(base: Event, incoming: Event) -> bool:
    """Whether ``incoming`` adds a version instead of contradicting a known one.

    Registry versions are per-subject counters, so ``2`` in a registry says nothing
    about ``2.0.0`` in a catalog; only self-describing sources extend the history.
    """
    if "registry" in base.metadata or "registry" in incoming.metadata:
        return False
    known = {version_key(entry.version) for entry in base.version_entries()}
    return version_key(incoming.version) not in known


def _with_added_versions(data: dict[str, Any], base: Event, incoming: Event) -> None:
    """Union the version histories of both fragments; the newest becomes active."""
    entries = {version_key(entry.version): entry for entry in incoming.version_entries()}
    entries.update({version_key(entry.version): entry for entry in base.version_entries()})
    ordered = [entries[key] for key in sorted(entries)]
    data["versions"] = [entry.model_dump() for entry in ordered]
    data["version"] = ordered[-1].version
    data["schema"] = ordered[-1].schema.model_dump() if ordered[-1].schema else None


@overload
def merge_events(base: Event, incoming: Event) -> Event: ...


@overload
def merge_events(
    base: Event, incoming: Event, *, report_conflict: Literal[True]
) -> tuple[Event, bool]: ...


def merge_events(
    base: Event,
    incoming: Event,
    *,
    report_conflict: bool = False,
) -> Event | tuple[Event, bool]:
    """Merge ``incoming`` into ``base`` using first-wins/explicit-wins rules.

    With ``report_conflict=True`` the return value is ``(event, conflicted)`` so
    callers can warn about sources that disagree about a contract.
    """
    if base.id != incoming.id:
        raise ValueError(f"cannot merge events with different ids: {base.id!r} != {incoming.id!r}")
    data = base.model_dump()
    defaults = _model_defaults(Event)
    sequence_fields = _SEQUENCE_FIELDS["Event"]
    incoming_fields = set(incoming.model_fields_set)
    conflicted = False

    # Two different schemas for the same event mean the sources disagree about
    # the contract. The first definition wins wholesale: silently mixing
    # versions from conflicting sources would corrupt compatibility analysis.
    base_schema, incoming_schema = base.active_schema(), incoming.active_schema()
    if (
        base_schema is not None
        and incoming_schema is not None
        and not base_schema.is_empty()
        and not incoming_schema.is_empty()
        and not _schemas_agree(base, base_schema.content_hash, incoming_schema.content_hash)
    ):
        if _is_new_version(base, incoming):
            _with_added_versions(data, base, incoming)
        else:
            conflicted = True
        incoming_fields -= {"schema", "versions", "version"}

    for key in incoming_fields:
        if key == "id":
            continue
        value = getattr(incoming, key)
        current = data.get(key)
        if key == "producers":
            data[key] = _merge_participants(current or [], list(value or []), Producer)
        elif key == "consumers":
            data[key] = _merge_participants(current or [], list(value or []), Consumer)
        elif key == "versions":
            data[key] = _merge_versions(
                [v if isinstance(v, dict) else v.model_dump() for v in (current or [])],
                [v if isinstance(v, dict) else v.model_dump() for v in (value or [])],
            )
        elif key in sequence_fields:
            existing = list(current or [])
            extra = [item for item in (value or []) if item not in existing]
            if key == "topics":
                data[key] = sorted({*existing, *extra})
            else:
                data[key] = [*existing, *extra]
        elif key in _MAPPING_FIELDS:
            data[key] = {**value, **current} if current else dict(value or {})
        elif key == "schema":
            data[key] = _merge_schema(current, value)
        elif key == "inferred":
            continue  # settled below: an unset flag on a fragment still means "declared"
        elif key == "version":
            if version_key(value) > version_key(str(current)):
                data[key] = value
        elif key == "source":
            data[key] = current or value
        elif key == "status":
            data[key] = current if current not in (None, "active") else value
        elif _looks_default(current, defaults.get(key)) and not _looks_default(
            value, defaults.get(key)
        ):
            data[key] = value
    data["inferred"] = base.inferred and incoming.inferred
    merged = Event.model_validate(data)
    if report_conflict:
        return merged, conflicted
    return merged


def merge_topics(base: Topic, incoming: Topic) -> Topic:
    """Merge two declarations of the same topic."""
    if base.name != incoming.name:
        raise ValueError(
            f"cannot merge topics with different names: {base.name!r} != {incoming.name!r}"
        )
    data = base.model_dump()
    defaults = _model_defaults(Topic)
    for key in incoming.model_fields_set:
        if key == "name":
            continue
        value = getattr(incoming, key)
        current = data.get(key)
        if key in _SEQUENCE_FIELDS["Topic"]:
            data[key] = sorted({*(current or []), *(value or [])})
        elif key in _MAPPING_FIELDS:
            data[key] = {**(current or {}), **dict(value or {})}
        elif key == "source":
            data[key] = current or value
        elif _looks_default(current, defaults.get(key)) and not _looks_default(
            value, defaults.get(key)
        ):
            data[key] = value
    return Topic.model_validate(data)
