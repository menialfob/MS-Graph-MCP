# Running it against your own tenant

The other way to run this server — one person, one app registration, a server
on your own machine that your MCP client calls over HTTP. For the shared,
multi-user deployment see [DEPLOYMENT.md](DEPLOYMENT.md); the two modes cannot
be combined and the server refuses to start if you try.

```bash
scripts/bootstrap.sh                      # venv, sources, index

export AZURE_TENANT_ID=...
export AZURE_CLIENT_ID=...
export AZURE_CLIENT_SECRET=...
.venv/bin/python -m graph_mcp.http --port 8000

claude mcp add --transport http graph http://127.0.0.1:8000/mcp
```

With no `AZURE_*` set it serves the fixture tenant instead, which is the way to
see the tool surface without touching a directory.

## First: what those three variables can and cannot do

This is the part that costs people an afternoon, so it is worth being blunt.

Tenant + client + secret is the **client credentials** grant, and it mints an
**app-only** token. That token carries the app registration's *application*
permissions, in a `roles` claim. It has no user behind it, so:

- `/me` is not a valid path under it — and this catalog is built around `/me`;
- its reach is every mailbox and every file in the tenant at once.

**Delegated** permissions — the `scp` claim, the "Delegated" column on the API
permissions blade, what an enterprise app registration usually holds — only
ever appear in a token minted for a signed-in *user*. No client secret produces
one. If your registration's permissions are delegated, a client-credentials
token against it carries nothing at all and Graph answers **403 to every
call**. The fix is not a different secret. It is a sign-in.

So there are three flows, chosen with `GRAPH_MCP_AZURE_FLOW`:

| Flow | Acts as | Needs | When |
|---|---|---|---|
| `device_code` *(default)* | the user who signs in | *Allow public client flows* = Yes | delegated permissions, no redirect URI to register |
| `auth_code` | the user who signs in | a registered redirect URI + the secret | delegated permissions on a confidential registration |
| `client_credentials` | the application | *application* permissions + admin consent | you really do have application permissions |

Both delegated flows sign in once and cache the refresh token, so restarts are
silent. The sign-in happens at **startup**, not on the first tool call: a device
code printed in the middle of an MCP request is written where nobody is looking
while the request blocks for minutes.

## The app registration

Whichever flow, in the Entra portal under **App registrations → your app**:

**API permissions.** Add the Microsoft Graph *delegated* permissions you want
this server to be able to use, and grant consent. Start small — `User.Read`,
`Mail.Read`, `Calendars.Read`, `Files.Read`, `People.Read` covers most of what
the `end_user_helpdesk` profile proposes. The server requests `.default`, which
means "everything already consented on this registration", so the registration
*is* the permission list. [SCOPE.md](SCOPE.md) is worth reading before you
widen it.

**For `device_code`:** Authentication → Advanced settings → *Allow public
client flows* → **Yes**. That is the only change. No redirect URI, no secret
needed (set `AZURE_CLIENT_SECRET` anyway if you have it; it is unused here).

**For `auth_code`:** Authentication → Add a platform → Web → redirect URI
`http://localhost:8765/callback`, exactly. Uses `AZURE_CLIENT_SECRET`. Change
the port with `GRAPH_MCP_REDIRECT_PORT` and register the matching URI.

**For `client_credentials`:** add *Application* permissions, not delegated, and
grant admin consent. Then set `GRAPH_MCP_ACT_AS_USER` to the UPN that `/me`
should resolve to, or every `/me` path fails. The server logs a warning in this
mode and means it: an app-only token plus a language model is the whole tenant,
with none of the per-user bounding that makes the rest of this design safe.

## Running it

```bash
export AZURE_TENANT_ID=00000000-0000-0000-0000-000000000000
export AZURE_CLIENT_ID=11111111-1111-1111-1111-111111111111
export AZURE_CLIENT_SECRET='...'          # the secret *value*, not its ID

.venv/bin/python -m graph_mcp.http --port 8000
```

On a first run with `device_code` it prints the sign-in prompt and waits:

```
To sign in, use a web browser to open https://microsoft.com/devicelogin
and enter the code F7K9QX2L to authenticate.

INFO  Signed in as you@contoso.com; token grants 5 permission(s): User.Read, Mail.Read, ...
INFO  Graph reachable as you@contoso.com (GET /me -> 200).
INFO  Serving Streamable HTTP on http://127.0.0.1:8000/mcp (Graph (device_code), sessioned, UNAUTHENTICATED)
```

That last `UNAUTHENTICATED` refers to the *MCP* endpoint, not to Graph: anything
that can reach port 8000 gets to act as you. That is why the server refuses to
bind anything but loopback while it holds a credential, unless you set
`GRAPH_MCP_ALLOW_REMOTE_BIND=1` because something in front of it authenticates.

The startup probe (`GET /me`, or `/organization` when app-only) is there so a
permissions problem is reported at startup rather than surfacing as a 403 on
the first real question, where it is indistinguishable from a bad question.

## Connecting a client

```bash
claude mcp add --transport http graph http://127.0.0.1:8000/mcp
```

`.mcp.json` in the repository root already says the same thing, so a client
opened here offers the server without being told about it:

```json
{
  "mcpServers": {
    "graph": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

Change the port there if you run on another one. The server has to be running
first: it holds the credential, so the client needs no configuration beyond the
URL. Start a session with `graph_whoami` to see who the server is acting as and
which permissions the token really carries — the model uses that to avoid
proposing operations that will be denied.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AZURE_TENANT_ID` | — | Directory (tenant) ID |
| `AZURE_CLIENT_ID` | — | Application (client) ID |
| `AZURE_CLIENT_SECRET` | — | Secret *value*; unused by `device_code` |
| `AZURE_AUTHORITY_HOST` | `https://login.microsoftonline.com` | Sovereign clouds |
| `GRAPH_MCP_AZURE_FLOW` | `device_code` | `device_code`, `auth_code`, `client_credentials` |
| `GRAPH_MCP_ACT_AS_USER` | — | UPN or object id `/me` resolves to (app-only) |
| `GRAPH_MCP_GRAPH_SCOPES` | `…/.default offline_access openid profile` | Override the scope request |
| `GRAPH_MCP_GRAPH_BASE_URL` | `https://graph.microsoft.com` | Graph endpoint; pairs with the authority |
| `GRAPH_MCP_TOKEN_CACHE` | `~/.cache/graph-mcp/token-cache.json` | `none` keeps the refresh token in memory only |
| `GRAPH_MCP_REDIRECT_PORT` | `8765` | `auth_code` redirect port |
| `GRAPH_MCP_ALLOW_REMOTE_BIND` | unset | Permit a non-loopback bind with credentials held |
| `GRAPH_MCP_GRAPH` | `auto` | `azure`, `fixture`, or auto-detect from `AZURE_*` |

The token cache holds a refresh token — a bearer credential for whoever signed
in — so it is written `0600` and never contains the client secret. Delete the
file to sign in as somebody else.

## When it does not work

The server translates the AADSTS codes that matter, because Entra's own text
for them points at the wrong fix. Some you may still meet:

| What you see | What it means |
|---|---|
| `AADSTS7000218` on device code | *Allow public client flows* is off. Turn it on, or use `auth_code`. Entra's own message asks for a `client_secret`, which is not the problem. |
| `AADSTS7000215` | Wrong secret. You used the secret **ID** rather than its value, or it expired. |
| `AADSTS50011` on auth code | The redirect URI is not registered, verbatim, including the port. |
| `AADSTS65001` | Nobody has consented to the permissions yet. |
| "carries no application permissions" | A client-credentials token against a registration whose permissions are delegated. Use `device_code`. |
| `403` on every call, app-only | Same cause, seen from the Graph side. |
| `appOnlyTokenHasNoSignedInUser` | An app-only token met a `/me` path. Set `GRAPH_MCP_ACT_AS_USER`. |
| `421 Misdirected Request` | The `Host` header is not in the allowlist. Add `--allowed-host`; note it carries the port. |

## What this mode gives up

Everything in [DEPLOYMENT.md](DEPLOYMENT.md#multi-user-isolation) about
per-caller isolation is about the shared host. Here there is one credential and
one identity, and the per-request `Caller` exists only to keep the two modes
from being confused for each other: the transport factory refuses any caller
that is not the local one, so wiring this credential behind an authenticating
front end fails loudly instead of quietly serving everyone the operator's mail.

Otherwise everything is the same server: the same catalog, route validation,
shaping, cursors, write gating and label policy.
