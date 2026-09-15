# Sensitivity labels

**Question asked:** are "personal information" sensitivity labels respected, in
the sense that the server does not retrieve data carrying that label?

**Answer: no.** Nothing in this codebase reads, checks or filters on a
sensitivity label. A labeled item is retrieved, shaped and returned exactly
like an unlabeled one. Graph does not do it on the server's behalf either.

This is a gap, not a bug — no such control was ever designed. This document
records what was measured, why the obvious assumptions do not hold, and what
implementing it would actually require.

Reproduce the findings with `pytest tests/test_sensitivity_labels.py`.

## What was checked

Every layer that could plausibly filter, and the Graph schema underneath it.

| Layer | File | Label-aware? |
|---|---|---|
| Catalog scope | `config/scope_profiles/end_user_helpdesk.yaml` | No — path globs only |
| Route policy | `src/graph_mcp/policy/routes.py` | No — method + path template only |
| Write gating | `src/graph_mcp/policy/writes.py` | No — path allowlist only |
| OData build | `src/graph_mcp/graph/odata.py` | No |
| Response shaping | `src/graph_mcp/graph/shaping.py` | No — size reduction only |
| Default `$select` | `config/select_defaults.yaml` | No label property for any type |

A search across the repository for `sensitivity`, `label`, `classification`,
`informationProtection`, `purview`, `MIP`, `DLP` or `PII` returns two hits,
both the same thing: `/informationProtection/**` in the profile's *exclude*
list. That exclusion removes the API for *reading label definitions*. It does
not filter labeled content — if anything it removes the only vocabulary the
server could have used to recognise a label.

Meanwhile the profile **includes** `/me/messages/**`, `/users/{}/messages/**`,
`/me/drive/**`, `/drives/**`, `/sites/**` and `/search/**` — precisely where
labeled content lives.

## Demonstrated, not just read

`tests/test_sensitivity_labels.py` puts one message in the fixture mailbox
carrying the built-in "Personal information" label the way Graph actually
represents it (`MSIP_Label_<guid>_Name` extended properties), with PII in both
the subject and the body, and drives the real MCP tool surface against it:

- `graph_get /me/messages` returns it. It is not withheld, not redacted, not
  flagged.
- The subject — `"Payroll: Q3 salary and national ID for J. Lind"` — comes back
  under the *default* `$select`. Subjects and filenames routinely carry the
  personal data the label exists to mark.
- Adding `content` to `$select` returns the body in full:
  `"Salary 812,000 DKK. National ID 010190-1234."`

The body being absent by default is **not** a protection. `body` and `content`
are in `_HEAVY_FIELDS` (`shaping.py`) because a page of raw messages is ~100k
tokens of HTML. It is a token-budget heuristic, and an explicit `$select`
bypasses it.

## Why the label is invisible, not merely ignored

The more awkward finding: even a caller who *wanted* to filter has almost
nothing to filter on.

**The default `$select` never asks for a label.** `config/select_defaults.yaml`
lists no label-bearing property for any of its 21 entity types, so against real
Graph the label is not returned at all. The server does not discard the label —
it never requests it.

**Graph v1.0 barely exposes labels on the types this server reads.** Measured
against `$metadata` (1.8 MB CSDL, the same file the type index parses):

| Type | Label property in v1.0 |
|---|---|
| `driveItem` | **none** |
| `message` | **none** (`inferenceClassification` is Focused Inbox, unrelated) |
| `chatMessage`, `site`, `listItem` | **none** |
| `group` | `assignedLabels`, `classification` |
| `event` | `sensitivity` — but see the trap below |
| `onlineMeetingBase` | `sensitivityLabelAssignment` |

For a file, the only v1.0 way to read the applied label is the bound action
`extractSensitivityLabels` (POST on `driveItem`). It is unreachable here three
times over: the profile's `drop_segments_containing: ["microsoft.graph."]`
prunes every bound-action path from the catalog, the action is not in the write
allowlist, and writes are `enabled: false` by default.

`POST /search/query` does return `sensitivityLabelId` in its resource metadata
— the one read path in v1.0 that surfaces a label id for arbitrary content. It
is also unreachable, for the same allowlist reason.

### The trap: `sensitivity` is not a sensitivity label

`event.sensitivity` and `message.sensitivity` use the `graph.sensitivity` enum:

```
normal | personal | private | confidential
```

This is the **legacy Outlook item flag**, set by the sender from the message
options dialog. It is not a Microsoft Purview sensitivity label, it is not
governed by any label policy, and a value of `personal` has nothing to do with
the "Personal information" label. The names collide almost perfectly, which
makes this an easy and expensive thing to get wrong.

Worse, the server forwards a filter on it unchanged, so
`$filter=sensitivity eq 'personal'` narrows results **to** flagged content. The
only label-shaped field readily available through this server works as a
targeting selector, not an exclusion.

## Why "Graph will enforce it" is wrong

Sensitivity labels are not an access-control filter on Graph read APIs. A label
does three things, none of which is "hide this from `GET`":

1. **Encryption (RMS)** — only when the label applies protection. The bytes are
   protected; item *metadata* (subject, filename, path, sender) is returned in
   the clear regardless.
2. **DLP policy** — enforced on egress channels (mail flow, endpoint, browser),
   not on an authenticated Graph read by the item's owner.
3. **Container governance** — site/team/group labels drive sharing and guest
   access, not per-item read filtering.

A classification-only label — which is what "Personal information" typically is,
and what auto-labeling for PII usually applies — has **zero** effect on a Graph
`GET`. The delegated token's scopes are the whole of the access decision.

## What *does* hold

Stated fairly, because this is not a privilege-escalation hole:

- **Delegated-only auth is intact.** Every call runs as the signed-in user
  (`docs/SCOPE.md` §3). The server cannot reach labeled content the user could
  not already open in Outlook. This is an *appropriate-use and exfiltration*
  gap — labeled content flowing into an LLM context window — not a permissions
  bypass. App-only credentials would turn it into one.
- **Labels cannot be tampered with.** `assignSensitivityLabel` is not in the
  write allowlist and writes are disabled by default, so the server cannot
  downgrade or strip a label. Covered by a test.

## Terminology to settle before implementing anything

"Personal information" is ambiguous, and the two readings need different work:

- **The built-in Purview "Personal" label.** In Microsoft's default taxonomy
  (Personal · Public · General · Confidential · Highly Confidential) this means
  *non-business, personal-use content* — not "contains personal data". Usually
  classification-only.
- **A custom label or auto-labeling policy for PII**, driven by sensitive
  information types (national ID, passport number, and so on).

The answer to the original question is "no" under either reading. The
remediation differs, so confirm which one is meant.

## What implementing this would take

Researched in detail in
[SENSITIVITY-LABELS-BLOCKING.md](SENSITIVITY-LABELS-BLOCKING.md), which measures
the three mechanisms Graph v1.0 actually offers — the Copilot Retrieval API's
`InformationProtectionLabelId` filter, Purview's `processContent` policy engine,
and direct label reads via `extractSensitivityLabels` /
`computeRightsAndInheritance` — with their permissions, licensing and coverage
gaps, plus what this repository would have to change to reach any of them.

Two findings from that research are worth repeating here, because they correct
assumptions this document originally left open:

- **Copilot does not do this through a Graph API call.** It relies on the EXTRACT
  usage right (encryption-backed labels only) and on a Purview DLP policy
  targeting the Copilot policy location, enforced inside Microsoft's service.
- **Every label-reading API in Graph v1.0 is a POST**, so none of them is
  reachable through `graph_get`, and routing them through `graph_write` would
  misclassify a read as a mutation. A label check needs its own internal call
  path.
