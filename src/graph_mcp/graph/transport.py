"""The seam between the server's logic and Microsoft Graph.

Everything above this file -- retrieval, policy, shaping, paging, error
translation -- is transport-agnostic and fully testable without a tenant.
A transport only has to answer two questions: who is calling, and what does
Graph say to this request.

Two implementations ship:

* ``FakeGraphTransport`` serves a fixture tenant from memory. It is the default
  so the server runs, and every tool is exercised end to end, with no
  credentials and no network. It models the behaviours the rest of the code has
  to cope with -- paging, 404s, 403s on ungranted scopes, throttling.
* ``HttpGraphTransport`` talks to real Graph. The request building, retry and
  error handling are implemented; obtaining a token is left to a
  ``TokenProvider`` callable, which is the piece each organisation wires to its
  own identity setup.
"""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import parse_qs, urlparse

from graph_mcp.graph.request import GraphRequest, GraphResponse


class Identity(Protocol):
    id: str
    display_name: str
    user_principal_name: str
    scopes: list[str]


class GraphTransport(Protocol):
    """What the server needs from Graph. Implement this to port the server."""

    async def send(self, request: GraphRequest) -> GraphResponse: ...

    async def identity(self) -> dict[str, Any]:
        """Signed-in user plus the scopes the token actually carries."""
        ...


TokenProvider = Callable[[], Awaitable[str]]


# --------------------------------------------------------------------------
# Fixture transport
# --------------------------------------------------------------------------


class FakeGraphTransport:
    """Serves a fixture tenant. No network, no credentials, deterministic.

    Fixture shape (see src/graph_mcp/fixtures/tenant.json):

        {
          "identity": {...},
          "responses": {
            "GET /me/messages": {"value": [...]},      # collection
            "GET /me": {...}                            # single entity
          }
        }

    Collections are paged using $top so that cursor handling is exercised for
    real rather than mocked.
    """

    def __init__(self, fixture: dict[str, Any], *, page_size: int = 10, caller=None):
        self.fixture = fixture
        self.page_size = page_size
        self.caller = caller
        self.sent: list[GraphRequest] = []
        self.fail_next: tuple[int, dict] | None = None

    @classmethod
    def load(cls, path: Path, **kw) -> "FakeGraphTransport":
        return cls(json.loads(path.read_text(encoding="utf-8")), **kw)

    async def identity(self) -> dict[str, Any]:
        """Fixture identity, overlaid with the caller when one is supplied.

        Lets tests exercise several distinct users against one fixture, which
        is how the multi-user isolation tests work.
        """
        identity = dict(self.fixture.get("identity", {}))
        per_caller = self.fixture.get("callers", {}).get(
            getattr(self.caller, "subject", ""), {}
        )
        identity.update(per_caller)
        if self.caller is not None and self.caller.scopes:
            identity["scopes"] = list(self.caller.scopes)
        return identity

    async def send(self, request: GraphRequest) -> GraphResponse:
        self.sent.append(request)

        if self.fail_next is not None:
            status, body = self.fail_next
            self.fail_next = None
            return GraphResponse(status, body, {"Retry-After": "1"})

        granted = set(self.fixture.get("identity", {}).get("scopes", []))
        required = self.fixture.get("scopes_required", {}).get(
            f"{request.method} {request.path}"
        )
        if required and not (set(required) & granted):
            return GraphResponse(403, {"error": {
                "code": "Authorization_RequestDenied",
                "message": "Insufficient privileges to complete the operation.",
            }})

        key = f"{request.method} {request.path}"
        payload = self.fixture.get("responses", {}).get(key)

        # A fixture may pin an explicit status, so error paths are exercisable
        # for any method, not just the happy path.
        if isinstance(payload, dict) and "@status" in payload:
            body = {k: v for k, v in payload.items() if k != "@status"}
            return GraphResponse(int(payload["@status"]), body)

        if request.method in ("POST", "PATCH", "PUT", "DELETE"):
            # Bound actions such as extractSensitivityLabels are POSTs that read
            # rather than write, so a fixture entry wins over the echo response.
            if payload is not None:
                return GraphResponse(200, dict(payload))
            return self._write_response(request)
        if payload is None:
            return GraphResponse(404, {"error": {
                "code": "itemNotFound",
                "message": f"No fixture for {key}.",
            }})

        if isinstance(payload, dict) and isinstance(payload.get("value"), list):
            return self._page(request, payload["value"])
        return GraphResponse(200, dict(payload))

    def _page(self, request: GraphRequest, items: list[dict]) -> GraphResponse:
        size = int(request.query.get("$top", self.page_size))
        skip = int(request.query.get("$skip", 0))
        window = items[skip : skip + size]
        body: dict[str, Any] = {"value": window}
        if request.query.get("$count") == "true":
            body["@odata.count"] = len(items)
        if skip + size < len(items):
            query = dict(request.query)
            query["$skip"] = str(skip + size)
            body["@odata.nextLink"] = GraphRequest(
                request.method, request.path, query, version=request.version
            ).url()
        return GraphResponse(200, body)

    def _write_response(self, request: GraphRequest) -> GraphResponse:
        if request.method == "DELETE":
            return GraphResponse(204, {})
        created = dict(request.body or {})
        created.setdefault("id", f"fixture-{len(self.sent)}")
        return GraphResponse(201 if request.method == "POST" else 200, created)


# --------------------------------------------------------------------------
# HTTP transport
# --------------------------------------------------------------------------


class HttpGraphTransport:
    """Real Graph over HTTPS.

    Deliberately does not acquire tokens. ``token_provider`` is an async
    callable returning a bearer token for the signed-in user; wiring it to MSAL,
    an on-behalf-of exchange, or a gateway is the integration point for each
    deployment. The server never handles credentials itself. ``graph_mcp.azure``
    is one such wiring, for a single-operator deployment.

    ``scopes_provider`` reports what the token actually grants. It is optional
    but worth supplying: it drives ``caller_has_scope`` in search results and
    the missing-scope guidance on a 403, both of which otherwise read "unknown".

    ``app_only`` and ``act_as_user`` exist for a token with no signed-in user.
    Graph rejects every ``/me`` path under such a token, and the catalog is full
    of them, so ``act_as_user`` names the user those paths resolve to and
    ``app_only`` makes the failure legible when nothing does.
    """

    def __init__(
        self,
        token_provider: TokenProvider,
        *,
        base_url: str = "https://graph.microsoft.com",
        max_retries: int = 3,
        timeout: float = 30.0,
        user_agent: str = "graph-mcp/0.1",
        scopes_provider: Callable[[], Awaitable[list[str]]] | None = None,
        act_as_user: str | None = None,
        app_only: bool = False,
        identity_hint: dict[str, Any] | None = None,
    ):
        self.token_provider = token_provider
        self.base_url = base_url
        self.max_retries = max_retries
        self.timeout = timeout
        self.user_agent = user_agent
        self.scopes_provider = scopes_provider
        self.act_as_user = act_as_user
        self.app_only = app_only
        self.identity_hint = identity_hint or {}

    async def identity(self) -> dict[str, Any]:
        scopes = list(await self.scopes_provider()) if self.scopes_provider else []
        identity = {
            "id": self.identity_hint.get("id", ""),
            "display_name": self.identity_hint.get("display_name", ""),
            "user_principal_name": self.identity_hint.get("user_principal_name", ""),
            "scopes": scopes,
        }
        if self.app_only and not self.act_as_user:
            # There is no user to ask about. Whatever the token's own claims
            # said about the application is the whole of the answer.
            return identity

        response = await self.send(GraphRequest("GET", "/me"))
        if not response.ok:
            return identity
        return {
            "id": response.body.get("id", "") or identity["id"],
            "display_name": response.body.get("displayName", "") or identity["display_name"],
            "user_principal_name": (
                response.body.get("userPrincipalName", "")
                or identity["user_principal_name"]
            ),
            # Scopes come from the token's scp claim, which the token provider
            # is better placed to surface than a /me round-trip.
            "scopes": scopes,
        }

    def _resolve_user(self, request: GraphRequest) -> GraphRequest:
        """Point ``/me`` at a real user when the token has no signed-in one.

        The rewrite happens here, below route validation, so the catalog and the
        policy layer keep reasoning about ``/me`` -- the path the model asked
        for -- while the wire carries the path Graph will accept.

        It rewrites the request in place rather than copying it, because the
        caller renders that same object into the result's `request` field, and
        that field is the audit record. Under an app-only token "/me" names
        nobody; the record has to say whose mailbox was read.
        """
        if not self.act_as_user:
            return request
        if request.path != "/me" and not request.path.startswith("/me/"):
            return request
        request.path = f"/users/{self.act_as_user}{request.path[3:]}"
        return request

    async def send(self, request: GraphRequest) -> GraphResponse:
        import httpx

        request = self._resolve_user(request)
        if self.app_only and (request.path == "/me" or request.path.startswith("/me/")):
            # Graph's own answer here is a 400 whose message does not mention
            # that the token is app-only, which sends a model round the same
            # request again. Say what is wrong and what fixes it.
            return GraphResponse(400, {"error": {
                "code": "appOnlyTokenHasNoSignedInUser",
                "message": (
                    f"'{request.path}' needs a signed-in user, but this server "
                    "holds an app-only token. Set GRAPH_MCP_ACT_AS_USER to the "
                    "user these paths should resolve to, or run a delegated "
                    "sign-in flow (GRAPH_MCP_AZURE_FLOW=device_code)."
                ),
            }})

        token = await self.token_provider()
        headers = {
            **request.headers,
            "Authorization": f"Bearer {token}",
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }

        last: GraphResponse | None = None
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(self.max_retries + 1):
                raw = await client.request(
                    request.method,
                    request.url(self.base_url),
                    headers=headers,
                    json=request.body if request.body is not None else None,
                )
                body: dict[str, Any] = {}
                if raw.content:
                    try:
                        body = raw.json()
                    except ValueError:
                        body = {"error": {"code": "nonJsonResponse",
                                          "message": raw.text[:500]}}
                last = GraphResponse(raw.status_code, body, dict(raw.headers))

                # 429 and 5xx are the only retryable statuses; honour
                # Retry-After when Graph sends it, otherwise back off with
                # jitter so parallel callers do not resynchronise.
                if raw.status_code not in (429, 502, 503, 504):
                    return last
                if attempt == self.max_retries:
                    return last
                delay = float(raw.headers.get("Retry-After", 2**attempt))
                await asyncio.sleep(delay + random.uniform(0, 0.5))

        assert last is not None
        return last


def next_link_to_request(next_link: str, version: str = "v1.0") -> GraphRequest:
    """Rebuild a GraphRequest from an @odata.nextLink URL."""
    parsed = urlparse(next_link)
    path = parsed.path
    for prefix in (f"/{version}", "/v1.0", "/beta"):
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    return GraphRequest("GET", path, query, version=version)
