"""Measure retrieval quality on the gold set.

Reports recall@k and MRR. Recall@k is the number that matters for this
architecture: the search tool hands the calling model a shortlist and the model
picks from it, so the retriever's job is to get the right operation *into* the
shortlist, not to rank it first. recall@5 is the headline.

Run modes let the corpus decisions be checked rather than assumed:

    python eval/run_retrieval_eval.py --index artifacts/index-v1.0
    python eval/run_retrieval_eval.py --index artifacts/index-v1.0 --mode lexical
    python eval/run_retrieval_eval.py --index artifacts/ablate-docs

`--mode` switches the retriever on a single index; comparing corpora (docs,
utterances) needs separately built indexes because those change what is stored.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from graph_mcp.retrieval.index import RetrievalIndex

KS = (1, 3, 5, 10)


def load_gold(paths: list[Path]) -> list[dict]:
    gold = []
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                gold.append(json.loads(line))
    return gold


def evaluate(index: RetrievalIndex, gold: list[dict], mode: str, embedder_kind: str | None):
    keys = [e["key"] for e in index.entries]

    query_vectors = None
    if mode in ("hybrid", "dense"):
        from graph_mcp.retrieval.embedders import get_embedder

        embedder = get_embedder(embedder_kind)
        if embedder.name != index.meta.embedder:
            raise SystemExit(
                f"embedder mismatch: index built with {index.meta.embedder}, "
                f"got {embedder.name}"
            )
        query_vectors = embedder.encode([g["query"] for g in gold], is_query=True)

    hits = {k: 0 for k in KS}
    reciprocal = 0.0
    misses = []

    for i, item in enumerate(gold):
        qv = query_vectors[i] if query_vectors is not None else None
        results = index.search(item["query"], top_k=max(KS), query_vector=qv, mode=mode)
        ranked = [keys[entry_id] for entry_id, _ in results]
        rank = ranked.index(item["target"]) + 1 if item["target"] in ranked else None
        if rank:
            reciprocal += 1.0 / rank
            for k in KS:
                if rank <= k:
                    hits[k] += 1
        else:
            misses.append({**item, "top3": ranked[:3]})

    n = len(gold)
    return {
        "n": n,
        "recall": {k: hits[k] / n for k in KS},
        "mrr": reciprocal / n,
        "misses": misses,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index", type=Path, default=Path("artifacts/index-v1.0"))
    ap.add_argument("--gold", type=Path, nargs="*", default=[
        Path("eval/gold/sample_queries.jsonl"),
        Path("eval/gold/internal_questions.jsonl"),
    ])
    ap.add_argument("--mode", default="hybrid", choices=["hybrid", "lexical", "dense"])
    ap.add_argument("--embedder", default=None)
    ap.add_argument("--show-misses", type=int, default=0)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    index = RetrievalIndex.load(args.index)
    gold = load_gold(args.gold)
    if not gold:
        raise SystemExit("no gold pairs found -- run eval/build_gold.py first")

    result = evaluate(index, gold, args.mode, args.embedder)

    if args.json:
        print(json.dumps({k: v for k, v in result.items() if k != "misses"}, indent=1))
        return

    meta = index.meta
    print(f"index    {args.index}")
    print(f"  profile {meta.profile} | {meta.n_operations:,} ops | "
          f"{meta.n_texts:,} texts | join {meta.join_coverage:.1%}")
    print(f"  embedder {meta.embedder} | utterances {meta.paraphraser}"
          + (f" | ablated: {','.join(meta.ablations)}" if meta.ablations else ""))
    print(f"mode     {args.mode}")
    print(f"gold     {result['n']} queries\n")
    for k in KS:
        bar = "#" * round(result["recall"][k] * 40)
        print(f"  recall@{k:<3} {result['recall'][k]:6.1%}  {bar}")
    print(f"  MRR       {result['mrr']:6.3f}")

    if args.show_misses and result["misses"]:
        print(f"\n--- {min(args.show_misses, len(result['misses']))} of "
              f"{len(result['misses'])} misses")
        for miss in result["misses"][: args.show_misses]:
            print(f"\n  query   {miss['query']}")
            print(f"  want    {miss['target']}")
            for got in miss["top3"]:
                print(f"  got     {got}")


if __name__ == "__main__":
    main()
