"""Parse the generated permission tables shipped with the API reference.

The OpenAPI description carries no security or scope information at all (there
are no ``securitySchemes``), so required permissions can only come from these
generated includes:

    |Permission type|Least privileged permissions|Higher privileged permissions|
    |:---|:---|:---|
    |Delegated (work or school account)|Mail.ReadBasic|Mail.ReadWrite, Mail.Read|
    |Delegated (personal Microsoft account)|Mail.ReadBasic|Mail.ReadWrite, Mail.Read|
    |Application|Mail.ReadBasic.All|Mail.ReadWrite, Mail.Read|

Least-privileged delegated scopes drive both the "is this end-user facing?"
curation rule and the runtime check against the caller's granted scopes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

_ROW_TYPES = {
    "delegated (work or school account)": "delegated_work",
    "delegated (personal microsoft account)": "delegated_personal",
    "application": "application",
}
_NONE = {"not supported.", "not supported", "none.", "none", ""}


@dataclass
class PermissionSet:
    least: dict[str, list[str]] = field(default_factory=dict)
    higher: dict[str, list[str]] = field(default_factory=dict)

    @property
    def delegated_work_least(self) -> list[str]:
        return self.least.get("delegated_work", [])

    @property
    def supports_delegated(self) -> bool:
        return bool(self.delegated_work_least or self.least.get("delegated_personal"))

    def to_dict(self) -> dict:
        return {"least": self.least, "higher": self.higher}


def _scopes(cell: str) -> list[str]:
    cell = cell.strip()
    if cell.lower() in _NONE:
        return []
    out = []
    for part in cell.split(","):
        scope = part.strip().strip("`*_ ").rstrip(".")
        # Rows occasionally read "Not supported." for one column only.
        if scope and scope.lower() not in _NONE and " " not in scope:
            out.append(scope)
    return out


def parse_file(path: Path) -> PermissionSet:
    result = PermissionSet()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        kind = _ROW_TYPES.get(cells[0].lower())
        if not kind:
            continue
        if least := _scopes(cells[1]):
            result.least[kind] = least
        if len(cells) > 2 and (higher := _scopes(cells[2])):
            result.higher[kind] = higher
    return result


def parse_all(perm_dir: Path) -> dict[str, PermissionSet]:
    return {
        p.stem: parsed
        for p in sorted(perm_dir.glob("*.md"))
        if (parsed := parse_file(p)).least
    }
