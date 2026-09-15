"""Route validation: does this concrete path correspond to a known operation?

The model constructs concrete paths ("/me/messages/AAMkAD.../attachments") from
templates it saw in search results. This maps that back to a template and
refuses anything that matches none, so a hallucinated endpoint is rejected here
rather than becoming a puzzling 400 from Graph.

Matching is a segment trie: literal segments win over the ``{}`` wildcard, so
``/me/drive/root`` prefers a literal ``root`` route over ``/me/drive/{}`` when
both exist.

This is a correctness and blast-radius control, not a security boundary -- what
a caller may actually do is bounded by the delegated scopes on the token. See
docs/SCOPE.md.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

# Segments that are always parameter values rather than literals.
_PARAM_RE = re.compile(r"^\{.*\}$")
# OData function-call syntax: keep the name, drop the arguments.
_CALL_RE = re.compile(r"\([^)]*\)")


@dataclass
class RouteMatch:
    template: str
    methods: list[str]
    path_params: list[str]


class RouteTable:
    """Trie over normalised path templates."""

    def __init__(self, table: dict[str, list[str]]):
        self.table = table
        self._trie: dict = {}
        for template, methods in table.items():
            node = self._trie
            for segment in self._segments(template):
                node = node.setdefault(segment, {})
            node["$"] = (template, methods)

    @staticmethod
    def _segments(path: str) -> list[str]:
        return [s for s in path.strip("/").split("/") if s]

    @staticmethod
    def normalize_segment(segment: str) -> str:
        """Concrete segment -> the form the trie stores."""
        stripped = _CALL_RE.sub("", segment)
        return stripped.lower()

    def match(self, path: str) -> RouteMatch | None:
        segments = self._segments(path.split("?", 1)[0])
        found = self._walk(self._trie, segments, [])
        if found is None:
            return None
        (template, methods), params = found
        return RouteMatch(template, list(methods), params)

    def _walk(self, node: dict, segments: list[str], params: list[str]):
        if not segments:
            leaf = node.get("$")
            return (leaf, params) if leaf else None

        head, rest = segments[0], segments[1:]
        normalized = self.normalize_segment(head)

        # Literal match first: it is the more specific route.
        if normalized in node and normalized != "{}":
            if (found := self._walk(node[normalized], rest, params)) is not None:
                return found
        # Then the parameter wildcard.
        if "{}" in node:
            if (found := self._walk(node["{}"], rest, [*params, head])) is not None:
                return found
        return None

    def allows(self, method: str, path: str) -> tuple[bool, RouteMatch | None]:
        match = self.match(path)
        if match is None:
            return False, None
        return method.upper() in match.methods, match

    def suggest(self, path: str, limit: int = 3) -> list[str]:
        """Nearest known templates, for an error message worth reading.

        Ranked by shared leading segments first (a wrong head means the caller
        is in the wrong place entirely), then by string similarity to break
        ties. Similarity alone would rank unrelated routes highly; prefix depth
        alone returns whichever route happened to be inserted first, which is
        how "/me/mesages" ended up suggesting "/me/wipeManagedAppRegistrations".
        """
        wanted = "/" + "/".join(self.normalize_segment(s) for s in self._segments(path))
        segments = self._segments(wanted)

        scored: list[tuple[int, float, str]] = []
        for template in self.table:
            candidate = self._segments(template)
            shared = 0
            for a, b in zip(segments, candidate):
                if a == b or b == "{}":
                    shared += 1
                else:
                    break
            ratio = difflib.SequenceMatcher(None, wanted, template).ratio()
            scored.append((shared, ratio, template))

        # Keep only plausible candidates: either a real shared prefix or a
        # close spelling. Everything else is noise in an error message.
        scored = [s for s in scored if s[0] > 0 or s[1] > 0.6]
        scored.sort(key=lambda s: (-s[0], -s[1]))
        return [template for _, _, template in scored[:limit]]
