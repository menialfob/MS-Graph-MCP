"""Write gating.

Three independent controls, because a mistaken write is not recoverable the way
a mistaken read is:

1. **Allowlist.** Only operations named in config/write_allowlist.yaml can be
   written at all, regardless of what the token permits or the catalog offers.
   The scope profile already keeps directory writes out; this is the narrower
   list of writes this deployment actually wants.
2. **Dry run by default.** graph_write returns the exact request it would send
   and stops. Executing takes a second, deliberate call.
3. **Confirmation bound to the request.** The confirm token is a hash of the
   request that was shown. Altering the body, path or method between review and
   execution invalidates it, so a model cannot get a benign request approved and
   then send a different one.

Control 3 is what makes 2 more than a formality.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from graph_mcp.graph.request import GraphRequest
from graph_mcp.policy.globs import match_any

# Per-process secret: confirm tokens are meaningful only within the session
# that issued them, and must not survive a restart.
_SECRET = os.urandom(32)


@dataclass
class WritePolicy:
    enabled: bool
    allow: list[str]
    require_confirmation: bool = True

    @classmethod
    def load(cls, path: Path) -> "WritePolicy":
        if not path.exists():
            return cls(enabled=False, allow=[])
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(
            enabled=bool(raw.get("enabled", False)),
            allow=list(raw.get("allow", [])),
            require_confirmation=bool(raw.get("require_confirmation", True)),
        )

    def permits(self, method: str, template: str) -> tuple[bool, str]:
        """Whether this write may be executed, and why not if it may not.

        Takes the *route template* ("/me/messages/{}"), not the concrete path
        ("/me/messages/AAMkAD..."). Allowlist entries are written against
        templates, where "{}" is a literal placeholder segment rather than a
        wildcard, so matching a concrete id here would silently deny every
        parameterised write. Callers resolve the template via
        RouteTable.match() first.
        """
        if not self.enabled:
            return False, (
                "Writes are disabled for this deployment. Enable them in "
                "config/write_allowlist.yaml (enabled: true) if that is intended."
            )
        entry = f"{method.upper()} {template}"
        for pattern in self.allow:
            allowed_method, _, allowed_path = pattern.partition(" ")
            if allowed_method.upper() != method.upper():
                continue
            if match_any([allowed_path], template):
                return True, ""
        return False, (
            f"'{entry}' is not in the write allowlist. Allowed writes: "
            f"{', '.join(self.allow) or 'none'}."
        )


def confirm_token(request: GraphRequest) -> str:
    """Token bound to this exact request."""
    payload = json.dumps(
        {
            "method": request.method,
            "path": request.path,
            "query": request.query,
            "body": request.body,
            "version": request.version,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(_SECRET, payload, hashlib.sha256).hexdigest()[:32]


def verify(request: GraphRequest, token: str) -> bool:
    return hmac.compare_digest(confirm_token(request), token or "")
