"""Version parsing and ordering helpers.

EventLineage does not require strict semver: real projects use ``1``, ``1.0``,
``2024-05-01`` or ``v3``. Ordering is therefore *best effort* but total and
deterministic: numeric segments compare numerically, anything else compares
lexicographically, and pre-release suffixes (``-rc1``) sort before the release.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_NUMERIC = re.compile(r"^\d+$")
_LEADING_V = re.compile(r"^[vV](\d)")
_SEGMENT_SPLIT = re.compile(r"[.\-_+]")


def version_key(value: str) -> tuple[tuple[int, int, str], ...]:
    """Return a total-ordering key for a version-ish string."""
    cleaned = value.strip()
    cleaned = _LEADING_V.sub(r"\1", cleaned)
    if not cleaned:
        return ()
    release, _, prerelease = cleaned.partition("-")
    key: list[tuple[int, int, str]] = []
    for segment in _SEGMENT_SPLIT.split(release):
        if not segment:
            continue
        if _NUMERIC.match(segment):
            key.append((0, int(segment), ""))
        else:
            key.append((1, 0, segment))
    # ``2`` and ``2.0.0`` are one version. The end marker ranks below every segment,
    # so ``2`` sorts before ``2.0.1``, and a pre-release before its final release.
    while key and key[-1] == (0, 0, ""):
        key.pop()
    key.append((-1, 0, prerelease) if prerelease else (-1, 1, ""))
    return tuple(key)


def sort_versions(values: Iterable[str], *, reverse: bool = True) -> list[str]:
    """Sort version strings (newest first by default)."""
    return sorted(values, key=version_key, reverse=reverse)


def latest_version(values: Iterable[str]) -> str | None:
    """Highest version in ``versions`` by semantic order."""
    ordered = sort_versions(values)
    return ordered[0] if ordered else None


def is_prerelease(value: str) -> bool:
    """Whether a version string marks a pre-release."""
    return "-" in value.strip()
