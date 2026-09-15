# Scope, permissions and the security boundary

## Read this first

**The scope profile is not a security boundary.** It decides what the retrieval
layer will *suggest*, which is a usefulness decision. Anyone who treats
`end_user_helpdesk.yaml` as an access-control policy has misunderstood the
system.

There are three layers, and only the third is a security control.

### 1. Catalog scope — what gets indexed

`config/scope_profiles/*.yaml`. Declarative include/exclude globs over
normalised path templates, plus structural pruning. Determines what
`graph_search_operations` can propose.

Changing this file changes what the assistant knows about. It does not change
what anyone is able to do.

### 2. Runtime policy — what the executor will send

The compiled route table (`routes.json`), enforced deny-by-default. A path the
model invents that is not in the table is refused with a structured error and a
suggestion, before any call reaches Graph.

This is a correctness and blast-radius control — it stops hallucinated
endpoints and keeps the server inside its declared surface. It is still not the
security boundary, because it lives in the same process as the thing it
constrains.

### 3. Entra consent — the actual boundary

What a caller can do is bounded by:

- the delegated scopes granted to the app registration, and
- the signed-in user's own privileges and the tenant's policies.

Every call runs **on behalf of the signed-in user**. A user who cannot read a
mailbox cannot read it through this server either. If layers 1 and 2 were
removed entirely, this one would still hold.

That is why the design uses delegated / on-behalf-of auth and not app-only
credentials. App-only plus an LLM is tenant-wide god-mode and discards the
single property that makes the system safe to deploy.

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

### A trap worth knowing about

Profile patterns are written in the camelCase spelling from the docs
(`/deviceManagement/**`) while normalised paths are lowercased. The glob
matcher folds case on **both** sides for exactly this reason. An earlier
case-sensitive version made every camelCase exclude a silent no-op — the
catalog still looked plausible because those paths also failed the include
list. A scope rule that quietly does nothing is worse than one that errors.

`tests/test_policy.py::TestGlobs::test_matching_is_case_insensitive` guards it.

Note also that `**` matches **zero or more** segments here, so `/sites/**`
covers `/sites` itself. This differs from git pathspec semantics and is chosen
so profiles do not have to list `/sites` and `/sites/**` separately.

## Writes

Writes are allowlisted to **end-user-owned resources** — send mail, drafts,
calendar events, To Do, Planner, own files, chat messages. Helpdesk and
directory operations stay read-only regardless of the granted token.

The write path is a separate tool (`graph_write`) so MCP clients can apply a
different approval policy to it, and it supports `dry_run` so the exact request
can be reviewed before it is sent.

## Permissions data

Scopes come from the generated permission tables in the docs
(`includes/permissions/*-permissions.md`) — the OpenAPI description has no
security information whatsoever.

**Do not blindly request the "least privileged" scope.** The upstream tables
are occasionally surprising: `GET /groups` lists
`Group-NestingSupport.ReadWrite.All` as least-privileged delegated, while the
conventional `Group.Read.All` sits in the higher-privileged column. Review the
scope set for the app registration by hand.

At runtime, `graph_whoami` exposes the token's `scp` claim, and search results
are annotated with whether the caller can actually call each operation — so the
model does not propose things destined to 403.

## Auditing

Every call should be logged with the user oid, operation id, path, status,
latency and byte count, and carry a `client-request-id` that correlates to the
tenant's Microsoft Graph activity logs.
