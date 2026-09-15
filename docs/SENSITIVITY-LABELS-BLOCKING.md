# Blocking labeled content: how it can actually be done

Follow-up to [SENSITIVITY-LABELS.md](SENSITIVITY-LABELS.md), which established that
this server does not respect sensitivity labels. This document is the research
into how to fix that, measured against Graph v1.0 `$metadata` and Microsoft's
current documentation.

Short version: it is achievable, three different ways, none of them free — and
the mechanism Copilot uses is **not** the one you would guess.

## First, correcting the premise

Microsoft 365 Copilot does not enforce sensitivity labels by calling a Graph API
and filtering the results. It uses two tenant-side mechanisms, neither of which
is a Graph call:

**1. The EXTRACT usage right — only for labels that apply encryption.**
When a label applies encryption, the signed-in user must hold both VIEW and
EXTRACT usage rights for Copilot to use the content. Given VIEW but not EXTRACT,
Copilot will cite the item with a link but will not summarise it. A
classification-only label has no usage rights at all, so this mechanism does
nothing for it.

**2. A Purview DLP policy targeting the "Microsoft 365 Copilot and Copilot Chat"
policy location**, with the `Content contains > Sensitivity labels` condition.
Enforcement happens *inside Microsoft's service*, configured by an admin in the
Purview portal. Microsoft's own documentation for this feature uses **`Personal`**
as its worked example, with GDPR as the motivation — precisely the case in
question. The documented effect: "the content of the item isn't used in the
response or accessed by Copilot", though the item may still appear in citations.

So Copilot gets this by being a first-party participant in a policy location that
the tenant admin configures. There is no `?$filter=notLabeled` to copy.

The useful news is that Microsoft has opened the same machinery to third-party
apps. That is what makes this implementable here.

## The three viable mechanisms

| | A. Copilot Retrieval | B. Purview processContent | C. Read the label yourself |
|---|---|---|---|
| Enforcement point | Before the query runs | After retrieval, before use | After retrieval, before use |
| Who decides | Microsoft's index | Tenant DLP policy | This server's config |
| Mail / Teams chat | ❌ | ✅ | ⚠️ mail only, awkwardly |
| Files / SharePoint | ✅ | ✅ | ✅ |
| Extra licence | M365 Copilot per user | — | — |
| Status | GA v1.0 | GA v1.0 | GA v1.0 |

### A. Copilot Retrieval API — exclude before the query runs

```http
POST https://graph.microsoft.com/v1.0/copilot/retrieval
{
  "queryString": "Q3 payroll process",
  "dataSource": "sharePoint",
  "filterExpression": "NOT InformationProtectionLabelId:\"<personal-info-guid>\"",
  "maximumNumberOfResults": 25
}
```

Delegated-only (`Files.Read.All` + `Sites.Read.All`; `ExternalItem.Read.All` for
connectors) — application permissions are *not supported*, which matches this
server's delegated-only design exactly.

Two things make it attractive. Each `retrievalHit` carries a `sensitivityLabel`
object (`sensitivityLabelId`, `displayName`, `priority`, `color`, `tooltip`), so
the label is visible for the first time anywhere in this system. And
`filterExpression` accepts `InformationProtectionLabelId` as a KQL property, so
labeled content can be excluded **server-side, before retrieval happens** — the
strongest possible enforcement point, because the data never crosses the wire.

The catches are real:

- **SharePoint, OneDrive and Copilot connectors only.** No Exchange mail, no
  Teams chat. The `retrievalDataSource` enum in `$metadata` confirms it:
  `sharePoint | oneDriveBusiness | externalItem | sharePointEmbedded`. For a
  server whose profile leads with `/me/messages`, this covers the minority of
  the surface.
- **A Microsoft 365 Copilot licence is required for every calling user**, on top
  of E3/E5. This is the expensive option.
- It returns *relevance-ranked text extracts*, not entities. It is a grounding
  API, not a replacement for `graph_get` — it cannot answer "list my unread mail".
- Capped at 25 results, 1,500-character query.
- `filterExpression` fails **open**: Microsoft's documentation states that invalid
  KQL causes the query to execute *as if there were no filter*. A malformed
  exclusion silently returns labeled content. Any use of this must validate the
  expression and verify the returned `sensitivityLabel` on every hit anyway.

### B. Purview `processContent` — ask the tenant's own DLP engine

```http
POST https://graph.microsoft.com/v1.0/me/dataSecurityAndGovernance/processContent
```

Delegated `Content.Process.User` (least) or `Content.Process.All`. GA in v1.0.
Note that `/me` **requires** a delegated permission — application permissions are
rejected on that path, which again fits this server's model.

This is the sanctioned "bring your own AI app under Purview governance" path. You
send the content you are about to use; Purview evaluates it against the tenant's
DLP policies and returns `policyActions`, a collection of `dlpActionInfo`. The
`dlpAction` enum includes `blockAccess` and `restrictAccess`, and
`restrictionAction` resolves to `warn | audit | block` — a genuine, actionable
verdict rather than a hint.

Architecturally this is the best answer for classification-only labels, because
the decision lives in Purview where the admin already manages it, instead of
being a list of label GUIDs hardcoded in a YAML file in this repo. When the
compliance team retires a label, nothing here needs redeploying.

Supporting endpoints complete the pattern:

- `POST /me/dataSecurityAndGovernance/protectionScopes/compute` returns an ETag.
  Pass it back as `If-None-Match`; a `protectionScopeState` of `notModified`
  means no policy applies and the `processContent` round trip can be skipped
  entirely. Without this, every read pays the latency.
- `POST /me/dataSecurityAndGovernance/activities/contentActivities` records what
  was accessed, giving the audit trail Copilot deployments get for free.
- `processConversationMetadata.accessedResources_v2` is the RAG-shaped path: each
  `resourceAccessDetail` carries a **`labelId`**, so grounding items can be
  declared with their labels. (It also carries `isCrossPromptInjectionDetected`,
  which is interesting for a different reason.)

The catches:

- Requires app registration with `ProtectionScopes.Compute.All`,
  `ContentActivity.Write` and `Content.Process.All` on the service principal,
  **and** an admin who has authored a DLP policy targeting the app's location.
  With no policy configured, this returns no actions and blocks nothing — it is
  inert until someone does the Purview-side work.
- You must send content to Purview to have it evaluated. That is another data
  flow to document and another round trip per read.
- For label-condition rules you still need the item's `labelId`, which brings
  mechanism C back in.

### C. Read the label directly and decide locally

The self-contained option — no extra licence, no admin prerequisite.

**Resolve label names to GUIDs** (`Content.Process.All` or
`InformationProtectionPolicy.Read`):
```http
GET /v1.0/security/dataSecurityAndGovernance/sensitivityLabels
    ?$filter=applicableTo has 'file'
```
Returns `id`, `name`, `priority`, `isEnabled`, `sublabels`. This is how
"Personal information" becomes a GUID to match on. Cache it — it changes rarely.

**Files** (delegated `Files.Read.All`):
```http
POST /v1.0/me/drive/items/{item-id}/extractSensitivityLabels
→ { "labels": [ { "sensitivityLabelId": "...", "assignmentMethod": "standard" } ] }
```
Limits: supported Office file extensions only; not supported for SharePoint
Embedded; returns `423 Locked` with `fileDoubleKeyEncrypted`,
`fileDecryptionNotSupported` or `fileDecryptionDeferred` for files it cannot
open. Each of those must be treated as "labeled" — see fail-closed below.

**Mail**: no label property exists on `message`. The label lives in
`MSIP_Label_{guid}_*` named MAPI properties, reachable via
`$expand=singleValueExtendedProperties($filter=...)`. Workable, but the filter
must name each label GUID, and it is one expansion per message.

**Encrypted labels — the Copilot-equivalent check**:
```http
POST /v1.0/security/dataSecurityAndGovernance/sensitivityLabels/computeRightsAndInheritance
```
Returns `contentRights` with a `usageRights` flags enum whose members include
`extract` (2048), `view`, `owner` and the failure sentinels `accessDenied` and
`encryptedProtectionTypeNotSupportedException`. Testing for `extract` reproduces
exactly what Copilot does for encrypted content. This is the one mechanism that
is a faithful port of first-party behaviour, and it is worth having regardless of
which of A/B/C is chosen for classification-only labels.

The catch for C as a whole: it is **N+1 calls**. One `extractSensitivityLabels`
POST per file does not survive a 200-item folder listing. It is viable for
"fetch this one item", not for enumeration — which is a large share of what an
MCP server does.

## What this repository would have to change

All three options are currently unreachable here, for reasons already documented
in `SCOPE.md` and the profile:

| Blocker | Location | Effect |
|---|---|---|
| `/copilot/**` excluded | `end_user_helpdesk.yaml` | kills option A |
| `/informationProtection/**` excluded | `end_user_helpdesk.yaml` | kills label enumeration |
| `/security/**` excluded | `end_user_helpdesk.yaml` | kills `computeRightsAndInheritance`, label list |
| `drop_segments_containing: ["microsoft.graph."]` | `end_user_helpdesk.yaml` | prunes `extractSensitivityLabels` (a bound action) |
| `graph_get` is GET-only | `server.py` | every one of these is a POST |
| Write allowlist + `enabled: false` | `write_allowlist.yaml` | blocks them again as writes |

That last pair is the awkward structural one. **Every label-reading API in Graph
v1.0 is a POST**, and this server routes POSTs through `graph_write`, which is
gated as a mutation and disabled by default. A label check is semantically a read.
It should not go through the write path, and it must not be something a caller
can decline to perform — so it belongs in neither existing tool. It wants a
separate internal call path in `_execute`, invoked by the server rather than
exposed as a tool at all.

## A recommended design for this codebase

Layered, because no single mechanism covers the surface:

1. **Enforce at `graph_get`, before the call.** `shaping.py` is the cheap place
   and the wrong one — by then the data is already in the process. The defensible
   boundary is refusing to make the request, or refusing to return its result.
2. **Baseline: the EXTRACT check** (C). It is the faithful port of what Copilot
   does, needs no extra licence, and is the right semantics for any label that
   applies encryption.
3. **Classification-only labels: prefer `processContent`** (B) where the tenant
   will do the Purview-side setup, so policy lives with the compliance team. Fall
   back to a local GUID denylist resolved from
   `/security/dataSecurityAndGovernance/sensitivityLabels` where they will not.
4. **Use Retrieval (A) only if the deployment is SharePoint/OneDrive-centric and
   already licensed for Copilot.** Treat it as an additional, better-grounded
   tool — not as a replacement for `graph_get`.
5. **Fail closed, and be honest about the cost.** An item whose label cannot be
   determined — `423 Locked`, an unsupported file type, a `processContent` error,
   a mail item whose extended properties were not expanded — must be treated as
   labeled. Otherwise the control is theatre: every failure mode becomes a bypass.
   This *will* withhold unlabeled content, and users will notice. Say so in the
   tool's error text rather than letting it look like a bug.
6. **Do not put the label check in the scope profile.** `SCOPE.md` is explicit
   that layer 1 is a usefulness decision, not a security boundary. A control
   asserted there would repeat the exact mistake that document warns about.

## Honest coverage assessment

Even fully built, this is partial:

- **Mail is the weak spot.** Retrieval does not cover Exchange; extended-property
  expansion is clunky and per-item. Mail is also where this server's profile
  points first.
- **Teams chat has no label read path at all** in v1.0.
- **Subjects, filenames and paths leak regardless.** Labels protect content;
  metadata is returned in the clear by every one of these APIs. A subject line
  reading "Payroll: national ID for J. Lind" is disclosed even when the body is
  withheld — as demonstrated in `tests/test_sensitivity_labels.py`.
- **Latency and cost are per-item**, and the most natural MCP operation is a list.

None of that argues against building it. It argues for scoping the claim
precisely: "bodies of labeled Office files and mail are withheld" is defensible;
"labeled data is never retrieved" would not be.

## Sources

- [DLP for Microsoft 365 Copilot](https://learn.microsoft.com/en-us/purview/dlp-microsoft365-copilot-location-learn-about) — the `Personal` label worked example
- [Purview data security for Copilot](https://learn.microsoft.com/en-us/purview/ai-microsoft-purview) — EXTRACT/VIEW usage rights
- [Copilot Retrieval API](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/api/ai-services/retrieval/copilotroot-retrieval) — `filterExpression`, `InformationProtectionLabelId`
- [Copilot APIs overview](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/copilot-apis-overview) — licensing
- [userDataSecurityAndGovernance: processContent](https://learn.microsoft.com/en-us/graph/api/userdatasecurityandgovernance-processcontent?view=graph-rest-1.0)
- [Purview data security and governance overview](https://learn.microsoft.com/en-us/graph/security-datasecurityandgovernance-overview)
- [driveItem: extractSensitivityLabels](https://learn.microsoft.com/en-us/graph/api/driveitem-extractsensitivitylabels?view=graph-rest-1.0)
- [sensitivityLabel: computeRightsAndInheritance](https://learn.microsoft.com/en-us/graph/api/sensitivitylabel-computerightsandinheritance?view=graph-rest-1.0)
- [List sensitivityLabels](https://learn.microsoft.com/en-us/graph/api/tenantdatasecurityandgovernance-list-sensitivitylabels?view=graph-rest-1.0)
