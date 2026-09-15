"""Ad-hoc retrieval check against a built index.

    python -m pipeline.query "who reports to my manager"

This is the shape of what graph_search_operations will return to the calling
model: enough to disambiguate between candidates, not enough to flood context.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from graph_mcp.retrieval.index import RetrievalIndex


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("query", nargs="+")
    ap.add_argument("--index", type=Path, default=Path("artifacts/index-v1.0"))
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--mode", default="hybrid", choices=["hybrid", "lexical", "dense"])
    args = ap.parse_args()

    query = " ".join(args.query)
    index = RetrievalIndex.load(args.index)

    query_vector = None
    if args.mode in ("hybrid", "dense"):
        from graph_mcp.retrieval.embedders import get_embedder

        query_vector = get_embedder().encode([query], is_query=True)[0]

    results = index.search(query, top_k=args.top_k, query_vector=query_vector, mode=args.mode)
    print(f'"{query}"\n')
    for rank, (entry_id, score) in enumerate(results, 1):
        entry = index.entries[entry_id]
        scopes = entry["permissions"].get("least", {}).get("delegated_work", [])
        print(f"{rank}. {entry['method']} {entry['path']}")
        print(f"   {entry['title']}  (score {score:.4f})")
        if entry["description"]:
            print(f"   {entry['description'][:110]}")
        print(f"   scope: {', '.join(scopes) or 'n/a'}   {entry['doc_url']}")
        print()


if __name__ == "__main__":
    main()
