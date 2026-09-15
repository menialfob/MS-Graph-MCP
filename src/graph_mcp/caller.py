"""Per-request caller identity.

A stdio server is one process per user, so "the signed-in user" can be process
state. A remote HTTP server is one process serving many users concurrently, and
the same assumption becomes a data leak: cached identity leaks the first
caller's scopes to everyone, and an unowned pagination cursor lets any caller
resume another caller's query.

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


# Used when the server runs without an auth layer: local development over
# stdio, or the fixture transport. Never reachable when AuthSettings is
# configured, because the SDK rejects unauthenticated requests first.
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
