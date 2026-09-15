"""Parse Microsoft Graph API reference pages into enrichment records.

The OpenAPI description is generated from CSDL, so its prose is mechanical --
``GET /users/{user-id}/messages`` is summarised as "Get messages from users"
and the POST as "Create new navigation property to messages for users". Nobody
phrases a question that way, so retrieval over those strings performs badly.

The human-written docs carry the semantic signal we actually need:

    ---
    title: "List messages"
    description: "Get the messages in the signed-in user's mailbox..."
    ---
    ## HTTP request
    ```http
    GET /me/messages
    GET /users/{id | userPrincipalName}/messages
    ```

One page typically covers several concrete path templates, so a single page
enriches several OpenAPI operations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
_FM_FIELD_RE = re.compile(r"^(?P<key>[a-zA-Z_.]+):\s*(?P<value>.*?)\s*$", re.MULTILINE)
# Paths must allow internal spaces: the docs write alternatives inside braces,
# e.g. "GET /users/{id | userPrincipalName}/messages". A \\S+ pattern silently
# drops every one of those, which is most of the non-/me surface.
_HTTP_LINE_RE = re.compile(
    r"^(?P<method>GET|POST|PATCH|PUT|DELETE)\s+(?P<path>/[^\n]*?)\s*$", re.MULTILINE
)
_PERMISSIONS_INCLUDE_RE = re.compile(r"includes/permissions/(?P<name>[\w.-]+)\.md")
_H1_RE = re.compile(r"^#\s+(?P<title>.+?)\s*$", re.MULTILINE)
_SECTION_RE = re.compile(r"^##\s+(?P<name>.+?)\s*$", re.MULTILINE)

# Markdown / docs-build noise that should not reach the retrieval index.
_NOISE_RE = re.compile(
    r"(\[!INCLUDE[^\]]*\][^\n]*)|(<!--.*?-->)|(\[([^\]]*)\]\([^)]*\))", re.DOTALL
)


@dataclass
class DocPage:
    slug: str
    title: str = ""
    description: str = ""
    intro: str = ""
    http_templates: list[tuple[str, str]] = field(default_factory=list)
    permissions_include: str = ""

    @property
    def doc_url(self) -> str:
        return f"https://learn.microsoft.com/en-us/graph/api/{self.slug}"

    @property
    def text(self) -> str:
        """The human-written blob we index for this page."""
        return " ".join(p for p in (self.title, self.description, self.intro) if p)


def _clean(text: str) -> str:
    text = _NOISE_RE.sub(lambda m: m.group(4) or "", text)
    return re.sub(r"\s+", " ", text).strip()


def _frontmatter(raw: str) -> dict[str, str]:
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return {}
    return {
        fm.group("key"): fm.group("value").strip().strip('"').strip("'")
        for fm in _FM_FIELD_RE.finditer(m.group(1))
    }


def _http_request_section(body: str) -> str:
    """Return only the text under '## HTTP request', up to the next H2.

    Scoping matters: example sections further down contain full request URLs
    that would otherwise be mistaken for canonical path templates.
    """
    sections = list(_SECTION_RE.finditer(body))
    for i, sec in enumerate(sections):
        if sec.group("name").strip().lower().startswith("http request"):
            end = sections[i + 1].start() if i + 1 < len(sections) else len(body)
            return body[sec.end() : end]
    return ""


def _intro(body: str) -> str:
    """First prose paragraph after the H1, before any H2."""
    m = _H1_RE.search(body)
    start = m.end() if m else 0
    end = sections[0].start() if (sections := list(_SECTION_RE.finditer(body[start:]))) else len(body)
    chunk = body[start : start + end]
    for para in chunk.split("\n\n"):
        cleaned = _clean(para)
        if cleaned.startswith("Namespace:"):
            continue
        if len(cleaned) > 40:
            return cleaned
    return ""


def parse_page(path: Path) -> DocPage:
    raw = path.read_text(encoding="utf-8", errors="replace")
    fm = _frontmatter(raw)
    body = _FRONTMATTER_RE.sub("", raw, count=1)

    page = DocPage(slug=path.stem)
    page.title = fm.get("title", "") or (
        m.group("title") if (m := _H1_RE.search(body)) else ""
    )
    page.description = _clean(fm.get("description", ""))
    page.intro = _intro(body)

    seen: set[tuple[str, str]] = set()
    for m in _HTTP_LINE_RE.finditer(_http_request_section(body)):
        entry = (m.group("method"), m.group("path"))
        if entry not in seen:
            seen.add(entry)
            page.http_templates.append(entry)

    if pm := _PERMISSIONS_INCLUDE_RE.search(body):
        page.permissions_include = pm.group("name")

    return page


def parse_all(api_dir: Path) -> list[DocPage]:
    pages = [parse_page(p) for p in sorted(api_dir.glob("*.md"))]
    return [p for p in pages if p.http_templates]
