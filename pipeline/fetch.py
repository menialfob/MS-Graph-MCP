"""Fetch all upstream sources into the local cache."""

from __future__ import annotations

import argparse
from pathlib import Path

from pipeline.sources import fetch_docs, fetch_file, file_sources


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", default="v1.0", choices=["v1.0", "beta"])
    ap.add_argument("--cache", type=Path, default=Path(".cache"))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    for src in file_sources(args.version):
        path = fetch_file(src, args.cache, args.force)
        print(f"  {src.name:16} {path} ({path.stat().st_size / 1e6:.1f} MB)")

    repo = fetch_docs(args.cache, args.version, args.force)
    api_dir = repo / "api-reference" / args.version / "api"
    perm_dir = repo / "api-reference" / args.version / "includes" / "permissions"
    print(f"  {'docs':16} {api_dir} ({len(list(api_dir.glob('*.md')))} pages)")
    print(f"  {'permissions':16} {perm_dir} ({len(list(perm_dir.glob('*.md')))} files)")


if __name__ == "__main__":
    main()
