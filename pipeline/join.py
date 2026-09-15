"""Join human-written doc pages onto machine-generated OpenAPI operations.

Matching on ``operationId`` does not work: the spec says ``users.ListMessages``
while the doc file is ``user-list-messages.md``, and the two naming schemes
disagree often enough (singular/plural, verb placement, navigation-property
casing) that string munging produces both misses and false matches.

The reliable key is the path template itself. Every doc page states the
templates it documents in its ``## HTTP request`` block, so we normalise both
sides to a canonical shape and join on ``(METHOD, normalised path)``:

    docs:    GET /users/{id | userPrincipalName}/messages
    openapi: GET /users/{user-id}/messages
    both ->  get /users/{}/messages

Parameter *names* are deliberately discarded -- only the position of a
parameter is structurally meaningful, and the two sources name them
differently. One doc page legitimately fans out to several operations
(``/me/messages`` and ``/users/{}/messages`` share a page), which is exactly
what we want: both operations inherit the same human description.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pipeline.parse_docs import DocPage
from pipeline.parse_openapi import Operation

_PARAM_RE = re.compile(r"\{[^}]*\}")
_VERSION_PREFIX_RE = re.compile(r"^/(v1\.0|beta)(?=/)")
# OData function calls: the description writes "/me/messages/delta()" and
# "/applications/{}/federatedIdentityCredentials(name='{}')", while the docs
# write the bare function name. 599 v1.0 paths use the parenthesised form.
_CALL_RE = re.compile(r"\([^)]*\)")


def normalize_path(path: str) -> str:
    """Canonical join key for a path template."""
    path = path.split("?", 1)[0].split("#", 1)[0].strip()
    path = _VERSION_PREFIX_RE.sub("", path)
    path = _PARAM_RE.sub("{}", path)
    path = _CALL_RE.sub("", path)
    path = re.sub(r"/+", "/", path).rstrip("/")
    # OData type-cast segments are noise for matching purposes.
    path = path.replace("/microsoft.graph.", "/")
    return path.lower() or "/"


def join_key(method: str, path: str) -> str:
    return f"{method.lower()} {normalize_path(path)}"


# /me/X and /users/{id}/X are the same operation reached two ways, and the
# docs routinely document only one of them. The calendarView page lists
# /users/{id}/calendarView and /me/calendar/calendarView but not the bare
# /me/calendarView -- which is the single most natural way to ask for "my
# events next week". Propagating a doc match across this equivalence recovers
# those without inventing anything.
_ME_PREFIX = "/me"
_USER_PREFIX = "/users/{}"


def alias_keys(method: str, path: str) -> list[str]:
    """Equivalent join keys for the same operation under the other addressing."""
    norm = normalize_path(path)
    out = []
    if norm == _ME_PREFIX or norm.startswith(_ME_PREFIX + "/"):
        out.append(f"{method.lower()} {_USER_PREFIX}{norm[len(_ME_PREFIX):]}")
    elif norm == _USER_PREFIX or norm.startswith(_USER_PREFIX + "/"):
        out.append(f"{method.lower()} {_ME_PREFIX}{norm[len(_USER_PREFIX):]}")
    return out


@dataclass
class JoinResult:
    doc_by_op: dict[str, DocPage]
    matched: int
    total: int
    ambiguous: int
    via_alias: int = 0

    @property
    def coverage(self) -> float:
        return self.matched / self.total if self.total else 0.0


def build_doc_index(pages: list[DocPage]) -> dict[str, DocPage]:
    """Map join key -> doc page, preferring the most specific page.

    Several pages can claim the same template (a resource overview and the
    dedicated operation page). The operation page carries the better
    description, and it is consistently the one that lists fewer templates,
    so we keep whichever page is least generic.
    """
    index: dict[str, DocPage] = {}
    for page in pages:
        for method, path in page.http_templates:
            key = join_key(method, path)
            incumbent = index.get(key)
            if incumbent is None or len(page.http_templates) < len(incumbent.http_templates):
                index[key] = page
    return index


def join(ops: list[Operation], pages: list[DocPage]) -> JoinResult:
    index = build_doc_index(pages)
    doc_by_op: dict[str, DocPage] = {}
    ambiguous = 0
    via_alias = 0
    for op in ops:
        page = index.get(join_key(op.method, op.path))
        if page is None:
            for alias in alias_keys(op.method, op.path):
                if (page := index.get(alias)) is not None:
                    via_alias += 1
                    break
        if page is not None:
            doc_by_op[op.key] = page
            if len(page.http_templates) > 6:
                ambiguous += 1
    return JoinResult(doc_by_op, len(doc_by_op), len(ops), ambiguous, via_alias)
