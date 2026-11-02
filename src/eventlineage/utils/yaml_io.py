"""YAML loading with the fastest safe loader available.

PyYAML's pure-Python loader is roughly an order of magnitude slower than the
libyaml-backed ``CSafeLoader``. Discovery is dominated by YAML parsing on large
repositories, so EventLineage uses the C loader whenever it is compiled in and
falls back transparently otherwise (see ``docs/performance.md``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

#: The fastest *safe* loader available in this interpreter.
SafeLoader: type[yaml.SafeLoader] = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def load_yaml(text: str) -> Any:
    """Parse a YAML document with the safest/fastest loader available."""
    return yaml.load(text, Loader=SafeLoader)


def load_yaml_file(path: Path | str) -> Any:
    """Read and parse a YAML file."""
    return load_yaml(Path(path).read_text(encoding="utf-8"))


__all__ = ["SafeLoader", "load_yaml", "load_yaml_file"]
