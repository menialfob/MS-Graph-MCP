"""Isolation between callers sharing one server process.

These are the tests that a remote, multi-tenant deployment lives or dies by.
A stdio server is one process per user, so none of this can go wrong there --
which is exactly why the bugs were present until the hosting model was made
explicit.
"""

from __future__ import annotations

import pytest

from graph_mcp.caller import Caller
from graph_mcp.graph.paging import CursorStore
from graph_mcp.graph.request import GraphRequest
from graph_mcp.policy.writes import confirm_token, verify
from tests.mcp_client import client_as, failure, structured

pytestmark = pytest.mark.anyio


class TestCursorOwnership:
    async def test_one_caller_cannot_resume_anothers_query(self, multiuser_runtime):
        """The bug that motivated all of this: Bob replayed Alice's cursor."""
        from graph_mcp.server import create_server

        server = create_server(multiuser_runtime)

        async with client_as(server, "alice", ("Mail.Read",)) as alice:
            first = structured(await alice.call_tool("graph_get", {"path": "/me/messages"}))
            cursor = first["cursor"]
            assert cursor

        async with client_as(server, "bob", ("Mail.Read",)) as bob:
            message = failure(await bob.call_tool("graph_next_page", {"cursor": cursor}))

        # Indistinguishable from a cursor that never existed: the error must
        # not confirm that someone else holds a valid one.
        assert "unknown or expired" in message

    async def test_the_owner_can_still_resume(self, multiuser_runtime):
        from graph_mcp.server import create_server

        server = create_server(multiuser_runtime)
        async with client_as(server, "alice", ("Mail.Read",)) as alice:
            first = structured(await alice.call_tool("graph_get", {"path": "/me/messages"}))
            second = structured(await alice.call_tool(
                "graph_next_page", {"cursor": first["cursor"]}))
        assert second["item_count"] > 0

    def test_store_rejects_a_foreign_owner(self):
        store = CursorStore()
        token = store.put("alice", "https://graph.microsoft.com/v1.0/me/messages", "GET", 1)
        assert store.get("alice", token) is not None
        assert store.get("bob", token) is None

    def test_cursor_ids_are_not_guessable(self):
        store = CursorStore()
        token = store.put("alice", "https://example.invalid", "GET", 1)
        # 16 random bytes, url-safe encoded: far beyond brute force in a
        # multi-user process where a guess reads someone else's data.
        assert len(token) >= 20


class TestIdentityIsolation:
    async def test_each_caller_sees_their_own_identity(self, multiuser_runtime):
        from graph_mcp.server import create_server

        server = create_server(multiuser_runtime)
        async with client_as(server, "alice", ("Mail.Read",)) as alice:
            a = structured(await alice.call_tool("graph_whoami", {}))
        async with client_as(server, "bob", ("User.Read",)) as bob:
            b = structured(await bob.call_tool("graph_whoami", {}))

        assert a["user_principal_name"] == "alice@contoso.com"
        assert b["user_principal_name"] == "bob@contoso.com"

    async def test_scopes_are_not_shared_between_callers(self, multiuser_runtime):
        """Caching identity process-wide leaked the first caller's scopes."""
        from graph_mcp.server import create_server

        server = create_server(multiuser_runtime)
        async with client_as(server, "alice", ("Mail.Read", "Mail.Send")) as alice:
            a = structured(await alice.call_tool("graph_whoami", {}))
        async with client_as(server, "bob", ("User.Read",)) as bob:
            b = structured(await bob.call_tool("graph_whoami", {}))

        assert set(a["granted_scopes"]) == {"Mail.Read", "Mail.Send"}
        assert set(b["granted_scopes"]) == {"User.Read"}

    async def test_scope_annotations_reflect_the_calling_user(self, multiuser_runtime):
        from graph_mcp.server import create_server

        server = create_server(multiuser_runtime)

        async with client_as(server, "alice", ("Mail.ReadBasic",)) as alice:
            a = structured(await alice.call_tool(
                "graph_search_operations", {"intent": "list my messages"}))
        async with client_as(server, "bob", ("User.Read",)) as bob:
            b = structured(await bob.call_tool(
                "graph_search_operations", {"intent": "list my messages"}))

        a_msgs = next(c for c in a["candidates"] if c["operation_id"] == "GET /me/messages")
        b_msgs = next(c for c in b["candidates"] if c["operation_id"] == "GET /me/messages")
        assert a_msgs["caller_has_scope"] is True
        assert b_msgs["caller_has_scope"] is False


class TestConfirmTokenBinding:
    def test_a_token_is_bound_to_its_caller(self):
        request = GraphRequest("POST", "/me/sendMail", body={"message": {"subject": "hi"}})
        alice_token = confirm_token(request, "alice")
        assert verify(request, alice_token, "alice")
        # Without subject binding, an identical request from another user
        # would be confirmed by a plan approved for Alice.
        assert not verify(request, alice_token, "bob")

    async def test_another_caller_cannot_use_a_plan_they_did_not_review(
        self, multiuser_runtime
    ):
        from graph_mcp.server import create_server

        multiuser_runtime.writes.enabled = True
        server = create_server(multiuser_runtime)
        body = {"message": {"subject": "hi"}}

        async with client_as(server, "alice", ("Mail.Send",)) as alice:
            plan = structured(await alice.call_tool(
                "graph_write", {"method": "POST", "path": "/me/sendMail", "body": body}))

        async with client_as(server, "bob", ("Mail.Send",)) as bob:
            message = failure(await bob.call_tool(
                "graph_write", {"method": "POST", "path": "/me/sendMail", "body": body,
                                "dry_run": False, "confirm": plan["confirm_token"]}))
        assert "confirm token" in message


class TestCallerResolution:
    def test_anonymous_caller_is_detectable(self):
        assert Caller(subject="").is_anonymous
        assert not Caller(subject="alice").is_anonymous

    def test_token_is_never_in_the_repr_or_redacted_view(self):
        caller = Caller(subject="alice", scopes=("Mail.Read",), token="secret-token")
        assert "secret-token" not in repr(caller)
        assert "token" not in caller.redacted()
