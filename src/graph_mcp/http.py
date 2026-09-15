"""Remote hosting over Streamable HTTP.

This is the intended deployment: one process serving many users, each
authenticated by their own bearer token, with the server acting as an OAuth 2.1
**resource server**. It does not issue tokens and never handles credentials --
it validates what arrives and acts on behalf of that user.

    python -m graph_mcp.http --host 0.0.0.0 --port 8000

This entry point sets up three things:

1. **Token verification.** A `TokenVerifier` turns a bearer token into a
   verified identity plus scopes. `AuthSettings.resource_server_url` makes the
   server publish RFC 9728 protected-resource metadata, so clients can discover
   where to authenticate, and `validate_token_resource` enforces the audience
   so a token minted for another API cannot be replayed here.
2. **Transport security.** MCP servers over HTTP must validate `Origin` to
   defeat DNS-rebinding attacks from a browser on the user's machine. Bind to
   localhost during development; name your real hosts in production.
3. **Warmup.** The embedding model is loaded before the first request rather
   than by whichever user happens to arrive first.

Session state: pagination cursors live in this process, so run either with
sticky sessions or with `--stateless`, which disables cursors rather than
handing a caller another replica's results. See docs/DEPLOYMENT.md.

This is the only way to run the server.
"""

from __future__ import annotations

import argparse
import logging
import os

from graph_mcp.runtime import build_runtime
from graph_mcp.server import create_server

logger = logging.getLogger("graph_mcp.http")


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
    if auth_settings is None:
        logger.warning(
            "Starting WITHOUT authentication: every request is treated as one "
            "local developer. Never expose this beyond localhost -- pass "
            "--resource-url to run as an OAuth resource server."
        )

    runtime = build_runtime()
    server = create_server(
        runtime, auth_settings=auth_settings, token_verifier=token_verifier
    )

    logger.info("Warming up (loading embedding model)...")
    runtime.warmup()

    logger.info(
        "Serving Streamable HTTP on http://%s:%s%s (%s, %s)",
        args.host, args.port, args.path,
        "stateless" if args.stateless else "sessioned",
        "authenticated" if auth_settings else "UNAUTHENTICATED",
    )
    import anyio

    anyio.run(
        lambda: server.run_streamable_http_async(
            host=args.host,
            port=args.port,
            streamable_http_path=args.path,
            stateless_http=args.stateless,
            transport_security=build_transport_security(
                args.allowed_host, args.allowed_origin, args.host, args.port
            ),
        )
    )


if __name__ == "__main__":
    main()
