"""Per-request caller identity.

This server is one process serving many users concurrently, so "the signed-in
user" can never be process state. Caching identity would leak the first
caller's scopes to everyone, and an unowned pagination cursor would let any
caller resume another caller's query.

So identity is resolved per request and threaded explicitly through every call
that touches Graph. Nothing about the caller is ever cached on the server.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Caller:
    """Who this particular request is acting for."""

    subject: str
    scopes: tuple[str, ...] = ()
    token: str | None = field(default=None, repr=False)

    @property
    def is_anonymous(self) -> bool:
        return not self.subject

    def redacted(self) -> dict:
        """Loggable view -- never includes the token."""
        return {"subject": self.subject, "scopes": list(self.scopes)}


# Used only when the server runs with no auth layer configured -- a
# localhost-bound development server over fixtures. Never reachable once
# AuthSettings is set, because unauthenticated requests are rejected first.
LOCAL_CALLER = Caller(subject="local-dev", scopes=())


def current_caller(fallback: Caller = LOCAL_CALLER) -> Caller:
    """The authenticated caller for the request in flight.

    Reads the access token the SDK's auth middleware attached to this request.
    Falls back to `fallback` only when no auth layer is configured at all.
    """
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
    except ImportError:  # pragma: no cover - SDK without auth support
        return fallback

    token = get_access_token()
    if token is None:
        return fallback

    # Prefer a stable directory identifier over the OAuth subject where the
    # identity provider supplies one: Entra's `oid` is immutable for the user
    # in the tenant, whereas `sub` is pairwise per application.
    claims = getattr(token, "claims", None) or {}
    subject = str(claims.get("oid") or token.subject or token.client_id or "")
    return Caller(
        subject=subject,
        scopes=tuple(token.scopes or ()),
        token=token.token,
    )
