# Microsoft Graph MCP

A retrieval-backed MCP server design for Microsoft Graph, plus the working
pipeline that makes it possible.

Graph v1.0 exposes **17,777 operations across 11,493 path templates** — far too
many to present as MCP tools. The architecture is therefore *discover → inspect
→ execute*: a small fixed tool surface, with a retrieval index doing the action
identification.

**Current state:** the corpus pipeline, retrieval index, evaluation harness and
the MCP server are all built. The server runs against a fixture tenant, so it
is fully exercisable without credentials; wiring it to a real tenant is a
single `GraphTransport` implementation — see [docs/SERVER.md](docs/SERVER.md).

## Results

| | |
|---|---|
| Catalog | 17,777 operations → 2,242 executable → 868 indexed |
| Route table | 1,737 routes (250 documented but absent from the OpenAPI) |
| recall@5, Microsoft-authored gold queries | **83.1%** |
| recall@5, combined gold set (124 queries) | **85.5%** |

The ablations are the interesting part — including one that refuted a design
assumption. See [What the ablations showed](docs/ARCHITECTURE.md#what-the-ablations-showed).

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -e ".[local-embeddings,dev]"

# ~65 MB of upstream sources into .cache/
python -m pipeline.fetch --version v1.0

# build the index (~1 min on CPU)
python -m pipeline.build_index --profile end_user_helpdesk --out artifacts/index-v1.0

# try retrieval on its own
python -m pipeline.query "who reports to my manager"

# run the MCP server (fixture tenant, no credentials needed)
GRAPH_MCP_OFFLINE=1 PYTHONPATH=src python -m graph_mcp.server

# measure it
python -m eval.build_gold
python eval/run_retrieval_eval.py --show-misses 10
```

## Layout

```
pipeline/     corpus build: fetch, parse, join, curate, alias, index
src/graph_mcp/
  server.py   the MCP server: seven tools over ~17,800 operations
  runtime.py  index, routes, policy and transport, assembled once
  graph/      transport seam, OData, paging, shaping, error translation
  retrieval/  BM25, embedders, hybrid index
  policy/     route validation, scope globs, write gating
  schema/     CSDL parser for entity types
  fixtures/   the fixture tenant the server runs against by default
config/       scope profiles, domain vocabulary, select defaults, write allowlist
eval/         gold sets and the retrieval harness
docs/         ARCHITECTURE.md, SERVER.md, SCOPE.md
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — design, corpus strategy, measured results
- [Server](docs/SERVER.md) — tools, spec conformance, and how to port the transport
- [Scope and permissions](docs/SCOPE.md) — **read before changing a scope profile**
