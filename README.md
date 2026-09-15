# Microsoft Graph MCP

A retrieval-backed MCP server for Microsoft Graph, plus the pipeline that builds
its operation catalog.

Graph v1.0 exposes **17,777 operations across 11,493 path templates** — far too
many to present as MCP tools. So the architecture is *discover → inspect →
execute*: a small fixed tool surface, with a retrieval index doing the action
identification.

Hosted remotely over Streamable HTTP as an OAuth 2.1 resource server, shared by
many users, each acting as themselves.

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
python -m venv .venv && .venv/bin/pip install -e ".[local-embeddings,dev]"

# ~65 MB of upstream sources into .cache/
python -m pipeline.fetch --version v1.0

# build the index (~1 min on CPU)
python -m pipeline.build_index --profile end_user_helpdesk --out artifacts/index-v1.0

# run the server against the fixture tenant — no credentials needed
GRAPH_MCP_OFFLINE=1 PYTHONPATH=src python -m graph_mcp.http --port 8000

# retrieval on its own
python -m pipeline.query "who reports to my manager"

# measure it
python -m eval.build_gold && python eval/run_retrieval_eval.py
```

## Layout

```
pipeline/     corpus build: fetch, parse, join, curate, alias, index
src/graph_mcp/
  http.py     entry point: Streamable HTTP, OAuth, transport security
  server.py   the seven tools
  caller.py   per-request identity; nothing about a user is cached
  runtime.py  shared read-only state + a per-caller transport factory
  graph/      transport seam, OData, paging, shaping, error translation
  retrieval/  BM25, embedders, hybrid index
  policy/     route validation, scope globs, write gating
  schema/     CSDL parser for entity types
  fixtures/   the fixture tenant the server runs against by default
config/       scope profiles, domain vocabulary, select defaults, write allowlist
eval/         gold sets and the retrieval harness
```

## Documentation

- [Deployment](docs/DEPLOYMENT.md) — **start here to port it**: tools, auth,
  the transport seam, isolation, scaling
- [Architecture](docs/ARCHITECTURE.md) — why the design is what it is, and the
  measurements behind it
- [Scope and permissions](docs/SCOPE.md) — read before changing a scope profile
- [Sensitivity labels](docs/SENSITIVITY-LABELS.md) — why Purview labels are
  not enforced today, and
  [how they could be](docs/SENSITIVITY-LABELS-BLOCKING.md) — the three
  mechanisms Graph v1.0 offers, measured
