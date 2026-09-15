# Architecture

Why the design is what it is, and the measurements behind it. For running and
porting the server, see [DEPLOYMENT.md](DEPLOYMENT.md).

## The problem

Microsoft Graph cannot be exposed as one MCP tool per operation. Measured from
the v1.0 OpenAPI description:

| | v1.0 |
|---|---|
| Description size | 43 MB / 982k lines |
| Path templates | 11,493 |
| Operations | 17,777 (9,378 GET · 3,681 POST · 2,144 PATCH · 237 PUT · 2,337 DELETE) |
| CSDL `$metadata` | 1.8 MB · 1,223 entity types · 1,810 complex types · 876 enums · 863 actions · 328 functions |

`beta` is larger. So the tool surface must be small and fixed, and finding the
right operation becomes a retrieval problem.

## Prior art

[Microsoft MCP Server for Enterprise](https://learn.microsoft.com/en-us/graph/mcp-server/overview)
(public preview) ships three tools — `suggest_queries` (RAG over a curated
catalog), `get`, `list_properties`. It validates the discover-then-execute
shape but is Entra-identity only, read-only, and capped at 100 calls/min/user.
[Lokka](https://github.com/merill/lokka) is the opposite: a single passthrough
tool, which works for endpoints the model already knows and hallucinates on the
long tail.

This design keeps the shape, extends it across the M365 workloads, and adds
scope-aware filtering and a gated write path.

## The pipeline

```
fetch → parse OpenAPI → parse docs → join → curate → alias → embed → index artifact
```

| Source | Contributes |
|---|---|
| `msgraph-metadata` OpenAPI | callable paths, parameters, request/response types |
| `microsoft-graph-docs-contrib` `api-reference/v1.0/api/*.md` | human titles, descriptions, HTTP templates |
| same repo, `includes/permissions/*.md` | least/higher-privileged scopes per operation |
| `graph.microsoft.com/v1.0/$metadata` | CSDL types for `graph_describe_type` |
| `sample-queries.json` | 328 curated NL→URL pairs, used **only** as the eval gold set |

The OpenAPI description carries no permission data — there are no
`securitySchemes` — so scopes come from the docs includes.

### Why the docs are not optional

The description is generated from CSDL, so its prose is mechanical:

- `GET /users/{user-id}/messages` → *"Get messages from users"*
- `POST /users/{user-id}/messages` → *"Create new navigation property to messages for users"*

Indexing that alone scores **recall@5 61.8%**. Joining the human-written docs
takes it to **78.7%**. The docs are the single biggest lever in the system.

### The join

`operationId` matching does not work — the spec says `users.ListMessages` while
the doc file is `user-list-messages.md`. The reliable key is the path template
from each page's `## HTTP request` block, with both sides normalised:

```
docs:    GET /users/{id | userPrincipalName}/messages
spec:    GET /users/{user-id}/messages
both ->  get /users/{}/messages
```

Parameter names are discarded deliberately: only position is structurally
meaningful. One doc page fans out to several operations, which is what we want —
`/me/messages` and `/users/{}/messages` inherit the same prose.

`/me/X` and `/users/{}/X` are treated as aliases, because the docs frequently
document only one. The calendarView page lists `/users/{id}/calendarView` and
`/me/calendar/calendarView` but not the bare `/me/calendarView` — the most
natural way to ask "what meetings do I have". This recovers 82 operations.

### Indexed vs executable

Two sets, deliberately:

- **Indexed (868 operations)** — everything with human-written documentation.
  Only these are retrievable: an operation described as *"Get media content for
  the navigation property items from drives"* cannot be found by natural
  language and only adds noise.
- **Executable (1,737 routes)** — every curated operation. The undocumented
  remainder is mostly deeper nesting of documented actions —
  `/me/mailFolders/{}/childFolders/{}/messages/{}/reply` is the documented
  `/me/messages/{}/reply` with more nesting. The model finds the documented
  action and constructs the path it needs.

The route table also unions in templates the **docs describe but the OpenAPI
omits**. The description does not expand `/me/drive/**` at all — only the
`/me/drive` singleton — yet `GET /me/drive/items/{item-id}/children` is
documented and works against the live API. Validating against the spec alone
would reject legitimate calls, so the docs get a vote (250 routes added).

### Curation

17,777 → 2,242 executable → 868 indexed.

Structural pruning removes generated noise: `$count`/`$ref`/`$value` siblings,
OData type-cast variants, and navigation expansions deeper than three ids. The
description enumerates every navigation-property combination, which is why there
are 11,493 paths for a few hundred real resources.

Scope filtering then applies `config/scope_profiles/end_user_helpdesk.yaml` —
a product decision about usefulness, **not** a security control. See
[SCOPE.md](SCOPE.md).

## Retrieval

Hybrid BM25 + dense embeddings fused with Reciprocal Rank Fusion. RRF rather
than a weighted blend because BM25 scores are unbounded while cosine
similarities sit in a narrow band, so any fixed weighting is arbitrary and
drifts as the corpus changes. RRF needs only the rankings.

Hybrid is not hedging — it beats either half:

| mode | recall@1 | recall@5 | MRR |
|---|---|---|---|
| lexical only | 50.6% | 64.0% | 0.581 |
| dense only | 49.4% | 64.0% | 0.556 |
| **hybrid** | **56.2%** | **71.9%** | **0.629** |

*(before the domain-alias layer, on the 89-query sample set)*

No cross-encoder reranker: search returns 8 candidates and the calling model
picks. The model is the reranker, which saves a hop.

No vector database. 868 operations × a few texts is a numpy array; the index
ships as a file beside the server, keeping it stateless and deployments atomic.
`Embedder` is a protocol — local `bge-small` for reproducible CI builds, Azure
OpenAI `text-embedding-3-large` for production.

## What the ablations showed

89 Microsoft-authored sample queries + 35 hand-written conversational questions.
Sample queries are **not** in the index, so there is no leakage.

| corpus | ops | texts | recall@1 | recall@3 | recall@5 | MRR |
|---|---|---|---|---|---|---|
| spec prose only | 2,242 | 2,242 | 41.6% | 53.9% | 61.8% | 0.501 |
| spec + template utterances | 2,242 | 5,795 | 49.4% | 64.0% | 70.8% | 0.577 |
| docs + template utterances | 868 | 4,421 | 56.2% | 69.7% | 71.9% | 0.629 |
| **docs, no utterances** | 868 | 868 | 56.2% | 74.2% | **78.7%** | 0.652 |

**Docs enrichment is worth +16.9 points** at recall@5. The core thesis,
confirmed.

**Template-generated utterances make retrieval worse when docs are present**
(78.7% → 71.9%), though they help when there are none (61.8% → 70.8%). They
substitute for missing descriptions rather than complementing real ones: every
phrasing derives from the same noun as the title, adding near-duplicate short
texts that dilute term statistics without adding vocabulary. **Off by default.**

This contradicted the design assumption that synthetic questions would be the
biggest win, which is exactly why the ablation existed.

### The real bottleneck was vocabulary

Every remaining miss was a synonym gap, not a ranking failure:

| user says | Graph says |
|---|---|
| send an email | `sendMail` |
| what meetings do I have | `calendarView` |
| am I available | `presence` |
| document library | `drive` / `driveItem` |
| colleague | `user` |

`config/domain_aliases.yaml` maps Graph concepts to the words people use,
indexed as one extra text per operation (250 of 868 matched).

| gold set | recall@5 before | recall@5 after |
|---|---|---|
| Microsoft sample queries (89) | 78.7% | **83.1%** |
| hand-written questions (35) | 68.6% | **91.4%** |
| combined (124) | 75.8% | **85.5%** |

**Caveat:** the alias file was written after observing that failures clustered
into vocabulary categories. The Microsoft-authored queries are the cleaner
signal (+4.4 points) because we did not write them; the hand-written set's
+22.8 should be treated as optimistic. Re-measure against real user queries
before trusting the combined figure.

The honest read: **recall@5 ≈ 83%** on independent queries. The next lever is
LLM-generated utterances introducing genuinely new vocabulary — wired up in
`pipeline/paraphrase.py` but unmeasured. Do not enable it on faith.

## Freshness

Graph ships weekly. The index is a versioned build artifact; a scheduled job
should re-pull, rebuild, re-run the eval, and open a PR with the operation diff
and the recall delta. A recall regression should block the merge.
