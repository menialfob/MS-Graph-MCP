# The MCP server

Seven tools over ~17,800 Graph operations. Runs against a fixture tenant out of
the box, so everything below can be exercised with no credentials.

```bash
GRAPH_MCP_OFFLINE=1 PYTHONPATH=src python -m graph_mcp.server   # stdio
```

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

Only tools that reach Graph are marked `openWorld`; retrieval and schema
lookups are served from local build artifacts.

The intended flow is search → describe → execute, but `graph_get` accepts a raw
path directly, so a model that already knows `/me/messages` skips the retrieval
round-trip. The path is validated either way.

## Specification conformance

- **Protocol version** negotiated with the client; developed against the
  `mcp` 2.x SDK (latest protocol `2026-07-28`).
- **Tool failures are results, not transport errors.** Every expected failure
  (unknown path, denied write, Graph 4xx) comes back as
  `CallToolResult(isError: true)` with readable text, so the model can see and
  correct it. Only protocol-level faults become JSON-RPC errors.
- **Structured output.** Every tool declares an `outputSchema` derived from its
  return type and returns `structuredContent` alongside text.
- **Annotations** are set per the specification's meaning, not decoratively:
  `destructiveHint` is true only for `graph_write`, `idempotentHint` only where
  repeating a call is genuinely safe.
- **No deprecated capabilities.** The MCP logging capability is deprecated as of
  protocol `2026-07-28` (SEP-2577), so server diagnostics go to stderr instead.
  Nothing is lost: every tool result already carries the exact request that was
  made, which is the more useful record.

## Porting to your own transport

The server touches Graph only through `GraphTransport`
(`src/graph_mcp/graph/transport.py`):

```python
class GraphTransport(Protocol):
    async def send(self, request: GraphRequest) -> GraphResponse: ...
    async def identity(self) -> dict[str, Any]: ...
```

Everything else -- retrieval, route validation, OData construction, shaping,
paging, write gating, error translation -- is transport-agnostic.

`HttpGraphTransport` implements the HTTP side (retry with `Retry-After`,
jittered backoff, JSON error handling) and takes a `token_provider`:

```python
from graph_mcp.graph.transport import HttpGraphTransport
from graph_mcp.runtime import build_runtime
from graph_mcp.server import create_server

async def token_provider() -> str:
    return await your_identity_layer.access_token_for_current_user()

server = create_server(build_runtime(transport=HttpGraphTransport(token_provider)))
server.run()
```

The server never acquires or stores credentials. Delegated / on-behalf-of is
the intended model: every call runs as the signed-in user, so the user's own
privileges remain the boundary. See [SCOPE.md](SCOPE.md).

Populate `identity()["scopes"]` from the token's `scp` claim -- that is what
drives `caller_has_scope` in search results and the missing-scope guidance on a
403.

## Behaviour worth knowing

**Advanced queries are handled for you.** On directory resources, `$search`,
`$count` and operators such as `endsWith` require both
`ConsistencyLevel: eventual` and `$count=true`. Graph's 400 for the missing
header does not mention consistency level, so a model left alone retries the
same broken request forever. `graph_get` detects the condition, adds both, and
says so in the result notes.

**Responses are shaped, not raw.** A default `$select` is applied per entity
type (`config/select_defaults.yaml`) when the caller supplies none; heavy
fields such as message bodies are dropped unless explicitly selected; long
strings and long lists are truncated with a marker. Every one of these is
reported in `notes`, never silent.

**Pagination uses opaque cursors.** `@odata.nextLink` is a long skiptoken URL
that never reaches the model; it is stored server-side under a short id
returned as `cursor`.

**Writes are gated three ways** -- allowlist, dry-run by default, and a confirm
token bound to the exact request. Changing the body between review and
execution invalidates the token, so a benign plan cannot be used to approve a
different call. Writes are disabled entirely unless
`config/write_allowlist.yaml` sets `enabled: true`.

**Unknown paths are refused with suggestions** rather than forwarded to Graph:

```
'/me/mesages' does not match any known Graph operation in this deployment's
catalog. Closest known paths: /me/messages, /me/messages/{}, /me/messages/delta.
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `GRAPH_MCP_INDEX` | `artifacts/index-v1.0` | Retrieval index directory |
| `GRAPH_MCP_CONFIG` | `config` | Scope profiles, select defaults, write allowlist |
| `GRAPH_MCP_METADATA` | `.cache/metadata-v1.0.xml` | CSDL for `graph_describe_type` |
| `GRAPH_MCP_EMBEDDER` | `local` | `local` or `azure` |
| `GRAPH_MCP_OFFLINE` | unset | Load the embedding model from cache only |

## Client configuration

```json
{
  "mcpServers": {
    "microsoft-graph": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "graph_mcp.server"],
      "env": { "PYTHONPATH": "src", "GRAPH_MCP_OFFLINE": "1" }
    }
  }
}
```

## Testing

`tests/test_server.py` drives the server through a real `ClientSession` over
in-memory streams rather than calling tool functions directly, so initialize,
tool listing, schema validation and `isError` semantics are all covered.

```bash
python -m pytest tests/ -q
```
