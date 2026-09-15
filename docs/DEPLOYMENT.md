# Running and porting the server

This document is the **shared host**: one process over Streamable HTTP serving
many users, acting as an OAuth 2.1 resource server, each caller acting as
themselves. For the single-operator server — one app registration, one
identity, localhost — see [SINGLE-USER.md](SINGLE-USER.md); it is the shorter
road to a running server and needs no verifier.

```bash
python -m graph_mcp.http \
  --host 0.0.0.0 --port 8000 \
  --resource-url https://graph-mcp.example.com \
  --issuer-url https://login.microsoftonline.com/<tenant>/v2.0 \
  --allowed-host graph-mcp.example.com \
  --required-scope Graph.Query
```

Without `--resource-url` it starts unauthenticated against the fixture tenant,
which is useful for development and must never be exposed beyond localhost.

The two modes are mutually exclusive and the entry point enforces it: with
`AZURE_*` set *and* `--resource-url`, the server would authenticate each caller
and then act as the one identity those credentials hold, so every user would
read the operator's mail. It refuses to start, and the transport factory
refuses any caller but the local one behind that.

## Tools

| Tool | Annotations | Purpose |
|---|---|---|
| `graph_search_operations` | readOnly | Natural-language intent → ranked candidate operations |
| `graph_describe_operation` | readOnly | Parameters, body type, permissions, gotchas |
| `graph_describe_type` | readOnly | Entity properties and enums, from CSDL |
| `graph_get` | readOnly, idempotent, openWorld | Execute a read |
| `graph_next_page` | readOnly, idempotent, openWorld | Continue a paged read |
| `graph_write` | destructive, openWorld | Execute a gated write |
| `graph_whoami` | readOnly, idempotent | Identity, granted scopes, server capabilities |

Only tools that reach Graph are `openWorld`; retrieval and schema lookups are
served from local build artifacts.

The intended flow is search → describe → execute, but `graph_get` accepts a raw
path, so a model that already knows `/me/messages` skips the retrieval
round-trip. The path is validated either way.

## Porting: the transport seam

Everything except this one protocol is transport-agnostic — retrieval, route
validation, OData construction, shaping, paging, write gating, error
translation:

```python
class GraphTransport(Protocol):
    async def send(self, request: GraphRequest) -> GraphResponse: ...
    async def identity(self) -> dict[str, Any]: ...

TransportFactory = Callable[[Caller], GraphTransport]   # what you provide
```

The factory receives the authenticated `Caller` for **that request**. This is
how each user's own token reaches Graph; returning one shared transport would
make every caller act as the same user.

```python
from graph_mcp.graph.transport import HttpGraphTransport
from graph_mcp.runtime import build_runtime

def transport_factory(caller):
    async def token_provider() -> str:
        # Exchange the caller's token for a Graph token (on-behalf-of), or pass
        # it through if the audience is already Graph.
        return await your_identity_layer.graph_token_for(caller.token)

    return HttpGraphTransport(token_provider)

runtime = build_runtime(transport_factory=transport_factory)
```

`HttpGraphTransport` already implements the HTTP side: retry honouring
`Retry-After`, jittered backoff, JSON error handling. It has never run against
a real tenant, so treat its error paths as unverified.

Pass a `scopes_provider` so `identity()["scopes"]` reports the token's `scp`
claim — it drives `caller_has_scope` in search results and the missing-scope
guidance on a 403, both of which otherwise read "unknown".
`graph_mcp.azure.build_transport_factory` is a worked example of the whole
factory, for the single-identity case.

## Authentication

The server validates tokens; it never issues them or handles a credential.

`--resource-url` enables resource-server mode and publishes RFC 9728
protected-resource metadata so clients can discover where to authenticate.
`validate_token_resource` is on, so a token minted for a different API cannot be
replayed here.

**You must supply a token verifier.** There is deliberately no default:

```bash
export GRAPH_MCP_TOKEN_VERIFIER=yourorg.auth:EntraTokenVerifier
```

```python
class EntraTokenVerifier:                       # implements mcp TokenVerifier
    async def verify_token(self, token: str) -> AccessToken | None:
        claims = await validate_jwt(token)      # signature, issuer, audience, expiry
        if claims is None:
            return None
        return AccessToken(
            token=token,
            client_id=claims["appid"],
            subject=claims["oid"],              # immutable per user per tenant
            scopes=claims.get("scp", "").split(),
            claims=claims,
        )
```

Starting with `--resource-url` and no verifier is a hard failure, not a
permissive fallback: accepting unverified bearer tokens would let any caller act
as any user.

`Caller.subject` prefers `oid` over the OAuth `sub`, because Entra's `sub` is
pairwise per application while `oid` is stable for the user in the tenant — and
cursor ownership depends on that stability.

## Multi-user isolation

One process serves every user, so nothing about a caller is cached. Each
request resolves a `Caller` (subject, scopes, token) from its verified token and
threads it through to the transport. `Runtime` holds only shared read-only
state: the retrieval index, route table, CSDL schema and policies.

Three guarantees, each with a test that fails without it
(`tests/test_multiuser.py`):

| Guarantee | Without it |
|---|---|
| Cursors owned by a subject | Any caller resumes another caller's query and reads their data |
| Identity and scopes per request | The first caller's scopes are reported to everyone |
| Confirm tokens bound to the caller | A plan approved by one user confirms the same request from another |

A cursor belonging to someone else is reported exactly like one that never
existed — the error must not confirm a valid cursor is out there.

## Transport security

The specification requires HTTP servers to validate `Origin` against
DNS-rebinding attacks. Name your real hosts:

```bash
--allowed-host graph-mcp.example.com --allowed-origin https://client.example.com
```

The `Host` header carries the port, so `example.com:443` and `example.com` are
different strings. A mismatch returns `421 Misdirected Request`. Terminate TLS
in front of the server.

## Scaling and session state

| Mode | Cursors | Scaling |
|---|---|---|
| Sessioned (default) | Work | Needs sticky sessions, or a shared cursor store |
| `--stateless` | Unavailable | Any replica serves any request |

Cursors live in-process, so across replicas a caller can land on one that has
never seen their cursor. In order of preference:

1. **Sticky sessions** keyed by `Mcp-Session-Id`. What the default assumes.
2. **Shared cursor store.** `CursorStore` is two methods (`put`/`get`) — back it
   with Redis and keep the ownership check.
3. **`--stateless`.** No affinity, at the cost of pagination; callers use
   `$top` and `$filter` instead.

Set `GRAPH_MCP_CONFIRM_SECRET` in any multi-replica deployment, or a write plan
issued by one replica is rejected by another.

The retrieval index is a read-only build artifact; ship it in the image or
mount it, and do not rebuild per replica. `Runtime.warmup()` loads the embedding
model at startup so the first user does not pay for it. Expect ~1–2 GB RSS per
replica, dominated by that model.

## Configuration

Every flag has an environment equivalent, so nothing needs a command line in a
container.

| Variable | Purpose |
|---|---|
| `GRAPH_MCP_HOST` / `GRAPH_MCP_PORT` | Bind address (`--host` / `--port`) |
| `GRAPH_MCP_RESOURCE_URL` | Public URL; enables OAuth resource-server mode |
| `GRAPH_MCP_ISSUER_URL` | Identity provider issuer |
| `GRAPH_MCP_TOKEN_VERIFIER` | `module:Class` implementing `TokenVerifier` |
| `GRAPH_MCP_CONFIRM_SECRET` | Shared HMAC key for write confirm tokens |
| `GRAPH_MCP_INDEX` | Retrieval index directory |
| `GRAPH_MCP_CONFIG` | Scope profiles, select defaults, write allowlist |
| `GRAPH_MCP_METADATA` | CSDL for `graph_describe_type` |
| `GRAPH_MCP_EMBEDDER` | `local`, `azure`, or `none` for a lexical-only build |
| `GRAPH_MCP_OFFLINE` | Load the embedding model from cache only |
| `GRAPH_MCP_GRAPH` | `auto`, `azure` or `fixture` — what to talk to |

Single-operator mode adds `AZURE_*` and a few more; they are tabulated in
[SINGLE-USER.md](SINGLE-USER.md#configuration).

## Behaviour worth knowing

**Advanced queries are handled for you.** On directory resources, `$search`,
`$count` and operators such as `endsWith` require both
`ConsistencyLevel: eventual` and `$count=true`. Graph's 400 does not mention
consistency level, so a model left alone retries the same broken request
forever. `graph_get` detects the condition, adds both, and says so in `notes`.

**Responses are shaped.** A default `$select` per entity type
(`config/select_defaults.yaml`) when the caller gives none; heavy fields such as
message bodies dropped unless selected; long strings and lists truncated. Every
one is reported in `notes`, never silently.

**Pagination uses opaque cursors.** `@odata.nextLink` is a long skiptoken URL
that never reaches the model.

**Writes are gated three ways** — allowlist, dry-run by default, and a confirm
token bound to the exact request and caller. Disabled entirely unless
`config/write_allowlist.yaml` sets `enabled: true`.

**Unknown paths are refused with suggestions** rather than forwarded to Graph:

```
'/me/mesages' does not match any known Graph operation in this deployment's
catalog. Closest known paths: /me/messages, /me/messages/{}, /me/messages/delta.
```

## Specification conformance

Built against the `mcp` 2.x SDK (latest protocol `2026-07-28`), version
negotiated with the client.

- **Tool failures are results, not transport errors.** Every expected failure
  returns `CallToolResult(isError: true)` with readable text, so the model can
  correct itself. Only protocol faults become JSON-RPC errors.
- **Structured output.** Every tool declares an `outputSchema` and returns
  `structuredContent` alongside text.
- **Annotations carry their real meaning**: `destructiveHint` only on
  `graph_write`, `idempotentHint` only where repeating a call is safe.
- **No deprecated capabilities.** MCP logging is deprecated as of `2026-07-28`
  (SEP-2577), so diagnostics go to stderr. Each result already carries the exact
  Graph request made, credentials stripped — the record that matters for audit.
  For tenant-side correlation, attach a `client-request-id` header in your
  transport and log it with `Caller.subject`.

## Before exposing this

- [ ] `--resource-url` set and a real `GRAPH_MCP_TOKEN_VERIFIER` wired
- [ ] `--allowed-host` / `--allowed-origin` name your real hosts
- [ ] TLS terminated in front
- [ ] `GRAPH_MCP_CONFIRM_SECRET` set if more than one replica
- [ ] Sticky sessions, a shared cursor store, or `--stateless`
- [ ] Delegated scopes on the app registration reviewed ([SCOPE.md](SCOPE.md))
- [ ] `config/write_allowlist.yaml` reviewed — writes are off unless enabled

## Testing

`tests/test_server.py` drives the server through a real `ClientSession` over
in-memory streams, so initialize, tool listing, schema validation and `isError`
semantics are covered. `tests/test_multiuser.py` covers isolation, and
`tests/test_azure.py` covers the sign-in flows against a stubbed token endpoint
— including the failure messages, which is most of what that module is for.

```bash
python -m pytest tests/ -q
```
