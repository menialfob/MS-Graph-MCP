"""Streaming parser for the Microsoft Graph OpenAPI description.

The v1.0 description is ~43 MB / 982k lines with 11,493 path templates and
17,777 operations. Loading it with PyYAML costs minutes and gigabytes, so we
exploit the fact that the file is machine-generated and rigidly indented:

    paths:
      /me/messages:                 # 2 spaces (may be single-quoted)
        get:                        # 4 spaces
          tags:                     # 6 spaces
            - users.message         # 8 spaces
          summary: Get messages from users
          operationId: users.ListMessages

Verified against the real file: no block scalars appear in operation fields,
so every value is a single-line scalar (occasionally single-quoted with ''
escaping). That makes a line-oriented state machine both safe and fast.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

METHODS = {"get", "post", "patch", "put", "delete"}

_PATH_RE = re.compile(r"^ {2}(?P<path>'[^']+'|\"[^\"]+\"|/\S*?):\s*$")
_VERB_RE = re.compile(r"^ {4}(?P<verb>[a-z]+):\s*$")
_FIELD_RE = re.compile(r"^ {6}(?P<key>[A-Za-z0-9$_-]+):(?P<rest>.*)$")
_TAG_RE = re.compile(r"^ {8}- (?P<tag>\S+)\s*$")
_PARAM_NAME_RE = re.compile(r"^ {8}- name: (?P<name>\S+)\s*$")
_PARAM_REF_RE = re.compile(r"^ {8}- \$ref: '#/components/parameters/(?P<name>[^']+)'\s*$")
_REF_RE = re.compile(r"\$ref: '#/components/(?P<kind>schemas|responses)/(?P<name>[^']+)'")
_PATH_PARAM_RE = re.compile(r"\{([^}]+)\}")


@dataclass
class Operation:
    """One callable Graph operation: a method against a path template."""

    path: str
    method: str
    operation_id: str = ""
    summary: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    query_params: list[str] = field(default_factory=list)
    response_type: str = ""
    request_type: str = ""
    pageable: bool = False

    @property
    def key(self) -> str:
        return f"{self.method.upper()} {self.path}"

    @property
    def path_params(self) -> list[str]:
        return _PATH_PARAM_RE.findall(self.path)

    @property
    def returns_collection(self) -> bool:
        return self.response_type.endswith("CollectionResponse") or self.pageable

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "path": self.path,
            "method": self.method.upper(),
            "operation_id": self.operation_id,
            "summary": self.summary,
            "description": self.description,
            "tags": self.tags,
            "query_params": self.query_params,
            "path_params": self.path_params,
            "response_type": self.response_type,
            "request_type": self.request_type,
            "returns_collection": self.returns_collection,
        }


def unquote(value: str) -> str:
    """Unwrap a single-line YAML scalar, handling '' escaping."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        inner = value[1:-1]
        return inner.replace("''", "'") if value[0] == "'" else inner
    return value


def _strip_type_prefix(ref: str) -> str:
    """'microsoft.graph.messageCollectionResponse' -> 'microsoft.graph.message'."""
    for suffix in ("CollectionResponse", "Response"):
        if ref.endswith(suffix):
            return ref[: -len(suffix)]
    return ref


def parse(path: Path) -> Iterator[Operation]:
    """Yield every operation in the description, in file order."""
    current_path: str | None = None
    op: Operation | None = None
    section = ""  # which 6-space block we are inside

    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue

            indent = len(line) - len(line.lstrip(" "))

            # A new path template closes any operation in progress.
            if indent == 2:
                m = _PATH_RE.match(line)
                if m:
                    if op:
                        yield op
                        op = None
                    current_path = unquote(m.group("path"))
                    section = ""
                continue

            if current_path is None:
                continue

            if indent == 4:
                m = _VERB_RE.match(line)
                if op:
                    yield op
                    op = None
                section = ""
                if m and m.group("verb") in METHODS:
                    op = Operation(path=current_path, method=m.group("verb"))
                continue

            if op is None:
                continue

            if indent == 6:
                m = _FIELD_RE.match(line)
                if not m:
                    continue
                key, rest = m.group("key"), m.group("rest")
                section = key
                value = unquote(rest) if rest.strip() else ""
                if key == "summary":
                    op.summary = value
                elif key == "description":
                    op.description = value
                elif key == "operationId":
                    op.operation_id = value
                elif key == "x-ms-pageable":
                    op.pageable = True
                continue

            # Deeper lines belong to whichever 6-space section is open.
            if section == "tags":
                m = _TAG_RE.match(line)
                if m:
                    op.tags.append(m.group("tag"))
            elif section == "parameters":
                m = _PARAM_REF_RE.match(line) or _PARAM_NAME_RE.match(line)
                if m:
                    op.query_params.append(m.group("name"))
            elif section in ("responses", "requestBody"):
                m = _REF_RE.search(line)
                if not m:
                    continue
                name = m.group("name")
                if name == "error":
                    continue
                if section == "responses" and not op.response_type:
                    op.response_type = _strip_type_prefix(name)
                elif section == "requestBody" and not op.request_type:
                    op.request_type = name

    if op:
        yield op
