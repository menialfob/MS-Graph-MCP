# Scope, permissions and the security boundary

**The scope profile is not a security boundary.** It decides what the retrieval
layer will *suggest*, which is a usefulness decision. Anyone treating
`end_user_helpdesk.yaml` as access control has misunderstood the system.

Three layers; only the third is a security control.

### 1. Catalog scope — what gets indexed

`config/scope_profiles/*.yaml`. Include/exclude globs over normalised path
templates, plus structural pruning. Determines what `graph_search_operations`
can propose. Changing it changes what the assistant knows about, not what
anyone is able to do.

### 2. Runtime policy — what the executor will send

The compiled route table (`routes.json`), enforced deny-by-default. An invented
path is refused with a suggestion before any call reaches Graph.

A correctness and blast-radius control — it stops hallucinated endpoints and
keeps the server inside its declared surface. Still not the security boundary,
because it runs in the same process as the thing it constrains.

### 3. Entra consent — the actual boundary

What a caller can do is bounded by the delegated scopes granted to the app
registration, and by the signed-in user's own privileges and tenant policies.

Every call runs **on behalf of the signed-in user**. A user who cannot read a
mailbox cannot read it through this server either. If layers 1 and 2 were
removed entirely, this one would still hold.

That is why the design uses delegated / on-behalf-of auth, not app-only
credentials. App-only plus an LLM is tenant-wide god-mode and discards the one
property that makes the system safe to deploy.

## The `end_user_helpdesk` profile

**Included** — `/me/**`, user lookups and their common sub-resources (messages,
mailFolders, events, calendars, calendarView, contacts, drive, presence,
manager, directReports, memberOf, photo, licenseDetails, people, todo, chats),
groups (read), sites, drives, teams, chats, planner, `/search/query`,
organization, subscribedSkus, directoryRoles (read), `/auditLogs/signIns`.

**Excluded** — `/deviceManagement`, `/deviceAppManagement`,
`/identityGovernance`, `/identity`, `/policies`, `/admin`, `/roleManagement`,
`/solutions`, `/security`, `/education`, `/print`, `/storage`,
`/informationProtection`, `/tenantRelationships`, `/contracts`, `/copilot`,
`/external`, `/reports`, `/directory`, `/servicePrincipals`, `/applications`,
`/devices`, `/invitations`, plus `**/workbook/**`, `**/subscriptions/**`,
`**/managedDevices/**` and the Teams app-catalog plumbing.

**Read-only** — `/users/**`, `/groups/**`, `/organization/**`,
`/directoryObjects/**`, `/directoryRoles/**`, `/subscribedSkus`,
`/auditLogs/**`, `/sites/**`, `/places/**`. Helpdesk can look people and groups
up; it cannot edit them from here.

### Two traps

Profile patterns use the camelCase spelling from the docs
(`/deviceManagement/**`) while normalised paths are lowercased, so the glob
matcher folds case on **both** sides. An earlier case-sensitive version made
every camelCase exclude a silent no-op — the catalog still looked plausible
because those paths also failed the include list. A scope rule that quietly does
nothing is worse than one that errors.
(`tests/test_policy.py::TestGlobs::test_matching_is_case_insensitive`)

`**` matches **zero or more** segments, so `/sites/**` covers `/sites` itself.
This differs from git pathspec semantics, and is chosen so profiles need not
list `/sites` and `/sites/**` separately.

## Writes

Allowlisted to **end-user-owned resources** — send mail, drafts, calendar
events, To Do, Planner, own files, chat messages. Helpdesk and directory
operations stay read-only regardless of the granted token.

`graph_write` is a separate tool so clients can apply a different approval
policy, and supports `dry_run` so the exact request can be reviewed first.

## Permissions data

Scopes come from the generated permission tables in the docs
(`includes/permissions/*-permissions.md`) — the OpenAPI description has no
security information at all.

**Do not blindly request the "least privileged" scope.** The upstream tables are
occasionally surprising: `GET /groups` lists
`Group-NestingSupport.ReadWrite.All` as least-privileged delegated, while the
conventional `Group.Read.All` sits in the higher-privileged column. Review the
app registration's scope set by hand.

At runtime `graph_whoami` exposes the token's scopes, and search results are
annotated with whether the caller can actually call each operation — so the
model does not propose things destined to 403.
