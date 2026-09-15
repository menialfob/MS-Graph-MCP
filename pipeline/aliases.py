"""Domain vocabulary layer.

The docs are written in API vocabulary and questions arrive in user vocabulary.
Measured on the gold set, every retrieval miss was a gap between the two --
"send an email" against an operation whose text only ever says "sendMail",
"what meetings do I have" against "calendarView". No amount of reranking fixes
a word that appears nowhere in the index.

This maps Graph concepts to the words people actually use, and indexes them as
one extra text per operation.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from pipeline.catalog import CatalogEntry


class AliasTable:
    def __init__(self, mapping: dict[str, list[str]]):
        self.mapping = {k.lower(): v for k, v in mapping.items()}

    @classmethod
    def load(cls, path: Path) -> "AliasTable":
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")) or {})

    def terms_for(self, entry: CatalogEntry) -> list[str]:
        """Alias phrases for an operation, matched on its type and its path."""
        candidates: list[str] = []
        for ref in (entry.response_type, entry.request_type):
            if ref:
                candidates.append(ref.rsplit(".", 1)[-1])
        candidates += [
            seg for seg in entry.path.strip("/").split("/") if not seg.startswith("{")
        ]

        out: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            for phrase in self.mapping.get(candidate.lower(), []):
                if phrase not in seen:
                    seen.add(phrase)
                    out.append(phrase)
        return out
