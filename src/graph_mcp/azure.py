"""Signing in to Microsoft Graph with an Entra app registration.

`HttpGraphTransport` deliberately does not acquire tokens: it takes a
``TokenProvider`` and leaves identity to the deployment. This module is that
piece for the smallest useful deployment -- one operator, one app registration,
three environment variables, a server on localhost that an MCP client calls:

    AZURE_TENANT_ID   AZURE_CLIENT_ID   AZURE_CLIENT_SECRET

Those three do not by themselves say *who the server acts as*, and that is the
whole difficulty, so it is worth being explicit about it:

* **Client credentials** (tenant + client + secret, no human) mint an *app-only*
  token. It carries the registration's **application** permissions, in a `roles`
  claim. It has no signed-in user, so `/me` is not a valid path under it, and
  its reach is every mailbox and every file in the tenant at once.
* **Delegated** permissions -- the `scp` claim, and what an enterprise app
  registration usually holds -- appear only in a token minted for a signed-in
  *user*. No client secret produces one on its own. Somebody signs in, once.

So an app registration that holds delegated permissions cannot be used
non-interactively, however many secrets it has: a client-credentials token
against it carries no `roles` and Graph answers 403 to everything. The fix is
not a different secret, it is a sign-in. Two flows do that here and both keep
`AZURE_*` as the only configuration:

* ``device_code`` (default) -- prints a code, you sign in on any device. Needs
  *Allow public client flows* = Yes on the registration. No redirect URI.
* ``auth_code`` -- opens a browser against a local redirect. Needs
  ``http://localhost:8765/callback`` registered as a redirect URI, and uses the
  client secret. The flow a confidential registration is already shaped for.

Both mint a refresh token, cached at ``GRAPH_MCP_TOKEN_CACHE``, so the sign-in
is once per machine rather than once per restart.

``client_credentials`` is implemented too, because some registrations really do
hold application permissions, but it is never the default and it is not the
design this server was built around -- see docs/SCOPE.md.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import sys
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from graph_mcp.caller import LOCAL_CALLER, Caller
from graph_mcp.graph.transport import GraphTransport, HttpGraphTransport

logger = logging.getLogger("graph_mcp.azure")

GRAPH_RESOURCE = "https://graph.microsoft.com"
DEFAULT_AUTHORITY = "https://login.microsoftonline.com"
DEFAULT_REDIRECT_PORT = 8765

FLOWS = ("device_code", "auth_code", "client_credentials")

# `.default` asks for every permission already consented on the registration,
# which is what "use the permissions my app reg has" means and avoids keeping a
# scope list in two places. openid/profile/offline_access may accompany it;
# other resource scopes may not.
DELEGATED_SCOPES = f"{GRAPH_RESOURCE}/.default offline_access openid profile"
APP_ONLY_SCOPES = f"{GRAPH_RESOURCE}/.default"

# Seconds added to the device-code poll interval each time Entra answers
# `slow_down`. Polling faster than it asks gets the flow rejected outright.
SLOW_DOWN_INCREMENT = 5.0

# Refresh this far ahead of expiry. Entra access tokens live 60-90 minutes, so
# a five-minute margin costs nothing and keeps a long tool call from crossing
# the boundary mid-request.
EXPIRY_SKEW = 300.0

# What each AADSTS code actually means for *this* setup. Entra's own text is
# accurate and unactionable ("The request body must contain the following
# parameter"), which sends people to the wrong fix.
_HINTS: dict[str, str] = {
    "AADSTS7000218": (
        "The app registration is not enabled for public client flows, which the "
        "device_code flow requires. Either set Authentication -> Advanced "
        "settings -> Allow public client flows = Yes, or switch to "
        "GRAPH_MCP_AZURE_FLOW=auth_code and register the redirect URI."
    ),
    "AADSTS7000215": (
        "AZURE_CLIENT_SECRET is wrong or expired. Use the secret *value* shown "
        "once at creation, not the secret ID."
    ),
    "AADSTS700016": (
        "No app registration with this AZURE_CLIENT_ID exists in this tenant. "
        "Check AZURE_CLIENT_ID and AZURE_TENANT_ID belong together."
    ),
    "AADSTS900023": "AZURE_TENANT_ID is not a tenant this authority knows.",
    "AADSTS50011": (
        "The redirect URI is not registered. Add it verbatim under "
        "Authentication -> Web -> Redirect URIs on the app registration."
    ),
    "AADSTS65001": (
        "Nobody has consented to these permissions yet. An administrator can "
        "grant consent on the registration's API permissions blade."
    ),
    "AADSTS500011": (
        "The resource principal was not found in the tenant -- usually a typo "
        "in the scope, or Graph is not provisioned for this tenant."
    ),
    "AADSTS1002012": (
        "`.default` cannot be combined with individual scopes for the same "
        "resource. Set GRAPH_MCP_GRAPH_SCOPES to one form or the other."
    ),
}


class AzureAuthError(RuntimeError):
    """A sign-in failure, with the next concrete step where one is known."""

    def __init__(self, message: str, *, hint: str = ""):
        super().__init__(f"{message}\n{hint}" if hint else message)
        self.hint = hint


# --------------------------------------------------------------------------
# App registration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AzureApp:
    """The app registration, from the environment."""

    tenant_id: str
    client_id: str
    client_secret: str | None = None
    authority: str = DEFAULT_AUTHORITY

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AzureApp | None":
        """The configured registration, or None when nothing is configured.

        A *partial* configuration is an error rather than a silent fall back to
        fixtures: someone who set two of the three variables meant to reach a
        real tenant, and a server that quietly answers from a fixture instead
        is worse than one that refuses to start.
        """
        env = os.environ if env is None else env
        tenant = (env.get("AZURE_TENANT_ID") or "").strip()
        client = (env.get("AZURE_CLIENT_ID") or "").strip()
        secret = (env.get("AZURE_CLIENT_SECRET") or "").strip() or None

        if not (tenant or client or secret):
            return None
        missing = [
            name
            for name, value in (("AZURE_TENANT_ID", tenant), ("AZURE_CLIENT_ID", client))
            if not value
        ]
        if missing:
            raise AzureAuthError(
                "Azure credentials are only partly configured; missing "
                + ", ".join(missing) + ".",
                hint="Set AZURE_TENANT_ID and AZURE_CLIENT_ID (plus "
                "AZURE_CLIENT_SECRET for the auth_code and client_credentials "
                "flows), or unset all three to run against the fixture tenant.",
            )
        authority = (env.get("AZURE_AUTHORITY_HOST") or DEFAULT_AUTHORITY).rstrip("/")
        return cls(tenant, client, secret, authority)

    @property
    def _base(self) -> str:
        return f"{self.authority}/{self.tenant_id}/oauth2/v2.0"

    @property
    def token_endpoint(self) -> str:
        return f"{self._base}/token"

    @property
    def device_code_endpoint(self) -> str:
        return f"{self._base}/devicecode"

    @property
    def authorize_endpoint(self) -> str:
        return f"{self._base}/authorize"


# --------------------------------------------------------------------------
# Refresh-token cache
# --------------------------------------------------------------------------


@dataclass
class TokenCache:
    """Where the refresh token survives a restart.

    A refresh token is a bearer credential for the user who signed in, so the
    file is written 0600 and never holds the client secret. Set
    ``GRAPH_MCP_TOKEN_CACHE=none`` to keep it in memory only, at the cost of
    signing in again on every start.
    """

    path: Path | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "TokenCache":
        env = os.environ if env is None else env
        setting = (env.get("GRAPH_MCP_TOKEN_CACHE") or "").strip()
        if setting.lower() in ("none", "off", "0", "false"):
            return cls(None)
        if setting:
            return cls(Path(setting).expanduser())
        cache_home = env.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        return cls(Path(cache_home).expanduser() / "graph-mcp" / "token-cache.json")

    def _all(self) -> dict[str, Any]:
        if self.path is None or not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A corrupt cache is a sign-in, not a crash.
            logger.warning("Ignoring unreadable token cache at %s", self.path)
            return {}
        return data if isinstance(data, dict) else {}

    def read(self, key: str) -> dict[str, Any]:
        entry = self._all().get(key)
        return entry if isinstance(entry, dict) else {}

    def write(self, key: str, entry: dict[str, Any]) -> None:
        if self.path is None:
            return
        data = self._all()
        data[key] = entry
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".part")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self.path)
        except OSError as exc:  # noqa: BLE001 - caching is best-effort
            logger.warning("Could not write token cache at %s: %s", self.path, exc)

    def clear(self, key: str) -> None:
        if self.path is None:
            return
        data = self._all()
        if data.pop(key, None) is not None:
            try:
                self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")
                os.chmod(self.path, 0o600)
            except OSError:  # pragma: no cover - best-effort
                pass


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------


@dataclass
class AcquiredToken:
    access_token: str
    expires_at: float
    scopes: tuple[str, ...] = ()
    refresh_token: str | None = None

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.expires_at - EXPIRY_SKEW


def decode_claims(token: str) -> dict[str, Any]:
    """Best-effort read of a JWT payload.

    This is *not* validation and must never be used as such. The token was just
    minted by Entra for this process; the claims are read only to report who
    the server is acting as and which permissions came back, so a token that
    does not parse degrades the banner rather than failing the request.
    """
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:  # noqa: BLE001 - diagnostics only
        return {}
    return claims if isinstance(claims, dict) else {}


def _scopes_from(payload: dict[str, Any], access_token: str) -> tuple[str, ...]:
    """Permissions the token actually carries.

    Delegated flows get them from the response's `scope`, which is what Entra
    granted rather than what was asked for. App-only tokens have no `scope`, so
    the `roles` claim is the only place the application permissions appear.
    """
    granted = payload.get("scope")
    if isinstance(granted, str) and granted.strip():
        return tuple(granted.split())
    claims = decode_claims(access_token)
    scp = claims.get("scp")
    if isinstance(scp, str) and scp.strip():
        return tuple(scp.split())
    roles = claims.get("roles")
    if isinstance(roles, list):
        return tuple(str(r) for r in roles)
    return ()


def _as_float(value: Any, default: float) -> float:
    """A number Entra sent, or the default when it sent nothing usable."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _raise_for_error(payload: dict[str, Any]) -> None:
    code = str(payload.get("error", ""))
    if not code:
        return
    description = str(payload.get("error_description", "")).strip()
    hint = ""
    for aadsts, advice in _HINTS.items():
        if aadsts in description:
            hint = advice
            break
    # Entra's description is a multi-line block with a trace id and timestamp;
    # the first line is the part a person needs.
    first_line = description.splitlines()[0] if description else code
    raise AzureAuthError(f"Entra rejected the sign-in ({code}): {first_line}", hint=hint)


# --------------------------------------------------------------------------
# Credential
# --------------------------------------------------------------------------


class EntraCredential:
    """Acquires and refreshes a Graph access token for one identity.

    One credential means one identity, which is why this belongs to a
    single-operator deployment only. A multi-user host mints a token per
    request from the caller's own token instead; see docs/DEPLOYMENT.md.
    """

    def __init__(
        self,
        app: AzureApp,
        *,
        flow: str = "device_code",
        scopes: str | None = None,
        cache: TokenCache | None = None,
        redirect_port: int = DEFAULT_REDIRECT_PORT,
        announce: Callable[[str], None] | None = None,
        open_browser: bool = True,
    ):
        if flow not in FLOWS:
            raise AzureAuthError(
                f"Unknown flow '{flow}'. Choose one of: {', '.join(FLOWS)}."
            )
        if flow in ("auth_code", "client_credentials") and not app.client_secret:
            raise AzureAuthError(
                f"The {flow} flow needs a client secret.",
                hint="Set AZURE_CLIENT_SECRET, or use GRAPH_MCP_AZURE_FLOW="
                "device_code, which does not need one.",
            )
        self.app = app
        self.flow = flow
        self.scopes = scopes or (
            APP_ONLY_SCOPES if flow == "client_credentials" else DELEGATED_SCOPES
        )
        self.cache = cache if cache is not None else TokenCache.from_env()
        self.redirect_port = redirect_port
        # Device codes and sign-in URLs go to stderr, not stdout: stdout may be
        # carrying protocol traffic, and a prompt written into it would corrupt
        # the stream.
        self.announce = announce or (lambda text: print(text, file=sys.stderr, flush=True))
        self.open_browser = open_browser
        self._token: AcquiredToken | None = None
        self._lock = asyncio.Lock()

    # ---- public surface ---------------------------------------------

    @property
    def app_only(self) -> bool:
        return self.flow == "client_credentials"

    @property
    def cache_key(self) -> str:
        return f"{self.app.tenant_id}:{self.app.client_id}:{self.flow}"

    @property
    def granted_scopes(self) -> tuple[str, ...]:
        return self._token.scopes if self._token else ()

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.redirect_port}/callback"

    def account(self) -> dict[str, Any]:
        """Who the current token belongs to, for the banner and graph_whoami."""
        if self._token is None:
            return {}
        claims = decode_claims(self._token.access_token)
        if self.app_only:
            return {
                "id": str(claims.get("oid", "")),
                "display_name": str(claims.get("app_displayname", "")) or self.app.client_id,
                "user_principal_name": "",
            }
        return {
            "id": str(claims.get("oid", "")),
            "display_name": str(claims.get("name", "")),
            "user_principal_name": str(
                claims.get("upn") or claims.get("preferred_username") or ""
            ),
        }

    async def token(self) -> str:
        """A valid access token, refreshing or signing in as needed."""
        async with self._lock:
            if self._token is not None and not self._token.expired():
                return self._token.access_token
            await self._acquire()
            assert self._token is not None
            return self._token.access_token

    async def sign_in(self) -> dict[str, Any]:
        """Acquire a token now, interactively if that is what the flow needs.

        Called at startup rather than on the first tool call: a device-code
        prompt that appears in the middle of an MCP request is invisible to the
        person who has to act on it, and the request blocks for minutes while
        nobody sees why.
        """
        async with self._lock:
            if self._token is None or self._token.expired():
                await self._acquire()
        return self.account()

    # ---- acquisition ------------------------------------------------

    async def _acquire(self) -> None:
        if self.app_only:
            await self._client_credentials()
            return

        cached = self._token.refresh_token if self._token else None
        cached = cached or self.cache.read(self.cache_key).get("refresh_token")
        if cached:
            try:
                await self._refresh(str(cached))
                return
            except AzureAuthError as exc:
                # A revoked, expired or superseded refresh token is a sign-in,
                # not a failure -- but say so, or the prompt looks unprompted.
                logger.info("Cached sign-in is no longer usable (%s); signing in again.", exc)
                self.cache.clear(self.cache_key)

        if self.flow == "device_code":
            await self._device_code()
        else:
            await self._auth_code()

    async def _post_token(self, data: dict[str, str]) -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                self.app.token_endpoint,
                data=data,
                headers={"Accept": "application/json"},
            )
        try:
            payload = response.json()
        except ValueError:
            raise AzureAuthError(
                f"Entra returned a non-JSON response ({response.status_code}) from "
                f"{self.app.token_endpoint}."
            ) from None
        if not isinstance(payload, dict):
            raise AzureAuthError("Entra returned an unexpected token response.")
        return payload

    def _store(self, payload: dict[str, Any]) -> None:
        _raise_for_error(payload)
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise AzureAuthError("Entra returned no access token.")
        refresh_token = payload.get("refresh_token")
        self._token = AcquiredToken(
            access_token=access_token,
            expires_at=time.time() + _as_float(payload.get("expires_in"), 3600.0),
            scopes=_scopes_from(payload, access_token),
            refresh_token=refresh_token if isinstance(refresh_token, str) else None,
        )
        if self._token.refresh_token:
            self.cache.write(
                self.cache_key,
                {
                    "refresh_token": self._token.refresh_token,
                    "scopes": list(self._token.scopes),
                    "account": self.account(),
                    "saved_at": int(time.time()),
                },
            )

    async def _client_credentials(self) -> None:
        self._store(await self._post_token({
            "grant_type": "client_credentials",
            "client_id": self.app.client_id,
            "client_secret": self.app.client_secret or "",
            "scope": self.scopes,
        }))
        if not self.granted_scopes:
            raise AzureAuthError(
                "The client-credentials token carries no application permissions.",
                hint="This registration's permissions are delegated, which a "
                "client secret alone cannot use -- Graph will answer 403 to "
                "every call. Use GRAPH_MCP_AZURE_FLOW=device_code (or "
                "auth_code) so a user signs in, or grant application "
                "permissions and admin consent on the registration.",
            )

    async def _refresh(self, refresh_token: str) -> None:
        data = {
            "grant_type": "refresh_token",
            "client_id": self.app.client_id,
            "refresh_token": refresh_token,
            "scope": self.scopes,
        }
        # A refresh token minted for a confidential client must be redeemed by
        # one; a public-client (device_code) registration has no secret to send.
        if self.app.client_secret and self.flow == "auth_code":
            data["client_secret"] = self.app.client_secret
        self._store(await self._post_token(data))

    async def _start_device_code(self) -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                self.app.device_code_endpoint,
                data={"client_id": self.app.client_id, "scope": self.scopes},
                headers={"Accept": "application/json"},
            )
        try:
            started = response.json()
        except ValueError:
            raise AzureAuthError(
                f"Entra returned a non-JSON response ({response.status_code}) from "
                f"{self.app.device_code_endpoint}."
            ) from None
        if not isinstance(started, dict):
            raise AzureAuthError("Entra returned an unexpected device-code response.")
        _raise_for_error(started)
        return started

    async def _device_code(self) -> None:
        started = await self._start_device_code()

        device_code = str(started.get("device_code", ""))
        if not device_code:
            raise AzureAuthError("Entra returned no device code.")
        # Entra composes its own instruction text, including the shortened
        # verification URL; prefer it over reassembling one from the parts.
        message = str(started.get("message") or "").strip()
        if not message:
            message = (
                f"Sign in at {started.get('verification_uri')} and enter the "
                f"code {started.get('user_code')}."
            )
        self.announce(f"\n{message}\n")

        # `x or default` would turn a legitimate interval of 0 into the
        # default, so read the keys rather than their truthiness.
        interval = _as_float(started.get("interval"), 5.0)
        deadline = time.time() + _as_float(started.get("expires_in"), 900.0)
        while True:
            await asyncio.sleep(interval)
            if time.time() > deadline:
                raise AzureAuthError(
                    "The device code expired before the sign-in completed.",
                    hint="Start the server again and complete the sign-in within "
                    "the time the prompt allows.",
                )
            payload = await self._post_token({
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": self.app.client_id,
                "device_code": device_code,
            })
            error = str(payload.get("error", ""))
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += SLOW_DOWN_INCREMENT
                continue
            if error == "authorization_declined":
                raise AzureAuthError("The sign-in was declined.")
            self._store(payload)
            self.announce("Signed in.\n")
            return

    async def _auth_code(self) -> None:
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        state = secrets.token_urlsafe(16)
        from urllib.parse import urlencode

        url = self.app.authorize_endpoint + "?" + urlencode({
            "client_id": self.app.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "response_mode": "query",
            "scope": self.scopes,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })

        self.announce(
            f"\nSign in to Microsoft Graph:\n  {url}\n\n"
            f"(waiting for the redirect to {self.redirect_uri})\n"
        )
        if self.open_browser:
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001 - headless is normal
                pass

        code = await self._await_redirect(state)
        self._store(await self._post_token({
            "grant_type": "authorization_code",
            "client_id": self.app.client_id,
            "client_secret": self.app.client_secret or "",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": verifier,
        }))
        self.announce("Signed in.\n")

    async def _await_redirect(self, state: str) -> str:
        """Serve exactly one request on the redirect port and read the code."""
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from urllib.parse import parse_qs, urlparse

        captured: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's contract
                captured.update(
                    {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                )
                body = b"Signed in. You can close this tab and return to the terminal."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # noqa: D102 - silence stdout logging
                pass

        def serve_once() -> None:
            with HTTPServer(("127.0.0.1", self.redirect_port), Handler) as httpd:
                httpd.timeout = 300
                httpd.handle_request()

        try:
            await asyncio.to_thread(serve_once)
        except OSError as exc:
            raise AzureAuthError(
                f"Could not listen on {self.redirect_uri}: {exc}",
                hint="Choose a free port with GRAPH_MCP_REDIRECT_PORT and "
                "register the matching redirect URI on the app registration.",
            ) from exc

        if error := captured.get("error"):
            _raise_for_error({
                "error": error,
                "error_description": captured.get("error_description", ""),
            })
        if captured.get("state") != state:
            raise AzureAuthError(
                "The redirect carried the wrong state value; the sign-in was not "
                "the one this server started."
            )
        code = captured.get("code")
        if not code:
            raise AzureAuthError(
                "The redirect carried no authorization code.",
                hint="This usually means the sign-in timed out. Start again.",
            )
        return code


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def credential_from_env(
    app: AzureApp, env: Mapping[str, str] | None = None, **kwargs: Any
) -> EntraCredential:
    env = os.environ if env is None else env
    port = env.get("GRAPH_MCP_REDIRECT_PORT")
    return EntraCredential(
        app,
        flow=(env.get("GRAPH_MCP_AZURE_FLOW") or "device_code").strip(),
        scopes=(env.get("GRAPH_MCP_GRAPH_SCOPES") or "").strip() or None,
        redirect_port=int(port) if port else DEFAULT_REDIRECT_PORT,
        **kwargs,
    )


def build_transport_factory(
    credential: EntraCredential,
    *,
    act_as_user: str | None = None,
    base_url: str = GRAPH_RESOURCE,
    expected_subject: str = LOCAL_CALLER.subject,
):
    """A TransportFactory that acts as the identity this credential holds.

    Every caller gets the *same* identity, which is only ever correct for a
    single-operator server. Handing this factory to a server that authenticates
    remote users would make each of them act as whoever signed in here, so the
    factory refuses any caller other than the local one rather than trusting
    the entry point to have checked.
    """

    async def token_provider() -> str:
        return await credential.token()

    async def scopes_provider() -> list[str]:
        return list(credential.granted_scopes)

    def factory(caller: Caller) -> GraphTransport:
        if caller.subject != expected_subject:
            raise RuntimeError(
                "This server holds one Azure credential and can only act as the "
                f"identity that signed in, but the request is authenticated as "
                f"'{caller.subject}'. Run without AZURE_* credentials and wire a "
                "per-caller transport factory instead (docs/DEPLOYMENT.md)."
            )
        return HttpGraphTransport(
            token_provider,
            base_url=base_url,
            scopes_provider=scopes_provider,
            act_as_user=act_as_user,
            app_only=credential.app_only,
            identity_hint=credential.account(),
        )

    return factory


async def preflight(
    credential: EntraCredential, transport: GraphTransport
) -> list[str]:
    """Prove the credentials reach Graph before anyone calls a tool.

    A 403 on the first real question is indistinguishable from a bad question.
    One probe at startup separates "the app registration is not consented" from
    "that user has no mailbox", and says which.
    """
    from graph_mcp.graph.request import GraphRequest

    probe = "/organization" if credential.app_only else "/me"
    lines: list[str] = []
    response = await transport.send(GraphRequest("GET", probe, {"$top": "1"}))
    if response.ok:
        account = credential.account()
        who = (
            account.get("user_principal_name")
            or account.get("display_name")
            or account.get("id")
            or "the app registration"
        )
        lines.append(f"Graph reachable as {who} (GET {probe} -> {response.status}).")
        return lines

    code = str(response.body.get("error", {}).get("code", response.status))
    message = str(response.body.get("error", {}).get("message", "")).splitlines()[:1]
    lines.append(f"Graph refused GET {probe}: {code} {' '.join(message)}")
    if response.status == 403 and credential.app_only:
        lines.append(
            "App-only tokens need *application* permissions with admin consent. "
            "Delegated permissions on the registration do nothing here."
        )
    elif response.status == 403:
        lines.append(
            "The signed-in user or the registration lacks consent for this scope. "
            "Granted: " + (", ".join(credential.granted_scopes) or "none")
        )
    elif response.status == 400 and credential.app_only:
        lines.append(
            "App-only has no signed-in user, so /me is not valid. Set "
            "GRAPH_MCP_ACT_AS_USER to the UPN this server should act on."
        )
    return lines
