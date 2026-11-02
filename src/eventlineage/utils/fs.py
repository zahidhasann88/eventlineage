"""Filesystem helpers: deterministic walking and glob matching."""

from __future__ import annotations

import fnmatch
import logging
import os
from collections.abc import Iterable, Iterator
from pathlib import Path

logger = logging.getLogger(__name__)


def matches_glob(relative_path: str, patterns: Iterable[str]) -> bool:
    """Match a POSIX-style relative path against a set of glob patterns.

    Supports both whole-path patterns (``schemas/*.json``) and fragment
    patterns (``**/node_modules/**``) by matching every path segment as well as
    the full path. This is intentionally more forgiving than ``fnmatch`` alone,
    which does not treat ``**`` specially.
    """
    candidate = relative_path.replace("\\", "/").removeprefix("./")
    segments = candidate.split("/")
    for pattern in patterns:
        normalised = pattern.replace("\\", "/")
        if fnmatch.fnmatch(candidate, normalised):
            return True
        fragment = normalised
        if fragment.startswith("**/"):
            fragment = fragment[3:]
        if fragment.endswith("/**"):
            fragment = fragment[:-3]
        if fragment and any(character in fragment for character in "*?[]"):
            if any(fnmatch.fnmatch(segment, fragment) for segment in segments):
                return True
        elif fragment and fragment in segments:
            return True
    return False


def relative_posix(path: Path, root: Path) -> str:
    """Path relative to ``root`` using forward slashes."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def iter_files(
    roots: Iterable[Path],
    *,
    exclude: Iterable[str] = (),
    include: Iterable[str] = (),
    follow_symlinks: bool = False,
    skip_hidden_dirs: bool = True,
    base: Path | None = None,
) -> Iterator[Path]:
    """Yield files under ``roots`` in deterministic order.

    Single files passed as roots are yielded directly, which lets the CLI accept
    ``eventlineage scan schemas/order.avsc``.
    """
    exclude_patterns = tuple(exclude)
    include_patterns = tuple(include)
    seen: set[Path] = set()
    for root in roots:
        root = root.expanduser()  # noqa: PLW2901 - normalize before use
        if root.is_file():
            resolved = root.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            yield resolved
            continue
        if not root.exists():
            logger.warning("scan root does not exist: %s", root)
            continue
        for current, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
            current_path = Path(current)
            dirnames.sort()
            filenames.sort()
            if skip_hidden_dirs:
                # Hidden directories (``.git``, ``.venv``, ``.eventlineage``) are never
                # event sources; skipping them early keeps scans fast and avoids
                # reading our own cache back in.
                dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            for filename in filenames:
                path = current_path / filename
                reference = relative_posix(path, base or root)
                if Path(reference).is_absolute():
                    # Outside the project: parts of the absolute path would match
                    # patterns such as ``**/build/**``.
                    reference = relative_posix(path, root)
                if exclude_patterns and matches_glob(reference, exclude_patterns):
                    continue
                if include_patterns and not matches_glob(reference, include_patterns):
                    continue
                if not path.is_file():
                    continue
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                yield path


def ensure_directory(path: Path) -> Path:
    """Create ``path`` if needed and return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_atomic(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (used for reports and exports)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


__all__ = ["ensure_directory", "iter_files", "matches_glob", "relative_posix", "write_atomic"]
