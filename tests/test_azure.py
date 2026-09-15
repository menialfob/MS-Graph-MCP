"""Tests for the Entra credential and the single-operator wiring.

No network and no tenant: the token endpoint is a stub, so the flows, the
caching, the refresh and -- most importantly -- the failure messages are all
exercised. The failure messages matter because every one of them stands in for
a person staring at an AADSTS code that does not say what to change.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from graph_mcp.azure import (
    AzureApp,
    AzureAuthError,
    EntraCredential,
    TokenCache,
    build_transport_factory,
    decode_claims,
    preflight,
)
from graph_mcp.caller import LOCAL_CALLER, Caller
from graph_mcp.graph.request import GraphRequest, GraphResponse
from graph_mcp.graph.transport import HttpGraphTransport

APP = AzureApp("tenant-1", "client-1", "secret-1")


def jwt(**claims) -> str:
    """A token shaped like Entra's, without a signature that means anything."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


DELEGATED = jwt(
    oid="user-oid", upn="alice@contoso.com", name="Alice",
    scp="Mail.Read User.Read",
)
APP_ONLY = jwt(oid="sp-oid", app_displayname="Graph MCP", roles=["Mail.Read.All"])


class StubEndpoint:
    """Stands in for the token and device-code endpoints."""

    def __init__(self, *responses: dict):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, data: dict) -> dict:
        self.calls.append(dict(data))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def credential(flow="device_code", *, tmp_path=None, responses=(), **kwargs):
    cache = TokenCache(tmp_path / "cache.json" if tmp_path else None)
    cred = EntraCredential(
        APP, flow=flow, cache=cache, announce=lambda _: None,
        open_browser=False, **kwargs,
    )
    if responses:
        stub = StubEndpoint(*responses)
        cred._post_token = lambda data: _async(stub(data))  # type: ignore[assignment]
        cred.stub = stub  # type: ignore[attr-defined]
    return cred


async def _async(value):
    return value


# --------------------------------------------------------------------------


class TestAzureApp:
    def test_unset_environment_means_no_app(self):
        assert AzureApp.from_env({}) is None

    def test_partial_configuration_is_refused(self):
        # Someone who set two of three meant to reach a real tenant; answering
        # from the fixture instead would look like it worked.
        with pytest.raises(AzureAuthError) as exc:
            AzureApp.from_env({"AZURE_TENANT_ID": "t", "AZURE_CLIENT_SECRET": "s"})
        assert "AZURE_CLIENT_ID" in str(exc.value)

    def test_a_secret_is_not_required(self):
        # device_code needs no secret, so tenant + client alone is a valid setup.
        app = AzureApp.from_env({"AZURE_TENANT_ID": "t", "AZURE_CLIENT_ID": "c"})
        assert app is not None and app.client_secret is None

    def test_endpoints_are_tenant_scoped(self):
        app = AzureApp("tenant-1", "client-1")
        assert app.token_endpoint.endswith("/tenant-1/oauth2/v2.0/token")
        assert app.device_code_endpoint.endswith("/tenant-1/oauth2/v2.0/devicecode")

    def test_a_sovereign_cloud_authority_is_honoured(self):
        app = AzureApp.from_env({
            "AZURE_TENANT_ID": "t", "AZURE_CLIENT_ID": "c",
            "AZURE_AUTHORITY_HOST": "https://login.microsoftonline.us/",
        })
        assert app.token_endpoint.startswith("https://login.microsoftonline.us/t/")


class TestFlowSelection:
    def test_unknown_flow_is_rejected(self):
        with pytest.raises(AzureAuthError) as exc:
            EntraCredential(APP, flow="implicit")
        assert "device_code" in str(exc.value)

    def test_flows_needing_a_secret_say_so(self):
        with pytest.raises(AzureAuthError) as exc:
            EntraCredential(AzureApp("t", "c"), flow="client_credentials")
        assert "AZURE_CLIENT_SECRET" in str(exc.value)

    def test_device_code_does_not_need_a_secret(self):
        assert EntraCredential(AzureApp("t", "c"), flow="device_code").flow == "device_code"


class TestClientCredentials:
    @pytest.mark.anyio
    async def test_acquires_an_app_only_token(self, tmp_path):
        cred = credential(
            "client_credentials", tmp_path=tmp_path,
            responses=({"access_token": APP_ONLY, "expires_in": 3600},),
        )
        assert await cred.token() == APP_ONLY
        assert cred.app_only
        # An app-only token carries `roles`, never `scp`.
        assert cred.granted_scopes == ("Mail.Read.All",)
        assert cred.stub.calls[0]["grant_type"] == "client_credentials"

    @pytest.mark.anyio
    async def test_a_token_with_no_roles_is_the_delegated_mistake(self, tmp_path):
        # The failure this whole module exists to explain: a registration whose
        # permissions are delegated mints a client-credentials token that
        # carries nothing, and Graph then 403s every single call.
        cred = credential(
            "client_credentials", tmp_path=tmp_path,
            responses=({"access_token": jwt(oid="sp"), "expires_in": 3600},),
        )
        with pytest.raises(AzureAuthError) as exc:
            await cred.token()
        assert "delegated" in str(exc.value)
        assert "device_code" in str(exc.value)

    @pytest.mark.anyio
    async def test_no_refresh_token_is_cached(self, tmp_path):
        cred = credential(
            "client_credentials", tmp_path=tmp_path,
            responses=({"access_token": APP_ONLY, "expires_in": 3600},),
        )
        await cred.token()
        # Client credentials can always mint another; caching one would be a
        # long-lived tenant-wide credential on disk for no benefit.
        assert cred.cache.read(cred.cache_key) == {}


class TestTokenLifetime:
    @pytest.mark.anyio
    async def test_a_valid_token_is_reused(self, tmp_path):
        cred = credential(
            "client_credentials", tmp_path=tmp_path,
            responses=({"access_token": APP_ONLY, "expires_in": 3600},),
        )
        await cred.token()
        await cred.token()
        assert len(cred.stub.calls) == 1

    @pytest.mark.anyio
    async def test_a_token_near_expiry_is_replaced(self, tmp_path):
        cred = credential(
            "client_credentials", tmp_path=tmp_path,
            responses=({"access_token": APP_ONLY, "expires_in": 3600},),
        )
        await cred.token()
        # Inside the refresh margin: still technically valid, but a long call
        # started now could outlive it.
        cred._token.expires_at = time.time() + 60
        await cred.token()
        assert len(cred.stub.calls) == 2

    @pytest.mark.anyio
    async def test_concurrent_callers_acquire_once(self, tmp_path):
        import anyio

        cred = credential(
            "client_credentials", tmp_path=tmp_path,
            responses=({"access_token": APP_ONLY, "expires_in": 3600},),
        )
        async with anyio.create_task_group() as tg:
            for _ in range(5):
                tg.start_soon(cred.token)
        assert len(cred.stub.calls) == 1


class TestRefreshAndCache:
    def test_a_delegated_sign_in_caches_its_refresh_token(self, tmp_path):
        cred = credential(tmp_path=tmp_path)
        cred._store({
            "access_token": DELEGATED, "expires_in": 3600,
            "refresh_token": "rt-1", "scope": "Mail.Read User.Read",
        })
        cached = cred.cache.read(cred.cache_key)
        assert cached["refresh_token"] == "rt-1"
        assert cached["account"]["user_principal_name"] == "alice@contoso.com"

    @pytest.mark.anyio
    async def test_a_cached_refresh_token_avoids_the_prompt(self, tmp_path):
        cache = TokenCache(tmp_path / "cache.json")
        first = credential(tmp_path=tmp_path)
        first.cache = cache
        first._store({"access_token": DELEGATED, "expires_in": 3600, "refresh_token": "rt-1"})

        # A fresh process, same cache file: it must not start a device-code flow.
        second = credential(
            tmp_path=tmp_path,
            responses=({"access_token": DELEGATED, "expires_in": 3600,
                        "refresh_token": "rt-2", "scope": "Mail.Read"},),
        )
        second._device_code = _never  # type: ignore[assignment]
        assert await second.token() == DELEGATED
        assert second.stub.calls[0]["grant_type"] == "refresh_token"
        assert second.stub.calls[0]["refresh_token"] == "rt-1"
        # Entra rotates refresh tokens; keeping the spent one would sign the
        # operator in again on the next restart.
        assert cache.read(second.cache_key)["refresh_token"] == "rt-2"

    @pytest.mark.anyio
    async def test_a_dead_refresh_token_falls_back_to_signing_in(self, tmp_path):
        cache = TokenCache(tmp_path / "cache.json")
        cache.write("tenant-1:client-1:device_code", {"refresh_token": "revoked"})

        cred = credential(
            tmp_path=tmp_path,
            responses=(
                {"error": "invalid_grant", "error_description": "AADSTS50173: expired"},
                {"access_token": DELEGATED, "expires_in": 3600, "scope": "Mail.Read"},
            ),
        )
        cred.cache = cache
        signed_in = []

        async def fake_device_code():
            signed_in.append(True)
            cred._store({"access_token": DELEGATED, "expires_in": 3600})

        cred._device_code = fake_device_code  # type: ignore[assignment]
        await cred.token()
        assert signed_in == [True]
        # The dead token is gone, not retried forever.
        assert cache.read(cred.cache_key) == {}

    def test_the_cache_can_be_turned_off(self):
        cache = TokenCache.from_env({"GRAPH_MCP_TOKEN_CACHE": "none"})
        assert cache.path is None
        cache.write("k", {"refresh_token": "rt"})
        assert cache.read("k") == {}

    def test_the_cache_file_is_not_world_readable(self, tmp_path):
        import stat

        path = tmp_path / "cache.json"
        TokenCache(path).write("k", {"refresh_token": "rt"})
        # It holds a bearer credential for the user who signed in.
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_a_corrupt_cache_is_ignored_not_fatal(self, tmp_path):
        path = tmp_path / "cache.json"
        path.write_text("{not json", encoding="utf-8")
        assert TokenCache(path).read("k") == {}

    def test_flows_do_not_share_a_cache_entry(self, tmp_path):
        a = credential("device_code", tmp_path=tmp_path)
        b = credential("auth_code", tmp_path=tmp_path)
        assert a.cache_key != b.cache_key


class TestDeviceCode:
    @pytest.mark.anyio
    async def test_polls_until_the_sign_in_completes(self, tmp_path, monkeypatch):
        import graph_mcp.azure as azure

        announced: list[str] = []
        cred = credential(tmp_path=tmp_path, responses=(
            {"error": "authorization_pending"},
            {"access_token": DELEGATED, "expires_in": 3600,
             "refresh_token": "rt", "scope": "Mail.Read User.Read"},
        ))
        cred.announce = announced.append
        monkeypatch.setattr(
            azure.EntraCredential, "_start_device_code",
            lambda self: _async({
                "device_code": "dc", "user_code": "ABCD-EFGH", "interval": 0,
                "expires_in": 900,
                "message": "Go to https://microsoft.com/devicelogin and enter ABCD-EFGH",
            }),
        )
        assert await cred.token() == DELEGATED
        assert any("ABCD-EFGH" in line for line in announced)
        assert cred.granted_scopes == ("Mail.Read", "User.Read")

    @pytest.mark.anyio
    async def test_slow_down_backs_off_rather_than_failing(self, tmp_path, monkeypatch):
        import graph_mcp.azure as azure

        cred = credential(tmp_path=tmp_path, responses=(
            {"error": "slow_down"},
            {"access_token": DELEGATED, "expires_in": 3600},
        ))
        monkeypatch.setattr(
            azure.EntraCredential, "_start_device_code",
            lambda self: _async({"device_code": "dc", "interval": 0, "expires_in": 900}),
        )
        monkeypatch.setattr(azure, "SLOW_DOWN_INCREMENT", 0.0)
        assert await cred.token() == DELEGATED

    @pytest.mark.anyio
    async def test_a_declined_sign_in_stops(self, tmp_path, monkeypatch):
        import graph_mcp.azure as azure

        cred = credential(tmp_path=tmp_path,
                          responses=({"error": "authorization_declined"},))
        monkeypatch.setattr(
            azure.EntraCredential, "_start_device_code",
            lambda self: _async({"device_code": "dc", "interval": 0, "expires_in": 900}),
        )
        with pytest.raises(AzureAuthError) as exc:
            await cred.token()
        assert "declined" in str(exc.value)


class TestErrorMessages:
    @pytest.mark.anyio
    async def test_public_client_flows_disabled_names_the_toggle(self, tmp_path):
        # Entra's own text here is "The request body must contain the following
        # parameter: 'client_assertion' or 'client_secret'", which sends people
        # to add a secret they already have. The fix is a different setting.
        cred = credential(tmp_path=tmp_path, responses=({
            "error": "invalid_client",
            "error_description": "AADSTS7000218: The request body must contain "
                                 "the following parameter: 'client_secret'.",
        },))
        with pytest.raises(AzureAuthError) as exc:
            await cred._refresh("rt")
        assert "Allow public client flows" in str(exc.value)
        assert "auth_code" in str(exc.value)

    @pytest.mark.anyio
    async def test_a_bad_secret_says_value_not_id(self, tmp_path):
        cred = credential("client_credentials", tmp_path=tmp_path, responses=({
            "error": "invalid_client",
            "error_description": "AADSTS7000215: Invalid client secret provided.",
        },))
        with pytest.raises(AzureAuthError) as exc:
            await cred.token()
        assert "secret ID" in str(exc.value)

    @pytest.mark.anyio
    async def test_an_unknown_aadsts_code_still_reports_the_first_line(self, tmp_path):
        cred = credential("client_credentials", tmp_path=tmp_path, responses=({
            "error": "invalid_request",
            "error_description": "AADSTS99999: Something new.\nTrace ID: abc\n",
        },))
        with pytest.raises(AzureAuthError) as exc:
            await cred.token()
        assert "Something new." in str(exc.value)
        # The trace id and timestamp block is noise for the person reading it.
        assert "Trace ID" not in str(exc.value)

    @pytest.mark.anyio
    async def test_a_response_with_no_token_is_an_error(self, tmp_path):
        cred = credential("client_credentials", tmp_path=tmp_path, responses=({},))
        with pytest.raises(AzureAuthError) as exc:
            await cred.token()
        assert "no access token" in str(exc.value)


class TestClaims:
    def test_scopes_come_from_the_response_when_present(self):
        cred = credential("device_code")
        cred._store({"access_token": DELEGATED, "expires_in": 3600,
                     "scope": "Files.Read"})
        # What Entra granted, which is not always what was asked for.
        assert cred.granted_scopes == ("Files.Read",)

    def test_scopes_fall_back_to_the_scp_claim(self):
        cred = credential("device_code")
        cred._store({"access_token": DELEGATED, "expires_in": 3600})
        assert cred.granted_scopes == ("Mail.Read", "User.Read")

    def test_the_delegated_account_is_the_signed_in_user(self):
        cred = credential("device_code")
        cred._store({"access_token": DELEGATED, "expires_in": 3600})
        assert cred.account()["user_principal_name"] == "alice@contoso.com"
        assert cred.account()["display_name"] == "Alice"

    def test_the_app_only_account_has_no_user(self):
        cred = credential("client_credentials")
        cred._store({"access_token": APP_ONLY, "expires_in": 3600})
        assert cred.account()["user_principal_name"] == ""
        assert cred.account()["display_name"] == "Graph MCP"

    def test_an_unparseable_token_degrades_rather_than_raises(self):
        # Claims are read for the banner, never for a decision, so a token that
        # does not parse must not take the server down.
        assert decode_claims("not-a-jwt") == {}
        assert decode_claims("") == {}


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


class FakeHttp:
    """Captures what HttpGraphTransport puts on the wire."""

    def __init__(self, status=200, body=None):
        self.status, self.body = status, body or {}
        self.requests: list[GraphRequest] = []

    async def send(self, request: GraphRequest) -> GraphResponse:
        self.requests.append(request)
        return GraphResponse(self.status, self.body)


class TestTransportFactory:
    def test_it_refuses_a_caller_it_cannot_act_as(self):
        # Defence in depth behind the entry point's check: one credential is one
        # identity, so serving an authenticated user with it would hand them the
        # operator's access to the whole tenant.
        cred = credential("device_code")
        factory = build_transport_factory(cred)
        factory(LOCAL_CALLER)  # the operator: fine
        with pytest.raises(RuntimeError) as exc:
            factory(Caller(subject="some-other-oid", scopes=("Mail.Read",)))
        assert "one Azure credential" in str(exc.value)

    @pytest.mark.anyio
    async def test_the_transport_reports_the_tokens_scopes(self):
        cred = credential("device_code")
        cred._store({"access_token": DELEGATED, "expires_in": 3600})
        transport = build_transport_factory(cred)(LOCAL_CALLER)
        transport.send = FakeHttp(body={
            "id": "user-oid", "displayName": "Alice",
            "userPrincipalName": "alice@contoso.com",
        }).send
        identity = await transport.identity()
        # Drives caller_has_scope in search and the missing-scope guidance on
        # a 403; without it both read "unknown".
        assert identity["scopes"] == ["Mail.Read", "User.Read"]
        assert identity["user_principal_name"] == "alice@contoso.com"


class TestAppOnlyPaths:
    def test_me_is_rewritten_to_the_named_user(self):
        transport = HttpGraphTransport(
            lambda: _async("token"), app_only=True, act_as_user="alice@contoso.com",
        )
        request = GraphRequest("GET", "/me/messages")
        transport._resolve_user(request)
        # In place, because the result's `request` field is rendered from this
        # object and is the record of whose mailbox was actually read.
        assert request.path == "/users/alice@contoso.com/messages"
        # The bare singleton too, not only its children.
        assert transport._resolve_user(GraphRequest("GET", "/me")).path == (
            "/users/alice@contoso.com"
        )

    def test_other_paths_are_untouched(self):
        transport = HttpGraphTransport(
            lambda: _async("t"), app_only=True, act_as_user="alice@contoso.com",
        )
        # /members must not become /usersalice.../mbers.
        for path in ("/users/bob/messages", "/groups", "/me2/thing"):
            assert transport._resolve_user(GraphRequest("GET", path)).path == path

    @pytest.mark.anyio
    async def test_me_without_a_named_user_explains_itself(self):
        transport = HttpGraphTransport(lambda: _async("t"), app_only=True)
        response = await transport.send(GraphRequest("GET", "/me/messages"))
        # Graph's own 400 does not mention that the token is app-only, so a
        # model retries the identical request.
        assert response.status == 400
        assert "GRAPH_MCP_ACT_AS_USER" in response.body["error"]["message"]

    @pytest.mark.anyio
    async def test_app_only_identity_needs_no_round_trip(self):
        cred = credential("client_credentials")
        cred._store({"access_token": APP_ONLY, "expires_in": 3600})
        transport = build_transport_factory(cred)(LOCAL_CALLER)
        http = FakeHttp()
        transport.send = http.send
        identity = await transport.identity()
        assert identity["display_name"] == "Graph MCP"
        assert http.requests == []


class TestPreflight:
    @pytest.mark.anyio
    async def test_a_working_delegated_credential_reports_the_user(self):
        cred = credential("device_code")
        cred._store({"access_token": DELEGATED, "expires_in": 3600})
        lines = await preflight(cred, FakeHttp(body={"id": "x"}))
        assert "alice@contoso.com" in lines[0]

    @pytest.mark.anyio
    async def test_a_403_names_what_the_token_actually_grants(self):
        cred = credential("device_code")
        cred._store({"access_token": DELEGATED, "expires_in": 3600})
        http = FakeHttp(403, {"error": {"code": "accessDenied", "message": "Denied."}})
        lines = await preflight(cred, http)
        assert "accessDenied" in lines[0]
        assert "Mail.Read" in lines[1]

    @pytest.mark.anyio
    async def test_app_only_probes_something_it_can_actually_read(self):
        cred = credential("client_credentials")
        cred._store({"access_token": APP_ONLY, "expires_in": 3600})
        http = FakeHttp(body={"value": []})
        await preflight(cred, http)
        # /me is not valid without a signed-in user, so probing it would fail
        # for a reason that has nothing to do with the credential working.
        assert http.requests[0].path == "/organization"

    @pytest.mark.anyio
    async def test_an_app_only_403_points_at_application_permissions(self):
        cred = credential("client_credentials")
        cred._store({"access_token": APP_ONLY, "expires_in": 3600})
        lines = await preflight(
            cred, FakeHttp(403, {"error": {"code": "accessDenied", "message": "no"}})
        )
        assert "application" in " ".join(lines).lower()


async def _never(*args, **kwargs):  # pragma: no cover - asserts it is not called
    raise AssertionError("an interactive sign-in was started when one was cached")
