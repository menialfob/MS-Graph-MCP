"""BM25 over the operation catalog.

Implemented directly rather than pulled in as a dependency: the whole index is
a few thousand short documents, the algorithm is a page of code, and the server
ships the index as a build artifact, so avoiding a runtime dependency keeps
deployment simple.

Lexical retrieval is not a fallback here -- it is half the design. Graph
queries are dense with exact identifiers ("signInActivity", "driveItem",
"subscribedSkus") that embeddings blur together but exact term matching nails.
"""

from __future__ import annotations

import math
import re
from collections import Counter

_TOKEN_RE = re.compile(r"[a-z0-9$]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

_STOPWORDS = frozenset(
    "a an the of in on to for and or is are was be been do does did how what "
    "which who whom whose when where why can could should would i me my we our "
    "you your it its this that these those with from by as at".split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, keeping both the joined and split camelCase forms.

    'mailFolders' yields 'mailfolders', 'mail' and 'folders', so a query typing
    the API spelling and a query typing the human phrasing both match the same
    document. Emitting only the split parts loses exact-identifier matching,
    which is most of what the lexical half of the hybrid is for.
    """
    out: list[str] = []
    for raw in _TOKEN_RE.findall(text.lower()):
        if raw not in _STOPWORDS:
            out.append(raw)
    for part in _TOKEN_RE.findall(_CAMEL_RE.sub(" ", text).lower()):
        if part not in _STOPWORDS and part not in out:
            out.append(part)
    return out


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.doc_tokens = [tokenize(d) for d in docs]
        self.doc_len = [len(t) for t in self.doc_tokens]
        self.avgdl = (sum(self.doc_len) / len(self.doc_len)) if self.doc_len else 0.0
        self.freqs = [Counter(t) for t in self.doc_tokens]

        df: Counter[str] = Counter()
        for tokens in self.doc_tokens:
            df.update(set(tokens))
        n = len(docs)
        self.idf = {
            term: math.log(1 + (n - count + 0.5) / (count + 0.5))
            for term, count in df.items()
        }
        # term -> doc ids, so scoring touches only documents that can score.
        self.postings: dict[str, list[int]] = {}
        for i, tokens in enumerate(self.doc_tokens):
            for term in set(tokens):
                self.postings.setdefault(term, []).append(i)

    def scores(self, query: str) -> dict[int, float]:
        result: dict[int, float] = {}
        for term in tokenize(query):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for doc_id in self.postings[term]:
                freq = self.freqs[doc_id][term]
                denom = freq + self.k1 * (
                    1 - self.b + self.b * self.doc_len[doc_id] / (self.avgdl or 1)
                )
                result[doc_id] = result.get(doc_id, 0.0) + idf * freq * (self.k1 + 1) / denom
        return result

    def search(self, query: str, top_k: int = 20) -> list[tuple[int, float]]:
        scored = self.scores(query)
        return sorted(scored.items(), key=lambda kv: -kv[1])[:top_k]
