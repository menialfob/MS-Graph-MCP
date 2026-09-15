"""The request model passed between the tools, the policy layer and transport.

Kept deliberately transport-agnostic: a GraphRequest is a plain description of
a Graph call, so policy can inspect and reject it, a dry run can show it, and
tests can assert on it without any HTTP involved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode


@dataclass
class GraphRequest:
    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] | None = None
    version: str = "v1.0"

    def url(self, base: str = "https://graph.microsoft.com") -> str:
        # Path segments are already OData-shaped (they may legitimately contain
        # $, (), ' and :), so only encode what would break the URL.
        path = quote(self.path, safe="/${}()',:@-_.~")
        # Keep $ and , literal and encode spaces as %20 rather than '+'. Graph
        # accepts the fully-escaped form too, but "$select=id,displayName" is
        # what appears in the docs and in audit logs, and a dry run the
        # reviewer cannot read defeats the point of having one.
        qs = (
            "?" + urlencode(self.query, safe="$,'()", quote_via=quote)
            if self.query
            else ""
        )
        return f"{base}/{self.version}{path}{qs}"

    def describe(self) -> dict[str, Any]:
        """Auditable, credential-free rendering of the call."""
        return {
            "method": self.method,
            "url": self.url(),
            "headers": {k: v for k, v in self.headers.items() if k.lower() != "authorization"},
            "body": self.body,
        }


@dataclass
class GraphResponse:
    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def next_link(self) -> str | None:
        link = self.body.get("@odata.nextLink")
        return link if isinstance(link, str) else None

    @property
    def items(self) -> list[dict[str, Any]] | None:
        value = self.body.get("value")
        return value if isinstance(value, list) else None
