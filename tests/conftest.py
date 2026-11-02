"""Shared pytest fixtures and factory helpers.

The fixtures here build catalogs from plain dictionaries so unit tests stay
readable: no test needs to know about discovery plugins unless it is testing
them explicitly.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

# Hypothesis caches inferred constants and failing examples in ``./.hypothesis``
# by default, i.e. inside the repository, and rewrites that cache on every run.
# Point it at the system temp directory before Hypothesis is imported so the
# working tree stays quiet, and keep an in-memory example database unless the
# caller asks for a persistent one via HYPOTHESIS_DATABASE_FILE.
os.environ.setdefault(
    "HYPOTHESIS_STORAGE_DIRECTORY",
    str(Path(tempfile.gettempdir()) / "eventlineage-hypothesis"),
)

import pytest
from hypothesis import settings as hypothesis_settings

if not os.environ.get("HYPOTHESIS_DATABASE_FILE"):
    hypothesis_settings.register_profile("eventlineage", database=None)
    hypothesis_settings.load_profile("eventlineage")

from eventlineage.config import Config
from eventlineage.core.catalog import EventCatalog
from eventlineage.models.event import Event
from eventlineage.models.participant import Consumer, Producer, Service
from eventlineage.models.schema import EventSchema, EventVersion
from eventlineage.models.topic import Topic

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_PROJECT = FIXTURES / "sample-project"
REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples"


def make_schema(definition: Any, fmt: str = "json-schema", **kwargs: Any) -> EventSchema:
    return EventSchema(format=fmt, definition=definition, **kwargs)


def make_event(
    event_id: str,
    *,
    schema: Any = None,
    schema_format: str = "json-schema",
    producers: Iterable[str] = (),
    consumers: Iterable[str] = (),
    topics: Iterable[str] = (),
    version: str = "1.0.0",
    versions: Iterable[tuple[str, Any]] | None = None,
    owners: Iterable[str] = (),
    tags: Iterable[str] = (),
    description: str | None = None,
) -> Event:
    """Build an event with sensible defaults for tests."""
    if isinstance(schema, EventSchema):
        schema_obj = schema
    else:
        schema_obj = make_schema(schema, schema_format) if schema is not None else None
    version_objs = [
        EventVersion(
            version=number,
            schema=make_schema(payload, schema_format) if payload is not None else None,
        )
        for number, payload in (versions or ())
    ]
    return Event(
        id=event_id,
        name=event_id,
        version=version,
        schema=schema_obj,
        versions=tuple(version_objs),
        producers=tuple(
            item if isinstance(item, Producer) else Producer(service=item) for item in producers
        ),
        consumers=tuple(
            item if isinstance(item, Consumer) else Consumer(service=item) for item in consumers
        ),
        topics=tuple(topics),
        owners=tuple(owners),
        tags=tuple(tags),
        description=description,
    )


def make_catalog(
    events: Iterable[Event] = (),
    *,
    services: Iterable[Service] = (),
    topics: Iterable[Topic] = (),
) -> EventCatalog:
    catalog = EventCatalog(project_name="test")
    for event in events:
        catalog.add_event(event)
    for service in services:
        catalog.add_service(service)
    for topic in topics:
        catalog.add_topic(topic)
    return catalog.normalize()


def simple_catalog() -> EventCatalog:
    """order.created -> payment.captured -> order.shipped chain."""
    return make_catalog(
        [
            make_event(
                "order.created",
                producers=["order-service"],
                consumers=["payment-service"],
                topics=["orders.v1"],
            ),
            make_event(
                "payment.captured",
                producers=["payment-service"],
                consumers=["shipping-service"],
                topics=["payments.v1"],
            ),
            make_event(
                "order.shipped",
                producers=["shipping-service"],
                consumers=[],
                topics=["shipping.v1"],
            ),
        ]
    )


@pytest.fixture
def catalog() -> EventCatalog:
    return simple_catalog()


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A copy of the sample project inside a temporary directory."""
    target = tmp_path / "sample-project"
    shutil.copytree(SAMPLE_PROJECT, target)
    shutil.rmtree(target / ".eventlineage", ignore_errors=True)
    return target


@pytest.fixture
def make_project(tmp_path: Path) -> Callable[..., Path]:
    """Factory that writes a minimal project and returns its root."""

    def _make(
        *,
        config: str | Mapping[str, Any] | None = None,
        files: Mapping[str, str] | None = None,
        name: str = "proj",
    ) -> Path:
        import json

        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        if config is None:
            config_text = "version: 1\nproject:\n  name: proj\n"
        elif isinstance(config, Mapping):
            import yaml

            config_text = yaml.safe_dump(dict(config), sort_keys=False)
        else:
            config_text = config
        (root / "eventlineage.yaml").write_text(config_text, encoding="utf-8")
        for relative, payload in (files or {}).items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            text = (
                json.dumps(payload, indent=2) if isinstance(payload, (dict, list)) else str(payload)
            )
            path.write_text(text, encoding="utf-8")
        return root

    return _make


@pytest.fixture
def sample_config(project_dir: Path) -> Config:
    return Config.load(project_dir)


@pytest.fixture
def example_project() -> Path:
    """The example project shipped in ``examples/shop-platform``."""
    path = EXAMPLES / "shop-platform"
    if not (path / "eventlineage.yaml").is_file():  # pragma: no cover
        pytest.skip("example project is not available")
    return path
