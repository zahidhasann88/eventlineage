"""Loading schema documents from disk (used by ``eventlineage compare``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from eventlineage.exceptions import ParseError, UsageError
from eventlineage.models.common import SchemaFormat, SourceLocation
from eventlineage.models.schema import EventSchema
from eventlineage.parsers import detect_schema_format
from eventlineage.utils.yaml_io import load_yaml

_TEXT_SUFFIXES = {".proto", ".avdl"}


def load_schema_document(
    path: Path | str, *, format: SchemaFormat | str | None = None
) -> EventSchema:
    """Read a schema file and return a normalized :class:`EventSchema`.

    Supports JSON Schema/JSON (``.json``), Avro (``.avsc``), Protobuf
    (``.proto``) and YAML documents. The dialect is taken from ``format`` when
    given, otherwise inferred from the content and the file extension.
    """
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise UsageError(f"{file_path}: schema file not found")
    try:
        text = file_path.read_text(encoding="utf-8-sig")
    except OSError as exc:  # pragma: no cover - filesystem failure
        raise ParseError(f"cannot read schema: {exc}", path=file_path) from exc

    definition: Any
    suffix = file_path.suffix.lower()
    if suffix in _TEXT_SUFFIXES:
        definition = text
        detected = SchemaFormat.PROTOBUF
    else:
        try:
            definition = (
                json.loads(text) if suffix in {".json", ".avsc", ".avro"} else load_yaml(text)
            )
        except json.JSONDecodeError as exc:
            raise ParseError(f"invalid JSON: {exc.msg}", path=file_path, line=exc.lineno) from exc
        except yaml.YAMLError as exc:
            raise ParseError(f"invalid YAML: {exc}", path=file_path) from exc
        detected = detect_schema_format(definition)
        if detected == SchemaFormat.UNKNOWN and suffix == ".avsc":
            detected = SchemaFormat.AVRO

    if format is None:
        resolved = detected
    else:
        resolved = SchemaFormat.coerce(format)
        if resolved is SchemaFormat.UNKNOWN:
            raise UsageError(
                f"unknown schema format {format!r}", hint="use avro, protobuf or json-schema"
            )
    name = definition.get("title") if isinstance(definition, dict) else None
    return EventSchema(
        format=resolved,
        definition=definition,
        raw=text if isinstance(definition, str) else None,
        name=str(name) if name else file_path.stem,
        source=SourceLocation(path=str(file_path)),
    )


__all__ = ["load_schema_document"]
