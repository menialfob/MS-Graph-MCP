"""Upstream Microsoft Graph metadata sources and local caching.

Every source here is public and unauthenticated. The GitHub REST API is
deliberately avoided: it is frequently blocked on locked-down corporate
networks. We use raw.githubusercontent.com for single files and a blobless
shallow git clone for the docs repo, which needs whole-directory enumeration.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import requests

RAW = "https://raw.githubusercontent.com"

# The docs repo is ~92k files; a blobless sparse clone of just the directories
# we parse keeps it under ~60 MB instead of several GB.
DOCS_REPO = "https://github.com/microsoftgraph/microsoft-graph-docs-contrib.git"
DOCS_SPARSE_DIRS = {
    "v1.0": ["api-reference/v1.0/api", "api-reference/v1.0/includes/permissions"],
    "beta": ["api-reference/beta/api", "api-reference/beta/includes/permissions"],
}


@dataclass(frozen=True)
class FileSource:
    name: str
    url: str
    filename: str


def file_sources(version: str) -> list[FileSource]:
    return [
        FileSource(
            "openapi",
            f"{RAW}/microsoftgraph/msgraph-metadata/master/openapi/{version}/openapi.yaml",
            f"openapi-{version}.yaml",
        ),
        FileSource(
            "csdl",
            f"https://graph.microsoft.com/{version}/$metadata",
            f"metadata-{version}.xml",
        ),
        FileSource(
            "sample_queries",
            f"{RAW}/microsoftgraph/microsoft-graph-devx-content/dev"
            "/sample-queries/sample-queries.json",
            "sample-queries.json",
        ),
    ]


def fetch_file(src: FileSource, cache: Path, force: bool = False) -> Path:
    dest = cache / src.filename
    if dest.exists() and not force:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(src.url, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
        tmp.replace(dest)
    return dest


def fetch_docs(cache: Path, version: str, force: bool = False) -> Path:
    """Sparse-checkout the docs directories we parse.

    Returns the repo root; callers join ``api-reference/<version>/...`` onto it.
    """
    repo = cache / "microsoft-graph-docs-contrib"
    dirs = DOCS_SPARSE_DIRS[version]
    if repo.exists() and not force:
        # Widen the sparse cone if a previous run fetched a different version.
        _run(["git", "sparse-checkout", "add", *dirs], cwd=repo)
        return repo

    repo.parent.mkdir(parents=True, exist_ok=True)
    _run([
        "git", "clone", "--depth", "1", "--filter=blob:none",
        "--sparse", DOCS_REPO, str(repo),
    ])
    _run(["git", "sparse-checkout", "set", *dirs], cwd=repo)
    return repo


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)
