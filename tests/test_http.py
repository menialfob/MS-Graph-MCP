"""Tests for the remote-hosting entry point.

These cover the properties that only matter once the server is reachable over
the network and shared between users.
"""

from __future__ import annotations

import pytest

from graph_mcp.http import build_auth, build_graph_backend, build_transport_security


class TestTransportSecurity:
    def test_allowed_hosts_include_the_port(self):
        # Regression: the Host header carries the port ("127.0.0.1:8000"), so
        # an allowlist of bare hostnames matches nothing and every request is
        # rejected with 421 Misdirected Request.
        settings = build_transport_security([], [], "127.0.0.1", 8000)
        assert "127.0.0.1:8000" in settings.allowed_hosts
        assert "127.0.0.1" in settings.allowed_hosts

    def test_the_bind_host_is_allowed(self):
        settings = build_transport_security([], [], "graph-mcp.internal", 443)
        assert "graph-mcp.internal:443" in settings.allowed_hosts

    def test_explicit_hosts_replace_the_defaults(self):
        settings = build_transport_security(
            ["graph-mcp.example.com"], ["https://client.example.com"], "0.0.0.0", 8000
        )
        assert settings.allowed_hosts == ["graph-mcp.example.com"]
        assert settings.allowed_origins == ["https://client.example.com"]

    def test_origins_cover_both_schemes_by_default(self):
        settings = build_transport_security([], [], "localhost", 8000)
        assert "http://localhost:8000" in settings.allowed_origins
        assert "https://localhost:8000" in settings.allowed_origins


class TestAuthConfiguration:
    def test_no_resource_url_means_unauthenticated_local_mode(self):
        settings, verifier = build_auth(None, None, [])
        assert settings is None and verifier is None

    def test_enabling_auth_without_a_verifier_refuses_to_start(self, monkeypatch):
        # Accepting unverified bearer tokens would let any caller act as any
        # user, so this must fail loudly rather than fall back to a permissive
        # default.
        monkeypatch.delenv("GRAPH_MCP_TOKEN_VERIFIER", raising=False)
        with pytest.raises(SystemExit) as exc:
            build_auth("https://graph-mcp.example.com", None, [])
        assert "token verifier" in str(exc.value)

    def test_token_audience_is_validated(self, monkeypatch):
        monkeypatch.setenv("GRAPH_MCP_TOKEN_VERIFIER", "tests.test_http:_AcceptAll")
        settings, verifier = build_auth("https://graph-mcp.example.com", None, ["Mail.Read"])
        # Without audience validation a token minted for another API could be
        # replayed against this one.
        assert settings.validate_token_resource is True
        assert str(settings.resource_server_url).rstrip("/") == "https://graph-mcp.example.com"
        assert verifier is not None

    def test_malformed_verifier_spec_is_rejected(self, monkeypatch):
        monkeypatch.setenv("GRAPH_MCP_TOKEN_VERIFIER", "not_a_valid_spec")
        with pytest.raises(SystemExit) as exc:
            build_auth("https://graph-mcp.example.com", None, [])
        assert "module:Class" in str(exc.value)


class _AcceptAll:
    """Stand-in verifier for configuration tests only. Never use this."""

    async def verify_token(self, token: str):  # pragma: no cover - not called
        return None


class TestGraphBackend:
    """Which tenant the server talks to, and the combinations it refuses."""

    @pytest.fixture(autouse=True)
    def clean_env(self, monkeypatch, tmp_path):
        for name in (
            "AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET",
            "GRAPH_MCP_AZURE_FLOW", "GRAPH_MCP_ACT_AS_USER",
            "GRAPH_MCP_ALLOW_REMOTE_BIND", "AZURE_AUTHORITY_HOST",
        ):
            monkeypatch.delenv(name, raising=False)
        # Never touch the developer's real token cache from a test run.
        monkeypatch.setenv("GRAPH_MCP_TOKEN_CACHE", str(tmp_path / "cache.json"))

    def azure_env(self, monkeypatch, **extra):
        monkeypatch.setenv("AZURE_TENANT_ID", "tenant-1")
        monkeypatch.setenv("AZURE_CLIENT_ID", "client-1")
        monkeypatch.setenv("AZURE_CLIENT_SECRET", "secret-1")
        for key, value in extra.items():
            monkeypatch.setenv(key, value)

    def test_no_credentials_means_the_fixture_tenant(self):
        credential, factory = build_graph_backend(
            "auto", authenticated=False, host="127.0.0.1"
        )
        assert credential is None and factory is None

    def test_credentials_are_picked_up_automatically(self, monkeypatch):
        self.azure_env(monkeypatch)
        credential, factory = build_graph_backend(
            "auto", authenticated=False, host="127.0.0.1"
        )
        # Delegated by default: an app registration's scoped permissions only
        # ever reach Graph through a signed-in user.
        assert credential.flow == "device_code"
        assert not credential.app_only
        assert factory is not None

    def test_fixtures_can_be_forced_over_real_credentials(self, monkeypatch):
        self.azure_env(monkeypatch)
        assert build_graph_backend("fixture", authenticated=False, host="127.0.0.1") == (
            None, None
        )

    def test_asking_for_azure_without_credentials_is_an_error(self):
        with pytest.raises(SystemExit) as exc:
            build_graph_backend("azure", authenticated=False, host="127.0.0.1")
        assert "AZURE_TENANT_ID" in str(exc.value)

    def test_partial_credentials_do_not_fall_back_to_fixtures(self, monkeypatch):
        monkeypatch.setenv("AZURE_TENANT_ID", "tenant-1")
        with pytest.raises(SystemExit) as exc:
            build_graph_backend("auto", authenticated=False, host="127.0.0.1")
        assert "AZURE_CLIENT_ID" in str(exc.value)

    def test_credentials_and_resource_server_mode_are_refused(self, monkeypatch):
        # The server holds one identity. Authenticating each caller and then
        # acting as that one identity would hand every user the operator's
        # access to the tenant.
        self.azure_env(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            build_graph_backend("auto", authenticated=True, host="0.0.0.0")
        assert "--resource-url" in str(exc.value)

    def test_credentials_are_not_exposed_on_a_public_interface(self, monkeypatch):
        self.azure_env(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            build_graph_backend("auto", authenticated=False, host="0.0.0.0")
        assert "127.0.0.1" in str(exc.value)

    def test_a_public_bind_can_be_opted_into(self, monkeypatch):
        # Something in front may be doing the authentication.
        self.azure_env(monkeypatch, GRAPH_MCP_ALLOW_REMOTE_BIND="1")
        credential, _ = build_graph_backend(
            "auto", authenticated=False, host="0.0.0.0"
        )
        assert credential is not None

    def test_app_only_is_available_but_never_the_default(self, monkeypatch):
        self.azure_env(monkeypatch, GRAPH_MCP_AZURE_FLOW="client_credentials")
        credential, _ = build_graph_backend(
            "auto", authenticated=False, host="127.0.0.1"
        )
        assert credential.app_only

    def test_an_unknown_flow_is_reported_not_raised_raw(self, monkeypatch):
        self.azure_env(monkeypatch, GRAPH_MCP_AZURE_FLOW="magic")
        with pytest.raises(SystemExit) as exc:
            build_graph_backend("auto", authenticated=False, host="127.0.0.1")
        assert "device_code" in str(exc.value)
