#!/usr/bin/env bash
#
# From a fresh clone to a server an MCP client can call.
#
#   scripts/bootstrap.sh            lexical index (BM25): seconds, no torch
#   scripts/bootstrap.sh --full     hybrid index: ~1 min, installs torch
#
# The index is a build artifact, not a runtime dependency on anything: once it
# exists the server starts offline. The default lexical build measures 75.8%
# recall@5 against the hybrid build's 85.5%, so it is the fast way to get
# something running and --full is what to deploy.
set -euo pipefail

cd "$(dirname "$0")/.."

MODE=lexical
[[ "${1:-}" == "--full" ]] && MODE=full

PYTHON=${PYTHON:-python3}
VENV=${VENV:-.venv}
INDEX=${GRAPH_MCP_INDEX:-artifacts/index-v1.0}

if [[ ! -d "$VENV" ]]; then
  echo "==> creating $VENV"
  "$PYTHON" -m venv "$VENV"
fi
PIP="$VENV/bin/pip"
PY="$VENV/bin/python"

echo "==> installing (mode: $MODE)"
"$PIP" install --quiet --upgrade pip
if [[ "$MODE" == full ]]; then
  "$PIP" install --quiet -e ".[local-embeddings,dev]"
else
  "$PIP" install --quiet -e ".[dev]"
fi

echo "==> fetching upstream Graph sources into .cache/ (~65 MB, cached)"
"$PY" -m pipeline.fetch --version v1.0

if [[ -d "$INDEX" ]]; then
  echo "==> index already at $INDEX (delete it to rebuild)"
else
  echo "==> building the retrieval index"
  if [[ "$MODE" == full ]]; then
    "$PY" -m pipeline.build_index --profile end_user_helpdesk --out "$INDEX"
  else
    "$PY" -m pipeline.build_index --profile end_user_helpdesk \
      --embedder none --out "$INDEX"
  fi
fi

cat <<'EOF'

Done. Two ways to run it:

  # fixture tenant -- no credentials, nothing real is reachable
  .venv/bin/python -m graph_mcp.http --port 8000

  # your tenant -- signs you in once, then caches the refresh token
  export AZURE_TENANT_ID=... AZURE_CLIENT_ID=... AZURE_CLIENT_SECRET=...
  .venv/bin/python -m graph_mcp.http --port 8000

Then point a client at it:

  claude mcp add --transport http graph http://127.0.0.1:8000/mcp

docs/SINGLE-USER.md covers the app registration, and what the three
AZURE_ variables can and cannot express.
EOF
