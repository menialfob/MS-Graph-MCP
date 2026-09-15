"""The retrieval index: a portable build artifact, not a running service.

At this catalog size (hundreds of operations, a few thousand indexed texts) the
vectors are a single numpy array. Shipping the index as a file next to the
server keeps the server stateless, makes deployments atomic, and removes an
external dependency that would otherwise need its own availability story. A
managed vector database would be all cost and no benefit here; if the catalog
ever grew by two orders of magnitude, the Searcher interface is where that
swap would happen.

Each operation contributes several texts (canonical description, generated
utterances). Retrieval scores texts, then collapses to best-scoring operation,
so an operation matched by any of its phrasings surfaces once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from graph_mcp.retrieval.bm25 import BM25


@dataclass
class IndexMeta:
    profile: str
    graph_version: str
    embedder: str
    paraphraser: str
    built_at: str
    n_operations: int
    n_texts: int
    join_coverage: float
    ablations: list[str]


class RetrievalIndex:
    """Hybrid lexical + dense retrieval over the operation catalog."""

    def __init__(
        self,
        entries: list[dict],
        texts: list[str],
        owners: list[int],
        vectors: np.ndarray | None,
        meta: IndexMeta,
    ):
        self.entries = entries
        self.texts = texts
        self.owners = owners  # text id -> entry id
        self.vectors = vectors
        self.meta = meta
        self.bm25 = BM25(texts)

    # ---- persistence -------------------------------------------------

    def save(self, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "catalog.json").write_text(
            json.dumps(self.entries, indent=1), encoding="utf-8"
        )
        (out_dir / "texts.json").write_text(
            json.dumps({"texts": self.texts, "owners": self.owners}), encoding="utf-8"
        )
        (out_dir / "meta.json").write_text(
            json.dumps(self.meta.__dict__, indent=1), encoding="utf-8"
        )
        if self.vectors is not None:
            np.save(out_dir / "vectors.npy", self.vectors)

    @classmethod
    def load(cls, in_dir: Path) -> "RetrievalIndex":
        entries = json.loads((in_dir / "catalog.json").read_text(encoding="utf-8"))
        blob = json.loads((in_dir / "texts.json").read_text(encoding="utf-8"))
        meta = IndexMeta(**json.loads((in_dir / "meta.json").read_text(encoding="utf-8")))
        vec_path = in_dir / "vectors.npy"
        vectors = np.load(vec_path) if vec_path.exists() else None
        return cls(entries, blob["texts"], blob["owners"], vectors, meta)

    # ---- retrieval ---------------------------------------------------

    def search(
        self,
        query: str,
        top_k: int = 8,
        query_vector: np.ndarray | None = None,
        mode: str = "hybrid",
        rrf_k: int = 60,
    ) -> list[tuple[int, float]]:
        """Return (entry_id, score), best first.

        Lexical and dense rankings are combined with Reciprocal Rank Fusion
        rather than by mixing raw scores: BM25 scores are unbounded and cosine
        similarities sit in a narrow band, so any fixed weighting between them
        is arbitrary and drifts as the corpus changes. RRF only needs the
        rankings, which is the part both backends agree on.
        """
        pool = max(top_k * 6, 50)
        rankings: list[list[int]] = []

        if mode in ("hybrid", "lexical"):
            lex = self.bm25.search(query, top_k=pool)
            rankings.append(self._collapse([t for t, _ in lex]))

        if mode in ("hybrid", "dense") and self.vectors is not None:
            if query_vector is None:
                raise ValueError("dense retrieval needs a query vector")
            sims = self.vectors @ query_vector
            order = np.argsort(-sims)[:pool]
            rankings.append(self._collapse([int(i) for i in order]))

        if not rankings:
            return []

        fused: dict[int, float] = {}
        for ranking in rankings:
            for rank, entry_id in enumerate(ranking):
                fused[entry_id] = fused.get(entry_id, 0.0) + 1.0 / (rrf_k + rank + 1)
        return sorted(fused.items(), key=lambda kv: -kv[1])[:top_k]

    def _collapse(self, text_ids: list[int]) -> list[int]:
        """Text ranking -> operation ranking, keeping each operation's best rank."""
        seen: set[int] = set()
        out: list[int] = []
        for text_id in text_ids:
            entry_id = self.owners[text_id]
            if entry_id not in seen:
                seen.add(entry_id)
                out.append(entry_id)
        return out
