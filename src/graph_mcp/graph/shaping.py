"""Response shaping: keep answers useful without flooding the context window.

A single Graph ``user`` carries 50+ properties; a ``message`` includes the full
HTML body; a page of 25 messages is comfortably over 100k tokens of mostly
markup. Returning that raw is the difference between a tool that works and one
that blows the window on its first call.

Three mechanisms, in order of preference:

1. Ask Graph for less -- inject a sensible ``$select`` when the caller gave
   none. Cheapest, because the data never crosses the wire.
2. Drop known-heavy fields the caller did not ask for (message bodies).
3. Truncate what is left, with an explicit marker so the model can tell that
   truncation happened rather than inferring the value was short.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Fields that are large and rarely what was asked for. Only dropped when the
# caller did not explicitly $select them.
_HEAVY_FIELDS = ("body", "uniqueBody", "content", "contentBytes", "@odata.context")

_MAX_STRING = 2000
_MAX_ITEMS = 50


@dataclass
class ShapingResult:
    body: dict[str, Any]
    notes: list[str] = field(default_factory=list)


class Shaper:
    def __init__(self, defaults: dict[str, list[str]]):
        self.defaults = {k.lower(): v for k, v in defaults.items()}

    @classmethod
    def load(cls, path: Path) -> "Shaper":
        if not path.exists():
            return cls({})
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")) or {})

    def default_select(self, response_type: str) -> list[str] | None:
        """Default $select for an entity type, if one is configured."""
        if not response_type:
            return None
        leaf = response_type.rsplit(".", 1)[-1].lower()
        return self.defaults.get(leaf)

    def shape(
        self,
        body: dict[str, Any],
        *,
        selected: list[str] | None = None,
        max_items: int = _MAX_ITEMS,
        max_string: int = _MAX_STRING,
    ) -> ShapingResult:
        notes: list[str] = []
        keep = {s.lower() for s in (selected or [])}

        def clean(value: Any, depth: int = 0) -> Any:
            if isinstance(value, str) and len(value) > max_string:
                notes.append(f"truncated a string field at {max_string} characters")
                return value[:max_string] + "…[truncated]"
            if isinstance(value, list):
                if len(value) > max_items:
                    notes.append(
                        f"showing {max_items} of {len(value)} items; "
                        "use $top or the cursor for more"
                    )
                    value = value[:max_items]
                return [clean(v, depth + 1) for v in value]
            if isinstance(value, dict):
                out = {}
                for key, inner in value.items():
                    if key in _HEAVY_FIELDS and key.lower() not in keep:
                        notes.append(f"omitted heavy field '{key}'; $select it to include")
                        continue
                    out[key] = clean(inner, depth + 1)
                return out
            return value

        shaped = clean(body)
        # Dedupe notes but keep order -- the same note fires per item otherwise.
        seen: set[str] = set()
        unique = [n for n in notes if not (n in seen or seen.add(n))]
        return ShapingResult(shaped, unique)
