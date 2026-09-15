"""Tests for the remote-hosting entry point.

These cover the properties that only matter once the server is reachable over
the network and shared between users.
"""

from __future__ import annotations

import pytest

from graph_mcp.http import build_auth, build_transport_security


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
