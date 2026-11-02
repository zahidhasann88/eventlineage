"""Incremental scan cache.

Parsing 10 000 event files is fast, but in CI it happens on every commit. The
cache stores the parsed fragments keyed by (relative path, content digest,
config fingerprint), so an unchanged file is a dictionary lookup instead of a
parse. Any change to a file or to the discovery configuration invalidates the
affected entries.

The cache is deliberately a single JSON file: it is inspectable, portable
between machines, and trivially safe to delete (``eventlineage cache clear``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CACHE_VERSION = 3


@dataclass(slots=True)
class CacheEntry:
    """One cached discovery result."""

    path: str
    digest: str
    plugin: str
    payload: dict[str, Any]
    #: Digest of every other file this result was built from, by relative path.
    dependencies: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready representation."""
        return {
            "digest": self.digest,
            "plugin": self.plugin,
            "payload": self.payload,
            "dependencies": self.dependencies,
        }


@dataclass(slots=True)
class ScanCache:
    """Content-addressed cache for discovery results."""

    path: Path
    fingerprint: str = ""
    entries: dict[str, CacheEntry] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0
    writes: int = 0
    _dirty: bool = False

    @classmethod
    def load(cls, path: Path, fingerprint: str = "", max_bytes: int | None = None) -> ScanCache:
        """Load a cache file, returning an empty cache when it is unusable."""
        cache = cls(path=path, fingerprint=fingerprint)
        if not path.is_file():
            return cache
        try:
            if max_bytes is not None and path.stat().st_size > max_bytes:
                logger.debug("ignoring scan cache larger than %d bytes", max_bytes)
                return cache
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            logger.debug("ignoring unreadable scan cache %s: %s", path, exc)
            return cache
        if not isinstance(raw, dict):
            logger.debug("ignoring malformed scan cache %s", path)
            return cache
        if raw.get("version") != CACHE_VERSION:
            logger.debug("ignoring scan cache with stale version")
            return cache
        if raw.get("fingerprint") != fingerprint:
            logger.debug("ignoring scan cache: configuration changed")
            return cache
        for relative, entry in (raw.get("entries") or {}).items():
            if not isinstance(entry, dict):
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            cache.entries[relative] = CacheEntry(
                path=relative,
                digest=str(entry.get("digest", "")),
                plugin=str(entry.get("plugin", "")),
                payload=payload,
                dependencies=dict(entry.get("dependencies") or {}),
            )
        return cache

    def get(self, relative: str, digest: str) -> CacheEntry | None:
        """Look up an entry by name."""
        entry = self.entries.get(relative)
        if entry is None or entry.digest != digest:
            self.misses += 1
            return None
        self.hits += 1
        return entry

    def put(
        self,
        relative: str,
        digest: str,
        plugin: str,
        payload: dict[str, Any],
        dependencies: dict[str, str] | None = None,
    ) -> None:
        """Store a payload under ``relative``."""
        self.entries[relative] = CacheEntry(
            path=relative,
            digest=digest,
            plugin=plugin,
            payload=payload,
            dependencies=dependencies or {},
        )
        self.writes += 1
        self._dirty = True

    def prune(self, keep: set[str] | None = None) -> int:
        """Drop entries not in ``keep`` (files that disappeared from the project)."""
        if keep is None:
            return 0
        stale = [key for key in self.entries if key not in keep]
        for key in stale:
            del self.entries[key]
        if stale:
            self._dirty = True
        return len(stale)

    def save(self) -> None:
        """Persist atomically; failures are logged, never fatal."""
        if not self._dirty:
            return
        document = {
            "version": CACHE_VERSION,
            "fingerprint": self.fingerprint,
            "entries": {key: entry.to_dict() for key, entry in sorted(self.entries.items())},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Key order is already deterministic (sorted entries, fixed field
            # order), so ``sort_keys`` would only cost encoding time. Encoding
            # in one shot keeps CPython on the C encoder: ``json.dump`` falls
            # back to the pure-Python generator, which is ~5x slower on the
            # multi-megabyte caches that 10k-event projects produce.
            encoded = json.dumps(document, separators=(",", ":"))
            handle, temporary = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(encoded)
            Path(temporary).replace(self.path)
        except OSError as exc:  # pragma: no cover - disk failure
            logger.warning("could not write scan cache %s: %s", self.path, exc)
            return
        self._dirty = False

    def stats(self) -> dict[str, int]:
        """Return statistics about this store."""
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        return {
            "entries": len(self.entries),
            "hits": self.hits,
            "misses": self.misses,
            "size_bytes": size,
        }


def digest_bytes(payload: bytes) -> str:
    """SHA-256 of raw bytes, as hex."""
    return hashlib.sha256(payload).hexdigest()


def digest_text(text: str) -> str:
    """SHA-256 of UTF-8 text, as hex."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "CACHE_VERSION",
    "CacheEntry",
    "ScanCache",
    "digest_bytes",
    "digest_text",
]
