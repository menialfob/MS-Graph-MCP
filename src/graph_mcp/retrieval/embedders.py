"""Pluggable embedding backends.

Production in a Microsoft-integrated environment should use Azure OpenAI: same
tenant, same compliance and data-residency story as the rest of the estate. The
local sentence-transformers backend is the default for builds and CI so the
index is reproducible without credentials and the eval can run anywhere.

Both must agree on dimensionality only within a single index -- the artifact
records which backend built it, and the server refuses to load an index built
by a different one.
"""

from __future__ import annotations

import os
from typing import Protocol

import numpy as np

DEFAULT_LOCAL_MODEL = "BAAI/bge-small-en-v1.5"


class Embedder(Protocol):
    name: str
    dim: int

    def encode(self, texts: list[str], is_query: bool = False) -> np.ndarray: ...


class LocalEmbedder:
    """sentence-transformers, CPU, normalised vectors."""

    def __init__(self, model_name: str = DEFAULT_LOCAL_MODEL):
        from sentence_transformers import SentenceTransformer

        self.name = f"local:{model_name}"
        self._model = SentenceTransformer(model_name, device="cpu")
        self.dim = self._model.get_sentence_embedding_dimension()
        # BGE models are trained with an asymmetric query prefix; skipping it
        # measurably degrades retrieval.
        self._query_prefix = (
            "Represent this sentence for searching relevant passages: "
            if "bge" in model_name.lower()
            else ""
        )

    def encode(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        if is_query and self._query_prefix:
            texts = [self._query_prefix + t for t in texts]
        return self._model.encode(
            texts, batch_size=64, normalize_embeddings=True, show_progress_bar=False
        ).astype(np.float32)


class AzureOpenAIEmbedder:
    """Azure OpenAI embeddings for production deployments."""

    def __init__(self, deployment: str = "text-embedding-3-large", dim: int = 3072):
        from openai import AzureOpenAI

        self.name = f"azure:{deployment}"
        self.dim = dim
        self._deployment = deployment
        self._client = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        )

    def encode(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), 256):
            resp = self._client.embeddings.create(
                model=self._deployment, input=texts[start : start + 256]
            )
            vectors.extend(item.embedding for item in resp.data)
        arr = np.asarray(vectors, dtype=np.float32)
        return arr / np.linalg.norm(arr, axis=1, keepdims=True)


def get_embedder(kind: str | None = None) -> Embedder:
    kind = kind or os.environ.get("GRAPH_MCP_EMBEDDER", "local")
    if kind == "local":
        return LocalEmbedder()
    if kind == "azure":
        return AzureOpenAIEmbedder()
    raise ValueError(f"unknown embedder: {kind}")
