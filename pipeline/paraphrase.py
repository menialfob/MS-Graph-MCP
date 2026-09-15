"""Generate the natural-language utterances each operation is retrieved by.

Indexing an operation's *description* is a mismatch: descriptions are written
in third-person API prose ("Get the messages in the signed-in user's mailbox")
while queries arrive in first-person intent ("what's in my inbox"). Indexing
hypothetical *questions* alongside the description closes some of that gap.

Two providers:

* ``template`` -- deterministic, offline, no API key. Derives utterances from
  the doc title, the returned entity type and the path family. Runs in CI and
  makes builds reproducible.
* ``llm`` -- richer paraphrases from a model. Better coverage of the way
  people actually phrase things, at the cost of an API key and a cache.

MEASURED RESULT: the ``template`` provider is OFF by default because it makes
retrieval worse when docs are available. On the 89-query gold set, recall@5 was
61.8% with neither docs nor utterances, 70.8% with utterances alone, 78.7% with
docs alone, and 71.9% with both. Template utterances substitute for missing
descriptions rather than complementing real ones: every phrasing is derived
from the same noun as the title, so they add near-duplicate short texts that
dilute term statistics without adding vocabulary.

The idea is not dead -- it needs utterances that introduce words the docs do
not use ("email", "inbox" for /me/messages), which is what the ``llm`` provider
is for. That remains unmeasured; do not enable it on faith, measure it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol

from pipeline.catalog import CatalogEntry

_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")

# The description generator names plain CRUD operations with a capitalised verb
# ("me.CreateMessages", "users.ListMessages") but leaves OData actions and
# functions under their real camelCase name ("me.sendMail", "event.accept").
# That distinction matters: "create a message" is a fair paraphrase of POST
# /me/messages but a bad one for POST /me/sendMail.
_CRUD_RE = re.compile(r"\.(List|Create|Get|Update|Delete|Set)[A-Z$]")

_GET_LEADS = ("show me", "list", "find", "what are")
_CRUD_LEADS = {
    "POST": ("create", "add", "how do i create"),
    "PATCH": ("update", "change", "edit"),
    "PUT": ("replace", "set", "upload"),
    "DELETE": ("delete", "remove", "get rid of"),
}


def collection_noun(entry: CatalogEntry) -> str:
    """The noun a person would use for what this operation returns.

    The navigation-property name in the path beats the OData type name.
    GET /me/directReports returns microsoft.graph.directoryObject, so the type
    yields "directory objects" -- but nobody calls their reports that. The last
    path segment says "direct reports", which is what people ask for.
    """
    last = entry.path.rstrip("/").rsplit("/", 1)[-1]
    if last and not last.startswith("{"):
        return _CAMEL_RE.sub(" ", last).lower().strip()
    return humanize_type(entry.response_type or entry.request_type)


# Nav properties ending in a preposition ("memberOf", "transitiveMemberOf")
# have no sensible plural -- "member ofs" is worse than leaving it alone. The
# doc title and description carry the meaning for these; utterances only add
# lexical variety.
_NO_PLURAL_TAIL = {"of", "to", "with", "by", "for", "in", "from"}


def pluralize(noun: str) -> str:
    if not noun or noun.endswith("s"):
        return noun
    if noun.rsplit(" ", 1)[-1] in _NO_PLURAL_TAIL:
        return noun
    if noun.endswith("y") and noun[-2:-1] not in "aeiou":
        return noun[:-1] + "ies"
    if noun.endswith(("x", "ch", "sh")):
        return noun + "es"
    return noun + "s"


class Paraphraser(Protocol):
    name: str

    def utterances(self, entry: CatalogEntry) -> list[str]: ...


def humanize_type(type_ref: str) -> str:
    """'microsoft.graph.mailFolder' -> 'mail folder'."""
    leaf = type_ref.rsplit(".", 1)[-1]
    return _CAMEL_RE.sub(" ", leaf).lower().strip()


class TemplateParaphraser:
    """Deterministic utterances from title, entity type and path family."""

    name = "template"

    def utterances(self, entry: CatalogEntry) -> list[str]:
        if not entry.title:
            return []

        # Titles read like "List messages" or "user: sendMail"; drop the
        # resource qualifier and split camelCase action names into words.
        subject = entry.title.split(":", 1)[-1].strip()
        subject = _CAMEL_RE.sub(" ", subject).lower().strip()
        out: list[str] = [subject]

        is_crud = bool(_CRUD_RE.search(entry.operation_id))
        if not is_crud:
            # An OData action: its own name is the best phrasing there is.
            # Generic CRUD lead-ins ("create my message" for sendMail) are
            # actively misleading, so they are not generated.
            out.append(f"how do i {subject}")
            if entry.is_self:
                out.append(f"{subject} for me")
            return _dedupe(out)

        noun = collection_noun(entry)
        if not noun:
            return _dedupe(out)
        if entry.returns_collection:
            noun = pluralize(noun)  # no-op when the nav property is already plural
        target = f"my {noun}" if entry.is_self else noun

        leads = _GET_LEADS if entry.method == "GET" else _CRUD_LEADS.get(entry.method, ())
        out += [f"{lead} {target}" for lead in leads]
        if entry.returns_collection:
            out.append(f"all {target}")
        return _dedupe(out)


def _dedupe(items: list[str]) -> list[str]:
    seen, result = set(), []
    for item in items:
        item = re.sub(r"\s+", " ", item).strip()
        if len(item) > 3 and item not in seen:
            seen.add(item)
            result.append(item)
    return result[:8]


class LLMParaphraser:
    """Model-generated utterances, cached on disk and keyed by operation.

    Not exercised in the spike build (no API key in CI); wired up so the
    production build can swap it in without touching the pipeline.
    """

    name = "llm"

    def __init__(self, cache_path: Path, model: str = "claude-sonnet-5"):
        self.cache_path = cache_path
        self.model = model
        self._cache: dict[str, list[str]] = {}
        if cache_path.exists():
            self._cache = json.loads(cache_path.read_text(encoding="utf-8"))
        self._fallback = TemplateParaphraser()

    def utterances(self, entry: CatalogEntry) -> list[str]:
        if cached := self._cache.get(entry.key):
            return cached
        return self._fallback.utterances(entry)

    def generate(self, entries: list[CatalogEntry], client) -> None:
        """Populate the cache. `client` is an Anthropic SDK client.

        Kept separate from `utterances` so a build never makes network calls
        implicitly -- generation is an explicit, reviewable pipeline step whose
        output is committed.
        """
        prompt = (
            "You are indexing the Microsoft Graph API for natural-language search.\n"
            "Given one API operation, write 8 short questions or commands a "
            "colleague might type that this operation answers. Vary the phrasing: "
            "some terse, some full questions. Use first person for /me paths. "
            "Return one per line, no numbering.\n\n"
        )
        for entry in entries:
            if entry.key in self._cache or not entry.title:
                continue
            msg = client.messages.create(
                model=self.model,
                max_tokens=400,
                messages=[{
                    "role": "user",
                    "content": (
                        f"{prompt}Operation: {entry.method} {entry.path}\n"
                        f"Name: {entry.title}\n"
                        f"Description: {entry.description}\n"
                        f"Returns: {entry.response_type}"
                    ),
                }],
            )
            lines = [ln.strip("-• ").strip() for ln in msg.content[0].text.splitlines()]
            self._cache[entry.key] = [ln for ln in lines if len(ln) > 3][:8]
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self._cache, indent=1), encoding="utf-8")


def get_paraphraser(kind: str, cache: Path | None = None) -> Paraphraser:
    if kind == "template":
        return TemplateParaphraser()
    if kind == "llm":
        if cache is None:
            raise ValueError("llm paraphraser needs a cache path")
        return LLMParaphraser(cache)
    raise ValueError(f"unknown paraphraser: {kind}")
