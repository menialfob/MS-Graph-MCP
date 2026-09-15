"""Build the operation catalog: the joined, curated view of the Graph surface.

The catalog draws a deliberate line between two sets:

* **Indexed** operations have human-written documentation. Only these go into
  the retrieval index, because an operation whose entire description reads
  "Get media content for the navigation property items from drives" cannot be
  found by natural language, and including it only adds noise that pushes real
  answers down the ranking.

* **Executable** operations are the whole curated set. The undocumented ones
  are overwhelmingly deeper nesting variants of documented actions --
  ``/me/mailFolders/{}/childFolders/{}/messages/{}/reply`` is the same action
  as the documented ``/me/messages/{}/reply``. The model finds the documented
  action, then constructs whichever concrete path it needs, and the executor
  validates that against the full route table.

So retrieval stays clean without losing reach.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pipeline.join import normalize_path
from pipeline.parse_docs import DocPage
from pipeline.parse_openapi import Operation
from pipeline.parse_permissions import PermissionSet

# Path families that a question refers to in the first person.
_SELF_PREFIXES = ("/me",)


@dataclass
class CatalogEntry:
    key: str
    path: str
    method: str
    operation_id: str
    tags: list[str]
    path_params: list[str]
    query_params: list[str]
    response_type: str
    request_type: str
    returns_collection: bool
    # From OpenAPI, always present but often mechanical.
    summary: str = ""
    spec_description: str = ""
    # From the docs, present only for indexed entries.
    title: str = ""
    description: str = ""
    intro: str = ""
    doc_url: str = ""
    permissions: dict = field(default_factory=dict)
    utterances: list[str] = field(default_factory=list)
    overloads: int = 1

    @property
    def indexed(self) -> bool:
        return bool(self.title)

    @property
    def is_self(self) -> bool:
        return self.path.startswith(_SELF_PREFIXES)

    @property
    def least_delegated(self) -> list[str]:
        return self.permissions.get("least", {}).get("delegated_work", [])

    def index_text(self, include_docs: bool = True, include_utterances: bool = True) -> str:
        """The text blob this entry is retrieved by.

        The two flags exist so the build can be ablated: we want to know what
        the docs join and the generated utterances are actually worth, rather
        than assuming they earn their cost.
        """
        if include_docs:
            parts = [self.title or self.summary, f"{self.method} {self.path}"]
            parts += [self.description, self.intro, " ".join(self.tags)]
        else:
            # No doc-derived text at all, including the title.
            parts = [self.summary, f"{self.method} {self.path}"]
            parts += [self.spec_description, " ".join(self.tags)]
        if include_utterances:
            parts += self.utterances
        return " ".join(p for p in parts if p)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "path": self.path,
            "method": self.method,
            "operation_id": self.operation_id,
            "tags": self.tags,
            "path_params": self.path_params,
            "query_params": self.query_params,
            "response_type": self.response_type,
            "request_type": self.request_type,
            "returns_collection": self.returns_collection,
            "summary": self.summary,
            "spec_description": self.spec_description,
            "title": self.title,
            "description": self.description,
            "intro": self.intro,
            "doc_url": self.doc_url,
            "permissions": self.permissions,
            "utterances": self.utterances,
            "overloads": self.overloads,
            "indexed": self.indexed,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CatalogEntry":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def build(
    ops: list[Operation],
    doc_by_op: dict[str, DocPage],
    permissions: dict[str, PermissionSet],
) -> list[CatalogEntry]:
    """One catalog entry per (method, path).

    OData function overloads share a path and differ only in their parameter
    signature, which the description disambiguates with a hash suffix
    (``...getActivitiesByInterval-4c35``). GET /sites/{}/getActivitiesByInterval
    has 24 of them. They are one action as far as a person is concerned, so
    they collapse to one entry here; the distinct signatures belong in
    describe_operation, not in 24 identical search results.
    """
    entries: list[CatalogEntry] = []
    seen: dict[str, CatalogEntry] = {}
    for op in ops:
        if (existing := seen.get(op.key)) is not None:
            existing.overloads += 1
            continue
        entry = CatalogEntry(
            key=op.key,
            path=op.path,
            method=op.method.upper(),
            operation_id=op.operation_id,
            tags=op.tags,
            path_params=op.path_params,
            query_params=op.query_params,
            response_type=op.response_type,
            request_type=op.request_type,
            returns_collection=op.returns_collection,
            summary=op.summary,
            spec_description=op.description,
        )
        seen[op.key] = entry
        page = doc_by_op.get(op.key)
        if page is not None:
            entry.title = page.title
            entry.description = page.description
            entry.intro = page.intro
            entry.doc_url = page.doc_url
            if perms := permissions.get(page.permissions_include):
                entry.permissions = perms.to_dict()
        entries.append(entry)
    return entries


def route_table(entries: list[CatalogEntry]) -> dict[str, list[str]]:
    """Normalised path template -> allowed methods.

    This is what the executor validates against, so it covers every curated
    operation, not just the indexed ones.
    """
    table: dict[str, list[str]] = {}
    for e in entries:
        table.setdefault(normalize_path(e.path), []).append(e.method)
    return {k: sorted(set(v)) for k, v in table.items()}


def add_doc_routes(table: dict[str, list[str]], pages, profile) -> int:
    """Union documented-but-unspecified templates into the route table.

    Only templates that pass the same scope rules are added, so this widens
    what is callable without widening what the profile allows.
    """
    from graph_mcp.policy.globs import match_any

    added = 0
    for page in pages:
        for method, raw_path in page.http_templates:
            path = normalize_path(raw_path)
            methods = table.get(path)
            if methods is not None and method.upper() in methods:
                continue
            if match_any(profile.exclude, path) or not match_any(profile.include, path):
                continue
            if method.upper() != "GET" and match_any(profile.read_only, path):
                continue
            table.setdefault(path, [])
            if method.upper() not in table[path]:
                table[path].append(method.upper())
                added += 1
    for path in table:
        table[path] = sorted(set(table[path]))
    return added
