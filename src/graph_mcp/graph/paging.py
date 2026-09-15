"""Opaque pagination cursors.

Graph returns an ``@odata.nextLink`` that is a full URL carrying an opaque
``$skiptoken`` -- often several hundred characters of base64. Handing that back
to the model costs context on every page and invites it to try to parse or
mutate the token, which Graph documentation explicitly warns against.

Instead the link is kept server-side under a short id. The model sees
``cursor: "c_4f3a1b"`` and passes it to graph_next_page.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass


@dataclass
class _Entry:
    next_link: str
    created_at: float
    operation: str
    page: int


class CursorStore:
    """In-memory, TTL'd cursor store.

    Per-process and non-persistent by design: a cursor is only meaningful
    within a conversation, and Graph skiptokens expire server-side anyway. A
    multi-replica deployment that needs sticky paging should swap this for a
    shared store -- the interface is deliberately tiny.
    """

    def __init__(self, ttl_seconds: float = 900.0, max_entries: int = 1000):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._entries: dict[str, _Entry] = {}

    def put(self, next_link: str, operation: str, page: int) -> str:
        self._evict()
        token = "c_" + secrets.token_hex(4)
        self._entries[token] = _Entry(next_link, time.monotonic(), operation, page)
        return token

    def get(self, token: str) -> _Entry | None:
        entry = self._entries.get(token)
        if entry is None:
            return None
        if time.monotonic() - entry.created_at > self.ttl:
            del self._entries[token]
            return None
        return entry

    def _evict(self) -> None:
        now = time.monotonic()
        expired = [k for k, v in self._entries.items() if now - v.created_at > self.ttl]
        for key in expired:
            del self._entries[key]
        # Hard cap as a backstop against a long-lived session accumulating
        # cursors faster than they expire.
        if len(self._entries) >= self.max_entries:
            oldest = sorted(self._entries.items(), key=lambda kv: kv[1].created_at)
            for key, _ in oldest[: len(self._entries) - self.max_entries + 1]:
                del self._entries[key]
