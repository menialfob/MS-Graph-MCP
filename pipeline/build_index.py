"""Build the retrieval index from cached upstream sources.

    python -m pipeline.build_index --profile end_user_helpdesk --out artifacts/index-v1.0

Ablation flags exist so the corpus decisions can be justified with numbers
rather than asserted. ``--ablate docs`` rebuilds using only the machine
generated OpenAPI prose; ``--ablate utterances`` drops the generated questions.
Pair with eval/run_retrieval_eval.py to see what each is worth.
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import numpy as np

from graph_mcp.retrieval.index import IndexMeta, RetrievalIndex
from pipeline import catalog as catalog_mod
from pipeline.curate import Profile, curate
from pipeline.join import join
from pipeline.parse_docs import parse_all as parse_docs
from pipeline.parse_openapi import parse as parse_openapi
from pipeline.parse_permissions import parse_all as parse_permissions
from pipeline.aliases import AliasTable
from pipeline.paraphrase import get_paraphraser

ABLATIONS = ("docs", "utterances", "dense", "aliases")


def build(
    cache: Path,
    profile_path: Path,
    version: str,
    paraphraser_kind: str,
    embedder_kind: str,
    ablate: list[str],
) -> RetrievalIndex:
    docs_root = cache / "microsoft-graph-docs-contrib" / "api-reference" / version

    print("parsing OpenAPI description...")
    ops = list(parse_openapi(cache / f"openapi-{version}.yaml"))
    print(f"  {len(ops):,} operations")

    profile = Profile.load(profile_path)
    kept, stats = curate(ops, profile)
    print(f"curating with profile '{profile.name}':")
    print(stats.report())

    print("parsing API reference docs...")
    pages = parse_docs(docs_root / "api")
    permissions = parse_permissions(docs_root / "includes" / "permissions")
    print(f"  {len(pages):,} pages, {len(permissions):,} permission tables")

    joined = join(kept, pages)
    print(f"joining docs onto operations: {joined.matched:,}/{joined.total:,} "
          f"= {joined.coverage:.1%}")

    entries = catalog_mod.build(kept, joined.doc_by_op, permissions)
    if "docs" in ablate:
        # The counterfactual is "no docs exist", so the docs-gated entry filter
        # goes too -- otherwise the ablation still benefits from the docs by
        # using them to choose what to index.
        indexed = entries
        print(f"catalog: {len(entries):,} executable, all indexed (docs ablated)")
    else:
        indexed = [e for e in entries if e.indexed]
        print(f"catalog: {len(entries):,} executable, {len(indexed):,} indexed "
              f"(documented)")

    # "none" is the default, not an ablation: it should not show up in the
    # artifact's ablation list, or every normal build looks deliberately
    # degraded in its own metadata.
    skip_utterances = paraphraser_kind == "none" or "utterances" in ablate
    if not skip_utterances:
        paraphraser = get_paraphraser(paraphraser_kind, cache / "utterances.json")
        for entry in indexed:
            entry.utterances = paraphraser.utterances(entry)
        total = sum(len(e.utterances) for e in indexed)
        print(f"utterances ({paraphraser.name}): {total:,}")

    aliases = None
    if "aliases" not in ablate:
        aliases = AliasTable.load(Path("config/domain_aliases.yaml"))
        covered = sum(1 for e in indexed if aliases.terms_for(e))
        print(f"domain aliases: {covered:,}/{len(indexed):,} operations matched")

    # Each operation contributes its canonical text plus one text per utterance.
    texts: list[str] = []
    owners: list[int] = []
    for entry_id, entry in enumerate(indexed):
        texts.append(entry.index_text(
            include_docs="docs" not in ablate,
            include_utterances=False,
        ))
        owners.append(entry_id)
        if aliases is not None and (terms := aliases.terms_for(entry)):
            texts.append(f"{entry.title} {' '.join(terms)}")
            owners.append(entry_id)
        if not skip_utterances:
            for utterance in entry.utterances:
                texts.append(utterance)
                owners.append(entry_id)

    vectors = None
    embedder_name = "none(ablated)"
    if "dense" not in ablate:
        from graph_mcp.retrieval.embedders import get_embedder

        embedder = get_embedder(embedder_kind)
        embedder_name = embedder.name
        print(f"embedding {len(texts):,} texts with {embedder_name}...")
        vectors = embedder.encode(texts)

    meta = IndexMeta(
        profile=profile.name,
        graph_version=version,
        embedder=embedder_name,
        paraphraser=paraphraser_kind,
        built_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        n_operations=len(indexed),
        n_texts=len(texts),
        join_coverage=round(joined.coverage, 4),
        ablations=sorted(ablate),
    )
    index = RetrievalIndex(
        [e.to_dict() for e in indexed], texts, owners, vectors, meta
    )
    # The route table covers every curated operation, including undocumented
    # nesting variants that are executable but not worth indexing.
    #
    # It also unions in templates the docs describe but the OpenAPI does not.
    # The description does not expand /me/drive/** at all -- it has only the
    # /me/drive singleton -- yet "GET /me/drive/items/{item-id}/children" is
    # documented and works against the live API. Validating against the spec
    # alone would reject legitimate calls, so the docs get a vote on what is
    # callable.
    routes = catalog_mod.route_table(entries)
    added = catalog_mod.add_doc_routes(routes, pages, profile)
    print(f"route table: {len(routes):,} routes ({added:,} documented but absent "
          f"from the description)")
    index.route_table = routes
    return index


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", type=Path, default=Path(".cache"))
    ap.add_argument("--profile", default="end_user_helpdesk")
    ap.add_argument("--version", default="v1.0", choices=["v1.0", "beta"])
    # Default off: measured. Template utterances help when there is no doc text
    # to index (recall@5 61.8% -> 70.8%) but HURT once docs are joined
    # (78.7% -> 71.9%), because they are all derived from the same noun as the
    # title and so add near-duplicate texts without adding vocabulary. See
    # docs/ARCHITECTURE.md "What the ablations showed".
    ap.add_argument("--paraphraser", default="none",
                    choices=["none", "template", "llm"])
    ap.add_argument("--embedder", default=None, help="local|azure (default: env or local)")
    ap.add_argument("--ablate", default="", help=f"comma-separated: {','.join(ABLATIONS)}")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    ablate = [a.strip() for a in args.ablate.split(",") if a.strip()]
    if bad := set(ablate) - set(ABLATIONS):
        ap.error(f"unknown ablation(s): {', '.join(sorted(bad))}")

    index = build(
        args.cache,
        Path("config/scope_profiles") / f"{args.profile}.yaml",
        args.version,
        args.paraphraser,
        args.embedder,
        ablate,
    )
    index.save(args.out)

    import json
    (args.out / "routes.json").write_text(
        json.dumps(index.route_table, indent=1), encoding="utf-8"
    )
    print(f"\nwrote index to {args.out}")
    print(f"  {index.meta.n_operations:,} operations, {index.meta.n_texts:,} texts, "
          f"{len(index.route_table):,} executable routes")


if __name__ == "__main__":
    main()
