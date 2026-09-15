"""Server runtime: the objects every tool needs, assembled once at startup.

Built from configuration and a transport so tests can construct one against
fixtures with no network and no credentials.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


@dataclass
class Runtime:
    index: RetrievalIndex
    routes: RouteTable
    types: TypeIndex
    shaper: Shaper
    writes: WritePolicy
    transport: GraphTransport
    cursors: CursorStore = field(default_factory=CursorStore)
    _embedder: Any = None
    _identity: dict[str, Any] | None = None

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

    def entry_index(self, key: str) -> int | None:
        for i, entry in enumerate(self.index.entries):
            if entry["key"] == key:
                return i
        return None

    def embed_query(self, text: str):
        """Query vector, or None if the index has no dense half.

        The embedder is loaded on first use: it pulls a model into memory, and
        a server whose client only ever calls graph_get should not pay for it.
        """
        if self.index.vectors is None:
            return None
        if self._embedder is None:
            from graph_mcp.retrieval.embedders import get_embedder

            kind = "local" if self.index.meta.embedder.startswith("local:") else None
            self._embedder = get_embedder(kind)
        return self._embedder.encode([text], is_query=True)[0]

    async def identity(self) -> dict[str, Any]:
        if self._identity is None:
            self._identity = await self.transport.identity()
        return self._identity

    async def granted_scopes(self) -> list[str]:
        return list((await self.identity()).get("scopes", []))


def build_runtime(
    *,
    index_dir: Path | None = None,
    config_dir: Path | None = None,
    metadata: Path | None = None,
    transport: GraphTransport | None = None,
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

    routes_path = index_dir / "routes.json"
    routes = RouteTable(json.loads(routes_path.read_text(encoding="utf-8")))

    if transport is None:
        # Default to fixtures: the server is useful and fully exercisable
        # without a tenant, and nothing here should reach a real directory by
        # accident. Wiring HttpGraphTransport is a deliberate act.
        fixture = Path(__file__).parent / "fixtures" / "tenant.json"
        transport = FakeGraphTransport.load(fixture)

    return Runtime(
        index=RetrievalIndex.load(index_dir),
        routes=routes,
        types=TypeIndex(metadata),
        shaper=Shaper.load(config_dir / "select_defaults.yaml"),
        writes=WritePolicy.load(config_dir / "write_allowlist.yaml"),
        transport=transport,
    )
