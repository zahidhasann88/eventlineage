"""Confluent Schema Registry integration (HTTP/REST).

The client is deliberately tiny: three GETs, basic-auth support, and an
injectable ``client`` so unit tests run against a fake transport instead of a
registry. Newer Registries expose the same v1 REST surface, and compatible ones
(Redpanda, Apicurio's Confluent API) work too.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from eventlineage.exceptions import IntegrationError
from eventlineage.integrations.base import Integration, IntegrationResult
from eventlineage.models.common import SchemaFormat
from eventlineage.models.event import Event
from eventlineage.models.schema import EventSchema, EventVersion
from eventlineage.utils.versioning import version_key

_SUFFIXES = ("-value", "-key", ".value", ".key")


def strip_subject_suffix(subject: str) -> str:
    """Drop the ``-value``/``-key`` suffix from a subject."""
    lowered = subject.lower()
    for suffix in _SUFFIXES:
        if lowered.endswith(suffix):
            return subject[: len(subject) - len(suffix)]
    return subject


class _HttpResponseProtocol:
    """Structural type for the tiny slice of ``httpx.Response`` we use."""

    status_code: int

    def json(self) -> Any: ...  # pragma: no cover - protocol

    def raise_for_status(self) -> None: ...  # pragma: no cover - protocol


class SchemaRegistryIntegration(Integration):
    """Fetch subjects and schemas from a Schema Registry."""

    name: ClassVar[str] = "schema-registry"
    requires: ClassVar[str | None] = "httpx"
    description: ClassVar[str] = "Confluent Schema Registry subjects, versions and schemas"

    def __init__(
        self,
        url: str | None = None,
        *,
        username: str | None = None,
        password: str | None = None,
        verify: bool = True,
        timeout: float = 10.0,
        client: Any | None = None,
        include_keys: bool = False,
    ) -> None:
        super().__init__(url=url)
        self.url = (url or "").rstrip("/")
        self.username = username
        self.password = password
        self.verify = verify
        self.timeout = timeout
        self._client = client
        self.include_keys = include_keys

    def client(self) -> Any:
        """Return an HTTP client bound to the registry."""
        if self._client is not None:
            return self._client
        module = self.require_dependency()
        if not self.url:
            raise IntegrationError(
                "schema-registry integration requires a 'url'",
                hint="set integrations.schema_registry.url in eventlineage.yaml",
            )
        auth = (self.username, self.password) if self.username else None
        self._client = module.Client(
            base_url=self.url, auth=auth, verify=self.verify, timeout=self.timeout
        )
        return self._client

    def _get(self, path: str) -> Any:
        client = self.client()
        try:
            response = client.get(path)
        except Exception as exc:
            raise IntegrationError(f"schema registry request failed: {exc}") from exc
        status = getattr(response, "status_code", 200)
        if status == 404:
            return None
        if status >= 400:
            raise IntegrationError(
                f"schema registry returned HTTP {status} for {path}",
                hint="check the registry URL and credentials",
            )
        return response.json()

    def subjects(self) -> tuple[str, ...]:
        """All subject names (key subjects excluded unless requested)."""
        payload = self._get("/subjects") or []
        names = [str(name) for name in payload]
        if not self.include_keys:
            names = [name for name in names if not name.lower().endswith(("-key", ".key"))]
        return tuple(sorted(names))

    def versions(self, subject: str) -> tuple[int, ...]:
        """List registered versions of a subject."""
        payload = self._get(f"/subjects/{subject}/versions") or []
        return tuple(sorted(int(version) for version in payload))

    def schema(self, subject: str, version: int | str = "latest") -> dict[str, Any] | None:
        """Fetch one schema definition."""
        payload = self._get(f"/subjects/{subject}/versions/{version}")
        if not payload:
            return None
        return {
            "subject": subject,
            "version": str(payload.get("version", version)),
            "id": payload.get("id"),
            "schema": payload.get("schema"),
            "schemaType": payload.get("schemaType", "AVRO"),
        }

    def collect(self) -> IntegrationResult:
        """Fetch every subject and turn them into fragments."""
        result = IntegrationResult()
        by_event: dict[str, list[dict[str, Any]]] = {}
        for subject in self.subjects():
            entries: list[dict[str, Any]] = []
            for version in self.versions(subject):
                payload = self.schema(subject, version)
                if payload:
                    entries.append(payload)
            if not entries:
                continue
            latest = self.schema(subject, "latest") or entries[-1]
            if latest not in entries:
                entries.append(latest)
            # With ``include_keys`` a key subject stays its own event, never a version
            # of the value schema.
            is_key = subject.lower().endswith(("-key", ".key"))
            by_event.setdefault(subject if is_key else strip_subject_suffix(subject), []).extend(
                entries
            )

        for event_name, entries in sorted(by_event.items()):
            parsed: list[tuple[str, EventSchema]] = []
            for entry in entries:
                schema = self._event_schema(entry)
                if schema is None:
                    result.notes.append(
                        f"subject {entry.get('subject')!r} version {entry.get('version')} "
                        "has an unsupported schema"
                    )
                    continue
                parsed.append((str(entry.get("version", "1")), schema))
            if not parsed:
                continue
            parsed.sort(key=lambda item: version_key(item[0]))
            versions = tuple(
                EventVersion(version=version, schema=schema) for version, schema in parsed
            )
            result.add_event(
                Event(
                    id=event_name,
                    name=event_name,
                    version=parsed[-1][0],
                    schema=parsed[-1][1],
                    versions=versions,
                    metadata={"registry": self.url, "subject": entries[0].get("subject")},
                    source=self.source(f"{self.url}/{event_name}"),
                )
            )
        result.notes.append(f"discovered {len(result.events)} event(s) from Schema Registry")
        return result

    def export(self, result: IntegrationResult) -> dict[str, Any]:
        """Render ``result`` in the ``schema-registry-export`` format."""
        names = {
            SchemaFormat.AVRO: "AVRO",
            SchemaFormat.JSON_SCHEMA: "JSON",
            SchemaFormat.PROTOBUF: "PROTOBUF",
        }
        subjects: list[dict[str, Any]] = []
        for event in sorted(result.events, key=lambda event: event.id):
            versions: list[dict[str, Any]] = []
            for entry in event.versions:
                schema = entry.schema
                if schema is None:
                    continue
                versions.append(
                    {
                        "version": int(entry.version),
                        "id": int(schema.schema_id) if schema.schema_id else None,
                        "schemaType": names.get(schema.format, "AVRO"),
                        "schema": schema.raw or json.dumps(schema.definition, sort_keys=True),
                    }
                )
            subjects.append({"subject": event.metadata["subject"], "versions": versions})
        return {"subjects": subjects}

    def _event_schema(self, entry: dict[str, Any]) -> EventSchema | None:
        raw = entry.get("schema")
        if raw is None:
            return None
        schema_type = str(entry.get("schemaType") or "AVRO").upper()
        format_map = {
            "AVRO": SchemaFormat.AVRO,
            "JSON": SchemaFormat.JSON_SCHEMA,
            "JSONSCHEMA": SchemaFormat.JSON_SCHEMA,
            "PROTOBUF": SchemaFormat.PROTOBUF,
        }
        fmt = format_map.get(schema_type, SchemaFormat.AVRO)
        definition: Any = raw
        if isinstance(raw, str) and fmt != SchemaFormat.PROTOBUF:
            try:
                definition = json.loads(raw)
            except json.JSONDecodeError:
                definition = raw
        return EventSchema(
            format=fmt,
            definition=definition,
            raw=raw if isinstance(raw, str) else None,
            version=str(entry.get("version", "1")),
            schema_id=str(entry["id"]) if entry.get("id") is not None else None,
            source=self.source(f"{self.url}/{entry.get('subject')}/{entry.get('version')}"),
        )


__all__ = ["SchemaRegistryIntegration", "strip_subject_suffix"]
