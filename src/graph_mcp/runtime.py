"""Server runtime.

Split deliberately in two, because this server is designed to be hosted
remotely and serve many users from one process:

* `Runtime` holds only what is shared and read-only across all callers -- the
  retrieval index, the route table, the CSDL schema, the shaping and write
  policies. Loaded once at startup, never mutated per request.
* Everything caller-specific -- identity, granted scopes, the Graph transport
  bound to that user's token -- is resolved per request from `Caller` and
  never cached on the server.

The one piece of shared mutable state is the cursor store, and every entry in
it is owned by a subject and only returned to that subject.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from graph_mcp.caller import LOCAL_CALLER, Caller
from graph_mcp.graph.paging import CursorStore
from graph_mcp.graph.shaping import Shaper
from graph_mcp.graph.transport import FakeGraphTransport, GraphTransport
from graph_mcp.policy.routes import RouteTable
from graph_mcp.policy.writes import WritePolicy
from graph_mcp.retrieval.index import RetrievalIndex
from graph_mcp.schema.csdl import TypeIndex

DEFAULT_INDEX = Path("artifacts/index-v1.0")
DEFAULT_CONFIG = Path("config")
DEFAULT_METADATA = Path(".cache/metadata-v1.0.xml")

# Given the authenticated caller, produce a transport that acts as that user.
TransportFactory = Callable[[Caller], GraphTransport]


@dataclass
class Runtime:
    index: RetrievalIndex
    routes: RouteTable
    types: TypeIndex
    shaper: Shaper
    writes: WritePolicy
    transport_factory: TransportFactory
    cursors: CursorStore = field(default_factory=CursorStore)
    default_caller: Caller = LOCAL_CALLER
    _embedder: Any = None
    _embedder_lock: Lock = field(default_factory=Lock)

    # ---- shared, read-only lookups -----------------------------------

    @property
    def entries_by_key(self) -> dict[str, dict]:
        if not hasattr(self, "_by_key"):
            self._by_key = {e["key"]: e for e in self.index.entries}
        return self._by_key

    @property
    def entries_by_normalized_key(self) -> dict[str, dict]:
        """Catalog keyed by 'METHOD /lowercased/path', for route-template lookup."""
        if not hasattr(self, "_by_norm_key"):
            self._by_norm_key = {
                f"{e['method'].upper()} {e['path'].lower()}": e for e in self.index.entries
            }
        return self._by_norm_key

    # ---- per-caller --------------------------------------------------

    def transport_for(self, caller: Caller) -> GraphTransport:
        """A Graph transport acting as this caller. Never cached."""
        return self.transport_factory(caller)

    # ---- embeddings --------------------------------------------------

    def warmup(self) -> None:
        """Load the embedding model before serving traffic.

        Without this the first search request pays the model load, and
        concurrent first requests all try to load it at once. A hosted server
        should do this at startup, not on a user's request.
        """
        if self.index.vectors is not None:
            self._get_embedder()

    def _get_embedder(self):
        if self._embedder is None:
            with self._embedder_lock:
                if self._embedder is None:  # re-check under the lock
                    from graph_mcp.retrieval.embedders import get_embedder

                    kind = "local" if self.index.meta.embedder.startswith("local:") else None
                    self._embedder = get_embedder(kind)
        return self._embedder

    def embed_query(self, text: str):
        """Query vector, or None if the index has no dense half."""
        if self.index.vectors is None:
            return None
        return self._get_embedder().encode([text], is_query=True)[0]


def build_runtime(
    *,
    index_dir: Path | None = None,
    config_dir: Path | None = None,
    metadata: Path | None = None,
    transport_factory: TransportFactory | None = None,
) -> Runtime:
    index_dir = index_dir or Path(os.environ.get("GRAPH_MCP_INDEX", DEFAULT_INDEX))
    config_dir = config_dir or Path(os.environ.get("GRAPH_MCP_CONFIG", DEFAULT_CONFIG))
    metadata = metadata or Path(os.environ.get("GRAPH_MCP_METADATA", DEFAULT_METADATA))

    if not index_dir.exists():
        raise SystemExit(
            f"No retrieval index at {index_dir}. Build one first:\n"
            f"  python -m pipeline.fetch --version v1.0\n"
            f"  python -m pipeline.build_index --out {index_dir}"
        )

    routes = RouteTable(json.loads((index_dir / "routes.json").read_text(encoding="utf-8")))

    if transport_factory is None:
        # Default to fixtures. The server is fully exercisable without a tenant,
        # and nothing here can reach a real directory by accident -- wiring
        # HttpGraphTransport is a deliberate act.
        fixture_path = Path(__file__).parent / "fixtures" / "tenant.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

        def transport_factory(caller: Caller) -> GraphTransport:  # noqa: F811
            return FakeGraphTransport(fixture, caller=caller)

    return Runtime(
        index=RetrievalIndex.load(index_dir),
        routes=routes,
        types=TypeIndex(metadata),
        shaper=Shaper.load(config_dir / "select_defaults.yaml"),
        writes=WritePolicy.load(config_dir / "write_allowlist.yaml"),
        transport_factory=transport_factory,
    )
