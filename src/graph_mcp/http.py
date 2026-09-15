"""Remote hosting over Streamable HTTP.

The server runs one way -- as an HTTP endpoint an MCP client connects to -- but
it answers to two different deployments, and they differ in *who it acts as*:

**Shared host.** One process serving many users, each authenticated by their
own bearer token, with the server acting as an OAuth 2.1 **resource server**.
It does not issue tokens and never handles credentials: it validates what
arrives and acts on behalf of that user. This is what `--resource-url` selects,
and what the isolation guarantees in `caller.py` are for.

    python -m graph_mcp.http --host 0.0.0.0 --port 8000 \\
        --resource-url https://graph-mcp.example.com

**Single operator.** One person, one app registration, a server on their own
machine that their MCP client calls. The server holds one Entra credential and
acts as whoever signed in with it. Set the three `AZURE_*` variables and it
runs; see `graph_mcp.azure` for what they can and cannot express.

    AZURE_TENANT_ID=... AZURE_CLIENT_ID=... AZURE_CLIENT_SECRET=... \\
        python -m graph_mcp.http

With neither, it serves the fixture tenant, which needs no credentials at all.

The two must not be combined, and this entry point refuses to: a server holding
one credential cannot honour another user's token, so every authenticated
caller would silently act as the operator.

This entry point also sets up:

1. **Token verification** for resource-server mode. A `TokenVerifier` turns a
   bearer token into a verified identity plus scopes.
   `AuthSettings.resource_server_url` makes the server publish RFC 9728
   protected-resource metadata, so clients can discover where to authenticate,
   and `validate_token_resource` enforces the audience so a token minted for
   another API cannot be replayed here.
2. **Transport security.** MCP servers over HTTP must validate `Origin` to
   defeat DNS-rebinding attacks from a browser on the user's machine. Bind to
   localhost during development; name your real hosts in production.
3. **Warmup and sign-in.** The embedding model loads, and any interactive
   sign-in happens, before the first request rather than during it.

Session state: pagination cursors live in this process, so run either with
sticky sessions or with `--stateless`, which disables cursors rather than
handing a caller another replica's results. See docs/DEPLOYMENT.md.
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import replace

from graph_mcp.runtime import build_runtime
from graph_mcp.server import create_server

logger = logging.getLogger("graph_mcp.http")

LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}


def build_auth(resource_url: str | None, issuer: str | None, required_scopes: list[str]):
    """AuthSettings plus a TokenVerifier, or (None, None) for unauthenticated dev.

    The verifier is the integration point: it must validate the token's
    signature, issuer, audience and expiry against your identity provider, and
    return the scopes it carries. A placeholder that trusts the token would
    make every isolation guarantee in this server meaningless, so there is no
    default implementation -- wire `GRAPH_MCP_TOKEN_VERIFIER` to yours.
    """
    if not resource_url:
        return None, None

    from mcp.server.auth.settings import AuthSettings

    verifier = _load_token_verifier()
    if verifier is None:
        raise SystemExit(
            "Authentication is enabled (--resource-url) but no token verifier is "
            "configured. Set GRAPH_MCP_TOKEN_VERIFIER to an import path such as "
            "'yourorg.auth:EntraTokenVerifier' implementing mcp TokenVerifier.\n"
            "Refusing to start: accepting unverified bearer tokens would let any "
            "caller act as any user."
        )

    settings = AuthSettings(
        issuer_url=issuer or resource_url,
        resource_server_url=resource_url,
        required_scopes=required_scopes or None,
        # Reject tokens whose audience is not this resource, so a token issued
        # for a different API cannot be replayed against this one.
        validate_token_resource=True,
    )
    return settings, verifier


def _load_token_verifier():
    spec = os.environ.get("GRAPH_MCP_TOKEN_VERIFIER", "")
    if not spec:
        return None
    module_name, _, attr = spec.partition(":")
    if not attr:
        raise SystemExit(
            f"GRAPH_MCP_TOKEN_VERIFIER must look like 'module:Class', got '{spec}'."
        )
    import importlib

    factory = getattr(importlib.import_module(module_name), attr)
    return factory()


def build_graph_backend(choice: str, *, authenticated: bool, host: str):
    """Decide what the server talks to, and refuse the unsafe combinations.

    Returns `(credential, transport_factory)`; both are None for the fixture
    tenant, which is the default when no Azure credentials are configured.
    """
    from graph_mcp.azure import (
        GRAPH_RESOURCE,
        AzureApp,
        AzureAuthError,
        build_transport_factory,
        credential_from_env,
    )

    if choice == "fixture":
        return None, None

    try:
        app = AzureApp.from_env()
    except AzureAuthError as exc:
        raise SystemExit(str(exc)) from exc

    if app is None:
        if choice == "azure":
            raise SystemExit(
                "--graph azure needs an app registration. Set AZURE_TENANT_ID, "
                "AZURE_CLIENT_ID and AZURE_CLIENT_SECRET, or drop the flag to "
                "run against the fixture tenant."
            )
        return None, None

    # One credential means one identity. A server that also authenticates
    # remote users would hand each of them the operator's access to Graph --
    # the exact failure the per-request Caller exists to prevent.
    if authenticated:
        raise SystemExit(
            "Refusing to start: AZURE_* credentials and --resource-url are both "
            "set. This server would authenticate each caller and then act as the "
            "one identity these credentials hold, so every user would read the "
            "operator's mail. Pick one:\n"
            "  * single operator -- keep AZURE_*, drop --resource-url\n"
            "  * shared host     -- drop AZURE_*, wire a per-caller transport "
            "factory (docs/DEPLOYMENT.md)"
        )

    # Unauthenticated plus a real credential means anything that can reach the
    # port can read the operator's tenant data.
    if host not in LOOPBACK and os.environ.get("GRAPH_MCP_ALLOW_REMOTE_BIND", "") not in ("1", "true", "yes"):
        raise SystemExit(
            f"Refusing to bind {host} with Azure credentials and no "
            "authentication: anything that can reach the port could read this "
            "tenant as the signed-in user. Bind 127.0.0.1, or set "
            "GRAPH_MCP_ALLOW_REMOTE_BIND=1 if something in front of it "
            "authenticates."
        )

    try:
        credential = credential_from_env(app)
    except AzureAuthError as exc:
        raise SystemExit(str(exc)) from exc

    act_as_user = (os.environ.get("GRAPH_MCP_ACT_AS_USER") or "").strip() or None
    # Graph lives at a different host in the sovereign clouds, and pairs with
    # AZURE_AUTHORITY_HOST -- both have to move together or the token is minted
    # for the wrong resource.
    base_url = (os.environ.get("GRAPH_MCP_GRAPH_BASE_URL") or "").strip() or GRAPH_RESOURCE
    if credential.app_only:
        logger.warning(
            "Using client_credentials: this is an APP-ONLY token, with the app "
            "registration's application permissions across the whole tenant and "
            "no signed-in user to bound them. docs/SCOPE.md explains why the "
            "delegated flows are the design. %s",
            f"/me resolves to {act_as_user}." if act_as_user
            else "Set GRAPH_MCP_ACT_AS_USER or every /me path will fail.",
        )
    return credential, build_transport_factory(
        credential, act_as_user=act_as_user, base_url=base_url
    )


def build_transport_security(
    allowed_hosts: list[str], allowed_origins: list[str], host: str, port: int
):
    """Origin and Host allowlists.

    Required by the MCP specification for HTTP transports: without it a web
    page the user visits can drive this server through their browser.

    The Host header carries the port ("127.0.0.1:8000"), so entries without one
    do not match and every request is rejected with 421. The local defaults
    below therefore include both forms. In production, pass --allowed-host for
    each public hostname; do not rely on these.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    if not allowed_hosts:
        allowed_hosts = [
            f"{name}{suffix}"
            for name in ("127.0.0.1", "localhost", host)
            for suffix in ("", f":{port}")
        ]
    if not allowed_origins:
        allowed_origins = [
            f"{scheme}://{name}{suffix}"
            for scheme in ("http", "https")
            for name in ("127.0.0.1", "localhost", host)
            for suffix in ("", f":{port}")
        ]
    return TransportSecuritySettings(
        allowed_hosts=sorted(set(allowed_hosts)),
        allowed_origins=sorted(set(allowed_origins)),
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=os.environ.get("GRAPH_MCP_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("GRAPH_MCP_PORT", 8000)))
    ap.add_argument("--path", default="/mcp", help="Streamable HTTP endpoint path")
    ap.add_argument(
        "--graph",
        choices=["auto", "azure", "fixture"],
        default=os.environ.get("GRAPH_MCP_GRAPH", "auto"),
        help="What to talk to: real Graph with the AZURE_* credentials, the "
             "fixture tenant, or auto (azure when those are set).",
    )
    ap.add_argument(
        "--resource-url",
        default=os.environ.get("GRAPH_MCP_RESOURCE_URL"),
        help="Public URL of this server. Enables OAuth resource-server mode.",
    )
    ap.add_argument("--issuer-url", default=os.environ.get("GRAPH_MCP_ISSUER_URL"))
    ap.add_argument(
        "--required-scope", action="append", default=[],
        help="Scope a token must carry to call this server at all.",
    )
    ap.add_argument(
        "--allowed-host", action="append", default=[],
        help="Hostnames this server may be addressed by (DNS-rebinding defence).",
    )
    ap.add_argument("--allowed-origin", action="append", default=[])
    ap.add_argument(
        "--stateless", action="store_true",
        help="Serve each request independently. Scales horizontally without "
             "sticky sessions, but disables pagination cursors.",
    )
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level.upper())

    auth_settings, token_verifier = build_auth(
        args.resource_url, args.issuer_url, args.required_scope
    )
    credential, transport_factory = build_graph_backend(
        args.graph, authenticated=auth_settings is not None, host=args.host
    )

    if auth_settings is None and credential is None:
        logger.warning(
            "Starting WITHOUT authentication against the FIXTURE tenant: every "
            "request is treated as one local developer and no real data is "
            "reachable. Set AZURE_* to query a real tenant, or --resource-url "
            "to run as an OAuth resource server."
        )

    runtime = build_runtime(transport_factory=transport_factory)
    server = create_server(
        runtime, auth_settings=auth_settings, token_verifier=token_verifier
    )

    logger.info("Warming up (loading embedding model)...")
    runtime.warmup()

    async def serve() -> None:
        # Sign-in, probe and serve share one event loop: the credential's lock
        # binds to the loop it is first awaited on, and a second anyio.run()
        # would leave it bound to a loop that has already exited.
        #
        # The banner comes after, so an interactive sign-in is not preceded by
        # a line claiming the server is already serving.
        if credential is not None:
            await _start_credential(credential, runtime)

        backend = "fixture tenant" if credential is None else f"Graph ({credential.flow})"
        logger.info(
            "Serving Streamable HTTP on http://%s:%s%s (%s, %s, %s)",
            args.host, args.port, args.path,
            backend,
            "stateless" if args.stateless else "sessioned",
            "authenticated" if auth_settings else "UNAUTHENTICATED",
        )
        if auth_settings is None:
            # The one line that turns a running server into a usable one.
            logger.info(
                "Add it to an MCP client with:\n"
                "    claude mcp add --transport http graph http://%s:%s%s",
                "127.0.0.1" if args.host in LOOPBACK else args.host,
                args.port, args.path,
            )

        await server.run_streamable_http_async(
            host=args.host,
            port=args.port,
            streamable_http_path=args.path,
            stateless_http=args.stateless,
            transport_security=build_transport_security(
                args.allowed_host, args.allowed_origin, args.host, args.port
            ),
        )

    import anyio

    anyio.run(serve)


async def _start_credential(credential, runtime) -> None:
    """Sign in and probe Graph before the first tool call.

    Both belong at startup rather than on demand. A device-code prompt raised
    during an MCP request is written where nobody is looking while the request
    blocks for minutes, and a 403 on the first real question is
    indistinguishable from the question being wrong.
    """
    from graph_mcp.azure import AzureAuthError, preflight
    from graph_mcp.caller import LOCAL_CALLER

    try:
        account = await credential.sign_in()
    except AzureAuthError as exc:
        raise SystemExit(f"Azure sign-in failed.\n{exc}") from exc

    # The permissions the token really carries, so search can mark operations
    # the caller cannot make and a 403 can name what is missing. Without this
    # every search would spend a Graph round-trip asking /me the same question.
    runtime.default_caller = replace(
        LOCAL_CALLER, scopes=tuple(credential.granted_scopes)
    )

    who = account.get("user_principal_name") or account.get("display_name") or "?"
    logger.info(
        "Signed in as %s; token grants %d permission(s): %s",
        who,
        len(credential.granted_scopes),
        ", ".join(credential.granted_scopes) or "none",
    )
    for line in await preflight(credential, runtime.transport_for(LOCAL_CALLER)):
        logger.info("%s", line)


if __name__ == "__main__":
    main()
