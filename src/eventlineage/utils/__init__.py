"""Shared helpers: caching, YAML loading, versioning, logging and filesystem.

These modules are dependency-light on purpose — the YAML loader, for instance,
prefers libyaml's C loader when it is available and falls back to the pure-Python
one, which matters when a scan reads thousands of small files.
"""

from __future__ import annotations

from eventlineage.utils.cache import (
    CACHE_VERSION,
    CacheEntry,
    ScanCache,
    digest_bytes,
    digest_text,
)
from eventlineage.utils.logging import configure_logging
from eventlineage.utils.versioning import latest_version, version_key
from eventlineage.utils.yaml_io import load_yaml, load_yaml_file

__all__ = [
    "CACHE_VERSION",
    "CacheEntry",
    "ScanCache",
    "configure_logging",
    "digest_bytes",
    "digest_text",
    "latest_version",
    "load_yaml",
    "load_yaml_file",
    "version_key",
]
