"""A dependency-free ``.proto`` parser.

EventLineage must run on a laptop with no ``protoc`` binary and no ``protobuf``
runtime, so this module implements the subset of proto2/proto3 needed for
contract analysis: syntax/package/import/option declarations, messages (with
nested messages, oneofs, maps and reserved ranges), enums and services.

What is deliberately not implemented: custom option value semantics (they are
kept as opaque tokens), extensions, and code generation. Unknown constructs are
skipped with brace balancing instead of aborting the parse, so a file using an
exotic feature still yields the events it declares.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import Field

from eventlineage.exceptions import ParseError
from eventlineage.models.base import EGBaseModel
from eventlineage.models.common import SchemaFormat
from eventlineage.models.schema import ParsedSchema, SchemaField
from eventlineage.parsers.schema_types import PROTOBUF_TYPES, CanonicalType


class TokenKind(StrEnum):
    """Lexical categories produced by the proto tokenizer."""

    IDENT = "ident"
    STRING = "string"
    NUMBER = "number"
    SYMBOL = "symbol"
    EOF = "eof"


@dataclass(slots=True)
class Token:
    """A single proto token with its source line."""

    kind: TokenKind
    value: str
    line: int
    doc: list[str] = field(default_factory=list)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Token({self.kind.value}, {self.value!r}, line={self.line})"


_IDENT_START = re.compile(r"[A-Za-z_]")
_IDENT_BODY = re.compile(r"[A-Za-z0-9_]")


def tokenize(text: str) -> list[Token]:
    """Split proto source into tokens, attaching preceding comments as docs."""
    tokens: list[Token] = []
    index = 0
    line = 1
    length = len(text)
    pending_doc: list[str] = []
    while index < length:
        char = text[index]
        if char == "\n":
            line += 1
            index += 1
            continue
        if char in " \t\r":
            index += 1
            continue
        if text.startswith("//", index):
            end = text.find("\n", index)
            end = length if end == -1 else end
            pending_doc.append(text[index + 2 : end].strip())
            index = end
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = length if end == -1 else end + 2
            block = text[index + 2 : end - 2]
            for comment_line in block.splitlines():
                pending_doc.append(comment_line.strip().lstrip("*").strip())
            line += block.count("\n")
            index = end
            continue
        if char in "\"'":
            quote = char
            cursor = index + 1
            buffer: list[str] = []
            while cursor < length and text[cursor] != quote:
                if text[cursor] == "\\" and cursor + 1 < length:
                    buffer.append(text[cursor : cursor + 2])
                    cursor += 2
                    continue
                if text[cursor] == "\n":
                    line += 1
                buffer.append(text[cursor])
                cursor += 1
            tokens.append(Token(TokenKind.STRING, "".join(buffer), line, pending_doc))
            pending_doc = []
            index = cursor + 1
            continue
        if _IDENT_START.match(char):
            cursor = index + 1
            while cursor < length and _IDENT_BODY.match(text[cursor]):
                cursor += 1
            tokens.append(Token(TokenKind.IDENT, text[index:cursor], line, pending_doc))
            pending_doc = []
            index = cursor
            continue
        if char.isdigit() or (char == "-" and index + 1 < length and text[index + 1].isdigit()):
            cursor = index + 1
            while cursor < length and (text[cursor].isalnum() or text[cursor] in ".+-_"):
                cursor += 1
            tokens.append(Token(TokenKind.NUMBER, text[index:cursor], line, pending_doc))
            pending_doc = []
            index = cursor
            continue
        tokens.append(Token(TokenKind.SYMBOL, char, line, pending_doc))
        pending_doc = []
        index += 1
    tokens.append(Token(TokenKind.EOF, "", line, pending_doc))
    return tokens


class ProtoEnumValue(EGBaseModel):
    """One value of a proto enum."""

    name: str
    number: int | None = None
    doc: str | None = None


class ProtoEnum(EGBaseModel):
    """A proto enum and its values."""

    name: str
    full_name: str = ""
    values: tuple[ProtoEnumValue, ...] = ()
    doc: str | None = None
    allow_alias: bool = False

    def symbol_names(self) -> tuple[str, ...]:
        """Raw token text of this declaration header."""
        return tuple(value.name for value in self.values)


class ProtoField(EGBaseModel):
    """One field of a proto message."""

    name: str
    number: int | None = None
    type: str
    label: str = "optional"
    map_key: str | None = None
    map_value: str | None = None
    oneof: str | None = None
    json_name: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    doc: str | None = None

    @property
    def is_repeated(self) -> bool:
        """Whether the field repeats (``repeated`` label)."""
        return self.label == "repeated" or self.map_key is not None


class ProtoMessage(EGBaseModel):
    """A proto message and its fields."""

    name: str
    full_name: str = ""
    fields: tuple[ProtoField, ...] = ()
    messages: tuple[ProtoMessage, ...] = ()
    enums: tuple[ProtoEnum, ...] = ()
    oneofs: tuple[str, ...] = ()
    reserved_numbers: tuple[int, ...] = ()
    reserved_names: tuple[str, ...] = ()
    doc: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)

    def iter_messages(self) -> Iterator[ProtoMessage]:
        """Iterate this message and its nested messages."""
        yield self
        for nested in self.messages:
            yield from nested.iter_messages()

    def field(self, name: str) -> ProtoField | None:
        """Look up a field by name (``None`` when absent)."""
        for entry in self.fields:
            if entry.name == name:
                return entry
        return None

    def field_by_number(self, number: int) -> ProtoField | None:
        """Look up a field by its wire number."""
        for entry in self.fields:
            if entry.number == number:
                return entry
        return None

    def find_nested(self, name: str) -> ProtoMessage | None:
        """Find this message or a nested message by simple or full name."""
        for candidate in self.iter_messages():
            if name in {candidate.name, candidate.full_name}:
                return candidate
        return None

    def is_annotated_event(self) -> bool:
        """Whether the message is explicitly marked with ``@event``."""
        doc = (self.doc or "").lower()
        if "@event" in doc:
            return True
        return any(str(value).lower() in {"true", "event"} for value in self.options.values())


class ProtoRpc(EGBaseModel):
    """One rpc method of a proto service."""

    name: str
    request: str
    response: str
    client_streaming: bool = False
    server_streaming: bool = False
    doc: str | None = None


class ProtoService(EGBaseModel):
    """A proto service and its rpcs."""

    name: str
    full_name: str = ""
    rpcs: tuple[ProtoRpc, ...] = ()
    doc: str | None = None


class ProtoFile(EGBaseModel):
    """A parsed ``.proto`` file."""

    syntax: str = "proto3"
    package: str = ""
    imports: tuple[str, ...] = ()
    options: dict[str, str] = Field(default_factory=dict)
    messages: tuple[ProtoMessage, ...] = ()
    enums: tuple[ProtoEnum, ...] = ()
    services: tuple[ProtoService, ...] = ()
    path: str | None = None

    def all_messages(self) -> tuple[ProtoMessage, ...]:
        """Every message in the file, nested ones included."""
        collected: list[ProtoMessage] = []
        for message in self.messages:
            collected.extend(message.iter_messages())
        return tuple(collected)

    def all_enums(self) -> tuple[ProtoEnum, ...]:
        """Every enum in the file, nested ones included."""
        collected: list[ProtoEnum] = list(self.enums)
        for message in self.all_messages():
            collected.extend(message.enums)
        return tuple(collected)

    def find_message(self, name: str) -> ProtoMessage | None:
        """Message with this (optionally qualified) name."""
        for message in self.all_messages():
            if name in (message.full_name, message.name):
                return message
        return None

    def enum_map(self) -> dict[str, ProtoEnum]:
        """Enums keyed by fully qualified name."""
        mapping: dict[str, ProtoEnum] = {}
        for entry in self.all_enums():
            mapping[entry.full_name or entry.name] = entry
            mapping.setdefault(entry.name, entry)
        return mapping

    def message_map(self) -> dict[str, ProtoMessage]:
        """Messages keyed by fully qualified name."""
        mapping: dict[str, ProtoMessage] = {}
        for entry in self.all_messages():
            mapping[entry.full_name or entry.name] = entry
            mapping.setdefault(entry.name, entry)
        return mapping


class _Parser:
    """Recursive-descent parser over the token stream."""

    def __init__(self, tokens: list[Token], path: str | None = None) -> None:
        self.tokens = tokens
        self.position = 0
        self.path = path
        self.package = ""

    def peek(self, offset: int = 0) -> Token:
        index = min(self.position + offset, len(self.tokens) - 1)
        return self.tokens[index]

    def next(self) -> Token:
        token = self.peek()
        if token.kind != TokenKind.EOF:
            self.position += 1
        return token

    def accept(self, value: str) -> bool:
        if self.peek().value == value:
            self.position += 1
            return True
        return False

    def expect(self, value: str) -> Token:
        token = self.peek()
        if token.value != value:
            raise ParseError(
                f"expected {value!r} but found {token.value!r}",
                path=self.path,
                line=token.line,
            )
        return self.next()

    def expect_ident(self) -> Token:
        token = self.peek()
        if token.kind != TokenKind.IDENT:
            raise ParseError(
                f"expected identifier but found {token.value!r}",
                path=self.path,
                line=token.line,
            )
        return self.next()

    def skip_block(self) -> None:
        """Skip a balanced ``{...}`` block (or a statement up to ``;``)."""
        if self.peek().value == "{":
            depth = 0
            while True:
                token = self.next()
                if token.kind == TokenKind.EOF:
                    return
                if token.value == "{":
                    depth += 1
                elif token.value == "}":
                    depth -= 1
                    if depth == 0:
                        return
        while True:
            token = self.next()
            if token.kind == TokenKind.EOF or token.value == ";":
                return

    def read_qualified_name(self) -> str:
        parts: list[str] = []
        if self.accept("."):
            parts.append("")
        parts.append(self.expect_ident().value)
        while self.peek().value == "." and self.peek(1).kind == TokenKind.IDENT:
            self.next()
            parts.append(self.next().value)
        return ".".join(parts).lstrip(".")

    def read_value(self) -> str:
        token = self.next()
        if token.value == "{":
            depth = 1
            chunks: list[str] = []
            while depth:
                current = self.next()
                if current.kind == TokenKind.EOF:
                    break
                if current.value == "{":
                    depth += 1
                elif current.value == "}":
                    depth -= 1
                    if not depth:
                        break
                chunks.append(current.value)
            return "{" + " ".join(chunks) + "}"
        return token.value

    def parse_file(self) -> ProtoFile:
        syntax = "proto2"
        package = ""
        imports: list[str] = []
        options: dict[str, str] = {}
        messages: list[ProtoMessage] = []
        enums: list[ProtoEnum] = []
        services: list[ProtoService] = []
        while self.peek().kind != TokenKind.EOF:
            token = self.peek()
            keyword = token.value
            if keyword in {"syntax", "edition"}:
                self.next()
                self.expect("=")
                syntax = self.read_value()
                self.accept(";")
            elif keyword == "package":
                self.next()
                package = self.read_qualified_name()
                self.accept(";")
            elif keyword == "import":
                self.next()
                if self.peek().value in {"public", "weak"}:
                    self.next()
                imports.append(self.read_value())
                self.accept(";")
            elif keyword == "option":
                self.next()
                name = self.read_qualified_name()
                if self.peek().value == "=":
                    self.next()
                    options[name] = self.read_value()
                self.accept(";")
            elif keyword == "message":
                messages.append(self.parse_message(package))
            elif keyword == "enum":
                enums.append(self.parse_enum(package))
            elif keyword == "service":
                services.append(self.parse_service(package))
            else:
                # Unknown top-level statement (extend, reserved, ...) — skip it.
                self.next()
                self.skip_block()
        return ProtoFile(
            syntax=syntax.strip('"'),
            package=package,
            imports=tuple(imports),
            options=options,
            messages=tuple(messages),
            enums=tuple(enums),
            services=tuple(services),
            path=self.path,
        )

    def parse_message(self, scope: str) -> ProtoMessage:
        start = self.expect("message")
        name = self.expect_ident().value
        full_name = f"{scope}.{name}" if scope else name
        self.expect("{")
        fields: list[ProtoField] = []
        nested: list[ProtoMessage] = []
        enums: list[ProtoEnum] = []
        oneofs: list[str] = []
        options: dict[str, Any] = {}
        reserved_numbers: list[int] = []
        reserved_names: list[str] = []
        while not self.accept("}"):
            if self.peek().kind == TokenKind.EOF:
                raise ParseError(f"unterminated message {name!r}", path=self.path, line=start.line)
            token = self.peek()
            keyword = token.value
            if keyword == "message":
                nested.append(self.parse_message(full_name))
            elif keyword == "enum":
                enums.append(self.parse_enum(full_name))
            elif keyword == "oneof":
                self.next()
                oneof_name = self.expect_ident().value
                oneofs.append(oneof_name)
                self.expect("{")
                while not self.accept("}"):
                    if self.peek().value == "option":
                        self.next()
                        self.read_qualified_name()
                        if self.accept("="):
                            self.read_value()
                        self.accept(";")
                        continue
                    parsed = self.parse_field(full_name, oneof=oneof_name)
                    if parsed is not None:
                        fields.append(parsed)
            elif keyword == "option":
                self.next()
                option_name = self.read_qualified_name()
                if self.accept("="):
                    options[option_name] = self.read_value()
                self.accept(";")
            elif keyword == "reserved":
                self.next()
                while True:
                    value = self.next()
                    if value.kind == TokenKind.NUMBER:
                        with contextlib.suppress(ValueError):
                            reserved_numbers.append(int(value.value))
                    elif value.kind == TokenKind.STRING:
                        reserved_names.append(value.value)
                    if self.accept(";"):
                        break
                    if self.peek().kind == TokenKind.EOF:
                        break
                    if self.peek().value == "to":
                        self.next()
            elif keyword in {"extensions", "extend"}:
                self.next()
                self.skip_block()
            else:
                parsed_field = self.parse_field(full_name)
                if parsed_field is not None:
                    fields.append(parsed_field)
                else:  # pragma: no cover - defensive: unknown construct
                    self.next()
                    self.skip_block()
        return ProtoMessage(
            name=name,
            full_name=full_name,
            fields=tuple(fields),
            messages=tuple(nested),
            enums=tuple(enums),
            oneofs=tuple(oneofs),
            reserved_numbers=tuple(reserved_numbers),
            reserved_names=tuple(reserved_names),
            doc="\n".join(start.doc) or None,
            options=options,
        )

    def parse_enum(self, scope: str) -> ProtoEnum:
        start = self.expect("enum")
        name = self.expect_ident().value
        full_name = f"{scope}.{name}" if scope else name
        self.expect("{")
        values: list[ProtoEnumValue] = []
        allow_alias = False
        while not self.accept("}"):
            token = self.peek()
            if token.kind == TokenKind.EOF:
                raise ParseError(f"unterminated enum {name!r}", path=self.path, line=token.line)
            if token.value == "option":
                self.next()
                option_name = self.read_qualified_name()
                if option_name == "allow_alias" and self.accept("="):
                    allow_alias = self.read_value().lower() == "true"
                elif self.accept("="):
                    self.read_value()
                self.accept(";")
                continue
            if token.value == "reserved":
                self.next()
                while not self.accept(";"):
                    if self.next().kind == TokenKind.EOF:
                        break
                continue
            if token.kind != TokenKind.IDENT:
                self.next()
                continue
            value_name = self.next().value
            number: int | None = None
            if self.accept("="):
                raw = self.read_value()
                try:
                    number = int(raw)
                except ValueError:
                    number = None
            if self.peek().value == "[":
                while self.peek().value not in {"]", ""}:
                    self.next()
                self.accept("]")
            self.accept(";")
            values.append(
                ProtoEnumValue(name=value_name, number=number, doc="\n".join(token.doc) or None)
            )
        return ProtoEnum(
            name=name,
            full_name=full_name,
            values=tuple(values),
            doc="\n".join(start.doc) or None,
            allow_alias=allow_alias,
        )

    def parse_service(self, scope: str) -> ProtoService:
        start = self.expect("service")
        name = self.expect_ident().value
        full_name = f"{scope}.{name}" if scope else name
        self.expect("{")
        rpcs: list[ProtoRpc] = []
        while not self.accept("}"):
            token = self.peek()
            if token.kind == TokenKind.EOF:
                raise ParseError(f"unterminated service {name!r}", path=self.path, line=start.line)
            if token.value == "rpc":
                self.next()
                rpc_name = self.expect_ident().value
                self.expect("(")
                client_streaming = self.accept("stream")
                request = self.read_qualified_name()
                self.expect(")")
                self.expect("returns")
                self.expect("(")
                server_streaming = self.accept("stream")
                response = self.read_qualified_name()
                self.expect(")")
                if self.peek().value == "{":
                    self.skip_block()
                else:
                    self.accept(";")
                rpcs.append(
                    ProtoRpc(
                        name=rpc_name,
                        request=request,
                        response=response,
                        client_streaming=client_streaming,
                        server_streaming=server_streaming,
                        doc="\n".join(token.doc) or None,
                    )
                )
            elif token.value == "option":
                self.next()
                self.read_qualified_name()
                if self.accept("="):
                    self.read_value()
                self.accept(";")
            else:
                self.next()
                self.skip_block()
        return ProtoService(
            name=name,
            full_name=full_name,
            rpcs=tuple(rpcs),
            doc="\n".join(start.doc) or None,
        )

    def parse_field(self, scope: str, *, oneof: str | None = None) -> ProtoField | None:
        token = self.peek()
        doc = "\n".join(token.doc) or None
        label = "optional"
        if token.value in {"repeated", "required", "optional"}:
            label = self.next().value
            token = self.peek()
        if token.kind != TokenKind.IDENT and token.value != "map":
            return None
        map_key: str | None = None
        map_value: str | None = None
        if token.value == "map" and self.peek(1).value == "<":
            self.next()
            self.expect("<")
            map_key = self.read_qualified_name()
            self.expect(",")
            map_value = self.read_qualified_name()
            self.expect(">")
            field_type = "map"
        else:
            field_type = self.read_qualified_name()
        name_token = self.peek()
        if name_token.kind != TokenKind.IDENT:
            return None
        name = self.next().value
        number: int | None = None
        if self.accept("="):
            raw = self.read_value()
            try:
                number = int(raw)
            except ValueError:
                number = None
        options: dict[str, Any] = {}
        if self.peek().value == "[":
            self.next()
            while self.peek().value not in {"]", ""}:
                if self.peek().kind == TokenKind.EOF:
                    break
                key = self.read_qualified_name()
                value: Any = True
                if self.accept("="):
                    value = self.read_value()
                options[key] = value
                if not self.accept(","):
                    break
            self.accept("]")
        self.accept(";")
        if oneof is not None:
            label = "optional"
        return ProtoField(
            name=name,
            number=number,
            type=field_type,
            label=label,
            map_key=map_key,
            map_value=map_value,
            oneof=oneof,
            json_name=str(options.get("json_name")) if "json_name" in options else None,
            options=options,
            doc=doc,
        )


def parse_proto_file(text: str, *, path: str | None = None) -> ProtoFile:
    """Parse proto source text into a :class:`ProtoFile`."""
    try:
        return _Parser(tokenize(text), path=path).parse_file()
    except ParseError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise ParseError(f"failed to parse proto file: {exc}", path=path) from exc


def _proto_type_to_field(
    field: ProtoField,
    *,
    messages: dict[str, ProtoMessage],
    enums: dict[str, ProtoEnum],
    depth: int,
    seen: frozenset[str],
) -> SchemaField:
    raw_type = field.map_value if field.type == "map" else field.type
    enum_entry = enums.get(raw_type or "")
    message_entry = messages.get(raw_type or "")

    if enum_entry is not None:
        canonical = CanonicalType.ENUM
        children: tuple[SchemaField, ...] = ()
        enum_values = enum_entry.symbol_names()
    elif message_entry is not None:
        canonical = CanonicalType.RECORD if field.type != "map" else CanonicalType.MAP
        enum_values = ()
        if message_entry.full_name in seen or depth <= 0:
            children = ()
        else:
            children = _message_fields(
                message_entry,
                messages=messages,
                enums=enums,
                depth=depth - 1,
                seen=seen | {message_entry.full_name},
            )
    else:
        canonical = PROTOBUF_TYPES.get(raw_type or "", CanonicalType.ANY)
        if canonical == CanonicalType.ANY and raw_type and raw_type[:1].isupper():
            canonical = CanonicalType.RECORD
        enum_values = ()
        children = ()

    if field.type == "map":
        return SchemaField(
            name=field.name,
            type=CanonicalType.MAP.value,
            required=False,
            repeated=False,
            constraints={
                "number": field.number,
                "label": field.label,
                "map_key": field.map_key,
                "map_value": field.map_value,
                **({"oneof": field.oneof} if field.oneof else {}),
            },
            fields=children,
            doc=field.doc,
            raw_type=f"map<{field.map_key},{field.map_value}>",
        )

    return SchemaField(
        name=field.name,
        type=canonical.value,
        required=field.label == "required",
        repeated=field.label == "repeated",
        enum_values=enum_values,
        constraints={
            "number": field.number,
            "label": field.label,
            **({"oneof": field.oneof} if field.oneof else {}),
            **({"json_name": field.json_name} if field.json_name else {}),
        },
        fields=children,
        doc=field.doc,
        raw_type=field.type,
    )


def _message_fields(
    message: ProtoMessage,
    *,
    messages: dict[str, ProtoMessage],
    enums: dict[str, ProtoEnum],
    depth: int = 16,
    seen: frozenset[str] = frozenset(),
) -> tuple[SchemaField, ...]:
    return tuple(
        _proto_type_to_field(
            field,
            messages=messages,
            enums=enums,
            depth=depth,
            seen=seen,
        )
        for field in message.fields
    )


def message_to_parsed(
    message: ProtoMessage, proto_file: ProtoFile, *, file_path: str | None = None
) -> ParsedSchema:
    """Lower a single protobuf message into normalized fields."""
    return ParsedSchema(
        format=SchemaFormat.PROTOBUF,
        root_type="record",
        fields=_message_fields(
            message,
            messages=proto_file.message_map(),
            enums=proto_file.enum_map(),
            seen=frozenset({message.full_name}),
        ),
        name=message.full_name or message.name,
        metadata={
            "package": proto_file.package,
            "syntax": proto_file.syntax,
            "file": file_path or proto_file.path,
            "fields": len(message.fields),
        },
    )


def parse_protobuf(definition: Any, *, message_name: str | None = None) -> ParsedSchema:
    """Lower a protobuf schema definition into normalized fields.

    ``definition`` is either proto source text, a serialized
    ``{"file": <ProtoFile>, "message": <full name>}`` envelope (what discovery
    stores on events), or a bare message dump.
    """
    if isinstance(definition, str):
        proto_file = parse_proto_file(definition)
        message = _select_message(proto_file, message_name)
        if message is None:
            return ParsedSchema(format=SchemaFormat.PROTOBUF, root_type="record")
        return message_to_parsed(message, proto_file, file_path=proto_file.path)
    if isinstance(definition, dict):
        if "file" in definition:
            proto_file = ProtoFile.model_validate(definition["file"])
            full_name = definition.get("message") or message_name
            message = proto_file.find_message(full_name) if full_name else None
            if message is None:
                return ParsedSchema(format=SchemaFormat.PROTOBUF, root_type="record")
            return message_to_parsed(message, proto_file)
        if "fields" in definition:
            message = ProtoMessage.model_validate(
                {**definition, "name": definition.get("name", "Message")}
            )
            proto_file = ProtoFile(messages=(message,))
            return message_to_parsed(message, proto_file)
        if "messages" in definition or "syntax" in definition or "package" in definition:
            proto_file = ProtoFile.model_validate(definition)
            message = _select_message(proto_file, message_name)
            if message is None:
                return ParsedSchema(format=SchemaFormat.PROTOBUF, root_type="record")
            return message_to_parsed(message, proto_file)
    return ParsedSchema(format=SchemaFormat.PROTOBUF, root_type="record")


def _select_message(proto_file: ProtoFile, message_name: str | None) -> ProtoMessage | None:
    if message_name:
        return proto_file.find_message(message_name)
    messages = proto_file.all_messages()
    if not messages:
        return None
    annotated = [message for message in messages if message.is_annotated_event()]
    if annotated:
        return annotated[0]
    return messages[0]


def definition_from_file(proto_file: ProtoFile, message: ProtoMessage) -> dict[str, Any]:
    """Build the self-contained envelope stored on an event's ``EventSchema``.

    The source path is dropped on purpose: a contract's identity must not depend
    on where the repository happens to be checked out, otherwise schema hashes
    (and therefore reports and caches) would differ between machines.
    """
    payload = proto_file.model_dump(mode="json", exclude_none=True)
    payload.pop("path", None)
    envelope: dict[str, Any] = json.loads(
        json.dumps({"file": payload, "message": message.full_name or message.name})
    )
    return envelope


def event_messages(
    proto_file: ProtoFile, *, suffixes: tuple[str, ...] = ("Event",), annotations: bool = True
) -> tuple[ProtoMessage, ...]:
    """Select the messages that represent events in a proto file.

    A message is an event when it carries an ``@event`` annotation (doc comment
    or option) or its name ends with one of the configured suffixes. Nested
    messages are included because request/response envelopes are usually nested.
    """
    selected: list[ProtoMessage] = []
    for message in proto_file.all_messages():
        if annotations and message.is_annotated_event():
            selected.append(message)
            continue
        if any(message.name.endswith(suffix) for suffix in suffixes if suffix):
            selected.append(message)
    return tuple(selected)


__all__ = [
    "ProtoEnum",
    "ProtoEnumValue",
    "ProtoField",
    "ProtoFile",
    "ProtoMessage",
    "ProtoRpc",
    "ProtoService",
    "Token",
    "TokenKind",
    "definition_from_file",
    "event_messages",
    "message_to_parsed",
    "parse_proto_file",
    "parse_protobuf",
    "tokenize",
]
