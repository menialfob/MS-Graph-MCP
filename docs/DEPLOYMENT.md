# Deployment: remote hosting over Streamable HTTP

This server is designed to be **hosted remotely and shared by many users**.
stdio exists for local development and desktop clients; it is not the target.

That distinction is not cosmetic. A stdio server is one process per user, so
"the signed-in user" can be process state. A hosted server is one process
serving many users concurrently, and the same assumption becomes a data leak.

```bash
python -m graph_mcp.http \
  --host 0.0.0.0 --port 8000 \
  --resource-url https://graph-mcp.example.com \
  --issuer-url https://login.microsoftonline.com/<tenant>/v2.0 \
  --allowed-host graph-mcp.example.com \
  --required-scope Graph.Query
```

## Every request carries its own identity

Nothing about a caller is cached on the server. Each request resolves a
`Caller` (subject, scopes, token) from its verified access token, and that
caller is threaded explicitly through to the Graph transport:

```python
TransportFactory = Callable[[Caller], GraphTransport]
```

`Runtime` holds only what is shared and read-only — the retrieval index, route
table, CSDL schema, shaping and write policy. It is loaded once and never
mutated per request.

Three concrete isolation guarantees, each with a test that fails without it
(`tests/test_multiuser.py`):

| Guarantee | Without it |
|---|---|
| Pagination cursors are owned by a subject | Any caller can resume another caller's query and read their data |
| Identity and scopes resolved per request | The first caller's scopes are reported to everyone |
| Confirm tokens bound to the caller | A write plan approved by one user confirms the same request from another |

A cursor belonging to someone else is reported exactly like one that never
existed — the error must not confirm that a valid cursor is out there.

## Authentication

The server is an OAuth 2.1 **resource server**. It does not issue tokens and
never sees a credential; it validates the token that arrives and acts as that
user against Graph.

Passing `--resource-url` enables this and makes the server publish RFC 9728
protected-resource metadata, so clients can discover where to authenticate.
`validate_token_resource` is on, so a token minted for a different API cannot
be replayed here.

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

Starting with `--resource-url` and no verifier is a hard failure rather than a
permissive fallback: accepting unverified bearer tokens would let any caller
act as any user, which defeats every guarantee above.

`Caller.subject` prefers the `oid` claim over the OAuth `sub`, because Entra's
`sub` is pairwise per application while `oid` is stable for the user in the
tenant — and cursor ownership depends on that stability.

Populate `scopes` from `scp`: it drives `caller_has_scope` in search results and
the missing-scope guidance on a 403.

## Transport security

The MCP specification requires HTTP servers to validate `Origin` to defeat
DNS-rebinding attacks from a browser on the user's machine. Name your public
hostnames explicitly in production:

```bash
--allowed-host graph-mcp.example.com --allowed-origin https://client.example.com
```

The `Host` header carries the port, so `graph-mcp.example.com:443` and
`graph-mcp.example.com` are different strings; the local defaults generate both
forms, but production entries should be exact. A mismatch returns
`421 Misdirected Request`.

Terminate TLS in front of the server.

## Scaling and session state

| Mode | Cursors | Scaling |
|---|---|---|
| Sessioned (default) | Work | Needs sticky sessions, or a shared cursor store |
| `--stateless` | Unavailable | Any replica serves any request |

Cursors live in this process (`CursorStore`), so across replicas a caller can
land on a replica that has never seen their cursor. Options, in order of
preference:

1. **Sticky sessions** on the load balancer, keyed by `Mcp-Session-Id`. Simplest,
   and what the default mode assumes.
2. **Shared cursor store.** `CursorStore` has a deliberately tiny interface
   (`put`/`get`) — back it with Redis and keep the ownership check.
3. **`--stateless`.** Horizontal scaling with no affinity, at the cost of
   pagination; callers must use `$top` and `$filter` instead.

Write confirm tokens are HMAC'd with a per-process key unless
`GRAPH_MCP_CONFIRM_SECRET` is set. **Set it** in any multi-replica deployment,
or a plan issued by one replica will be rejected by another.

The retrieval index is a read-only build artifact, so replicas share it
happily. Ship it in the image or mount it; do not rebuild per replica.

## Startup

`Runtime.warmup()` loads the embedding model before traffic arrives; the HTTP
entry point calls it. Without it the first search request pays a multi-second
model load, and concurrent first requests all try to load it at once.

Set `GRAPH_MCP_OFFLINE=1` so the model loads from the image's cache rather than
reaching the model hub at startup.

Expect ~1–2 GB RSS per replica: the embedding model dominates, the index is
tens of MB.

## Configuration

| Variable | Purpose |
|---|---|
| `GRAPH_MCP_RESOURCE_URL` | Public URL; enables OAuth resource-server mode |
| `GRAPH_MCP_ISSUER_URL` | Identity provider issuer |
| `GRAPH_MCP_TOKEN_VERIFIER` | `module:Class` implementing `TokenVerifier` |
| `GRAPH_MCP_CONFIRM_SECRET` | Shared HMAC key for write confirm tokens |
| `GRAPH_MCP_INDEX` | Retrieval index directory |
| `GRAPH_MCP_CONFIG` | Scope profiles, select defaults, write allowlist |
| `GRAPH_MCP_METADATA` | CSDL for `graph_describe_type` |
| `GRAPH_MCP_OFFLINE` | Load the embedding model from cache only |

## Observability

Each tool result already carries the exact Graph request that was made
(credentials stripped), which is the record that matters for audit. Server-side
logs go to stderr — MCP's logging capability is deprecated as of protocol
`2026-07-28` (SEP-2577).

For tenant-side correlation, have your transport attach a `client-request-id`
header and log it alongside `Caller.subject`; Microsoft Graph activity logs can
then be joined to individual MCP calls.

## Checklist before exposing this

- [ ] `--resource-url` set and a real `GRAPH_MCP_TOKEN_VERIFIER` wired
- [ ] `--allowed-host` / `--allowed-origin` name your real hosts
- [ ] TLS terminated in front
- [ ] `GRAPH_MCP_CONFIRM_SECRET` set if more than one replica
- [ ] Sticky sessions, a shared cursor store, or `--stateless`
- [ ] Delegated scopes on the app registration reviewed (see [SCOPE.md](SCOPE.md))
- [ ] `config/write_allowlist.yaml` reviewed — writes are off unless enabled
