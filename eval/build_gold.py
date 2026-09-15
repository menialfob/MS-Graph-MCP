"""Build the retrieval gold set from Graph Explorer's curated sample queries.

Each sample query pairs a natural-language name with the request it maps to:

    {"humanName": "my direct reports", "requestUrl": "/v1.0/me/directReports"}

That is exactly the (question, correct operation) shape an eval needs, written
by Microsoft rather than by us, which makes it a far more honest test than
questions authored by whoever also built the index.

No leakage: sample queries are NOT part of the index. The index is built from
doc titles/descriptions and generated utterances only (see pipeline/build_index),
so nothing here has been seen by the retriever.

A query is kept only when its target operation is actually in the index.
Queries dropped because their target was pruned out of scope are scope
decisions, not retrieval failures, and counting them either way would be
misleading -- so the script reports how many were dropped and why.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from pipeline.join import normalize_path


def build(sample_path: Path, index_dir: Path, version: str = "v1.0") -> tuple[list[dict], Counter]:
    samples = json.loads(sample_path.read_text(encoding="utf-8"))["SampleQueries"]
    catalog = json.loads((index_dir / "catalog.json").read_text(encoding="utf-8"))

    # Normalised (method, path) -> catalog key.
    lookup = {
        f"{e['method'].lower()} {normalize_path(e['path'])}": e["key"] for e in catalog
    }

    gold: list[dict] = []
    reasons: Counter = Counter()
    for sample in samples:
        url = sample["requestUrl"]
        if not url.startswith(f"/{version}"):
            reasons["wrong api version"] += 1
            continue
        key = f"{sample['method'].lower()} {normalize_path(url)}"
        target = lookup.get(key)
        if target is None:
            reasons["target not in index"] += 1
            continue
        gold.append({
            "query": sample["humanName"],
            "target": target,
            "category": sample.get("category", ""),
            "source": "graph-explorer-samples",
        })
        reasons["kept"] += 1
    return gold, reasons


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=Path, default=Path(".cache/sample-queries.json"))
    ap.add_argument("--index", type=Path, default=Path("artifacts/index-v1.0"))
    ap.add_argument("--out", type=Path, default=Path("eval/gold/sample_queries.jsonl"))
    args = ap.parse_args()

    gold, reasons = build(args.samples, args.index)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "\n".join(json.dumps(g) for g in gold) + "\n", encoding="utf-8"
    )
    total = sum(reasons.values())
    print(f"sample queries: {total}")
    for reason, count in reasons.most_common():
        print(f"  {reason:24} {count:>4}")
    print(f"\nwrote {len(gold)} gold pairs to {args.out}")


if __name__ == "__main__":
    main()
