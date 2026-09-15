# Microsoft Graph MCP

A retrieval-backed MCP server for Microsoft Graph, plus the pipeline that builds
its operation catalog.

Graph v1.0 exposes **17,777 operations across 11,493 path templates** — far too
many to present as MCP tools. So the architecture is *discover → inspect →
execute*: a small fixed tool surface, with a retrieval index doing the action
identification.

Served over Streamable HTTP, either as a shared OAuth 2.1 resource server with
every user acting as themselves, or as a single-operator server on localhost
holding one Entra credential.

## Results

| | |
|---|---|
| Catalog | 17,777 operations → 2,242 executable → 868 indexed |
| Route table | 1,737 routes (250 documented but absent from the OpenAPI) |
| recall@5, Microsoft-authored gold queries | **83.1%** |
| recall@5, combined gold set (124 queries) | **85.5%** |

The [ablations](docs/ARCHITECTURE.md#what-the-ablations-showed) are the
interesting part — one of them refuted a design assumption.

## Quick start

```bash
# venv, ~65 MB of upstream sources, and the index
scripts/bootstrap.sh          # --full for the hybrid index the numbers above refer to

# the fixture tenant — no credentials, nothing real is reachable
.venv/bin/python -m graph_mcp.http --port 8000

# or your own tenant: signs you in once, then caches the refresh token
export AZURE_TENANT_ID=... AZURE_CLIENT_ID=... AZURE_CLIENT_SECRET=...
.venv/bin/python -m graph_mcp.http --port 8000

# either way, point a client at it
claude mcp add --transport http graph http://127.0.0.1:8000/mcp
```

Those three `AZURE_` variables do not by themselves say *who the server acts
as*, and the difference decides whether anything works at all —
[SINGLE-USER.md](docs/SINGLE-USER.md) is short and worth reading first.

```bash
# retrieval on its own
.venv/bin/python -m pipeline.query "who reports to my manager"

# measure it
.venv/bin/python -m eval.build_gold && .venv/bin/python eval/run_retrieval_eval.py
```

## Layout

```
pipeline/     corpus build: fetch, parse, join, curate, alias, index
scripts/      bootstrap.sh: clone to running server in one command
src/graph_mcp/
  http.py     entry point: Streamable HTTP, OAuth, transport security
  server.py   the seven tools
  caller.py   per-request identity; nothing about a user is cached
  azure.py    Entra sign-in from AZURE_* for a single-operator server
  runtime.py  shared read-only state + a per-caller transport factory
  graph/      transport seam, OData, paging, shaping, error translation
  retrieval/  BM25, embedders, hybrid index
  policy/     route validation, scope globs, write gating
  schema/     CSDL parser for entity types
  fixtures/   the fixture tenant the server runs against by default
config/       scope profiles, domain vocabulary, select defaults, write
              allowlist, and the (off-by-default) sensitivity-label gate
eval/         gold sets and the retrieval harness
```

## Documentation

- [Running it against your own tenant](docs/SINGLE-USER.md) — **start here to
  use it**: the app registration, the three sign-in flows, connecting a client
- [Deployment](docs/DEPLOYMENT.md) — **start here to port it**: tools, auth,
  the transport seam, isolation, scaling
- [Architecture](docs/ARCHITECTURE.md) — why the design is what it is, and the
  measurements behind it
- [Scope and permissions](docs/SCOPE.md) — read before changing a scope profile
- [Sensitivity labels](docs/SENSITIVITY-LABELS.md) — why Purview labels are
  not enforced by default, and
  [how to enforce them](docs/SENSITIVITY-LABELS-BLOCKING.md) — the optional
  label gate (`config/label_policy.yaml`), what it covers and what it costs
