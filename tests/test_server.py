"""Server behaviour, exercised through a real MCP client session.

These go over the wire protocol rather than calling tool functions directly, so
they cover the parts of the specification that matter to a client: initialize,
tool listing with annotations and schemas, and failures reported as
`isError` results rather than transport errors.
"""

from __future__ import annotations

import pytest

from tests.mcp_client import client_for, failure, structured

pytestmark = pytest.mark.anyio


class TestProtocol:
    async def test_initializes_and_reports_instructions(self, server):
        async with client_for(server) as session:
            assert session.protocol_version
            assert "graph_search_operations" in (session.instructions or "")

    async def test_exposes_the_seven_tools(self, server):
        async with client_for(server) as session:
            names = {t.name for t in (await session.list_tools()).tools}
        assert names == {
            "graph_search_operations", "graph_describe_operation",
            "graph_describe_type", "graph_get", "graph_next_page",
            "graph_write", "graph_whoami",
        }

    async def test_every_tool_has_a_title_description_and_schemas(self, server):
        async with client_for(server) as session:
            tools = (await session.list_tools()).tools
        for tool in tools:
            assert tool.title, f"{tool.name} has no title"
            assert tool.description, f"{tool.name} has no description"
            assert tool.input_schema.get("type") == "object"
            assert tool.output_schema, f"{tool.name} has no output schema"

    async def test_read_tools_are_annotated_read_only(self, server):
        async with client_for(server) as session:
            by_name = {t.name: t for t in (await session.list_tools()).tools}
        for name in ("graph_get", "graph_next_page", "graph_search_operations",
                     "graph_describe_type", "graph_whoami"):
            assert by_name[name].annotations.read_only_hint is True

    async def test_write_tool_is_annotated_destructive(self, server):
        async with client_for(server) as session:
            by_name = {t.name: t for t in (await session.list_tools()).tools}
        annotations = by_name["graph_write"].annotations
        assert annotations.read_only_hint is False
        assert annotations.destructive_hint is True
        assert annotations.idempotent_hint is False

    async def test_open_world_marks_only_tools_that_reach_graph(self, server):
        async with client_for(server) as session:
            by_name = {t.name: t for t in (await session.list_tools()).tools}
        assert by_name["graph_get"].annotations.open_world_hint is True
        # Retrieval and schema lookups are served from local artifacts.
        assert by_name["graph_search_operations"].annotations.open_world_hint is False
        assert by_name["graph_describe_type"].annotations.open_world_hint is False

    async def test_tool_failures_are_error_results_not_transport_errors(self, server):
        # The specification requires tool execution failures to come back as a
        # CallToolResult with isError set, so the model can see and correct them.
        async with client_for(server) as session:
            result = await session.call_tool("graph_get", {"path": "/nope/nothing"})
        assert result.is_error is True
        assert result.content, "an error result still needs readable content"

    async def test_invalid_arguments_are_rejected(self, server):
        async with client_for(server) as session:
            result = await session.call_tool("graph_get", {})
        assert result.is_error is True


class TestWhoAmI:
    async def test_reports_identity_and_capabilities(self, server):
        async with client_for(server) as session:
            data = structured(await session.call_tool("graph_whoami", {}))
        assert data["user_principal_name"] == "you@contoso.com"
        assert "Mail.Read" in data["granted_scopes"]
        assert data["writes_enabled"] is False
        assert data["transport"] == "FakeGraphTransport"


class TestSearch:
    async def test_finds_operations_by_intent(self, server):
        async with client_for(server) as session:
            data = structured(await session.call_tool(
                "graph_search_operations", {"intent": "list my messages"}))
        assert data["candidates"][0]["operation_id"] == "GET /me/messages"

    async def test_annotates_whether_the_caller_holds_the_scope(self, server):
        async with client_for(server) as session:
            data = structured(await session.call_tool(
                "graph_search_operations", {"intent": "sign in logs audit"}))
        audit = [c for c in data["candidates"] if c["operation_id"] == "GET /auditLogs/signIns"]
        assert audit, "expected the audit log operation among candidates"
        assert audit[0]["caller_has_scope"] is False

    async def test_only_callable_filters_ungranted_operations(self, server):
        async with client_for(server) as session:
            data = structured(await session.call_tool(
                "graph_search_operations",
                {"intent": "sign in logs audit", "only_callable": True}))
        assert all(c["operation_id"] != "GET /auditLogs/signIns" for c in data["candidates"])

    async def test_method_filter(self, server):
        async with client_for(server) as session:
            data = structured(await session.call_tool(
                "graph_search_operations", {"intent": "mail", "method": "POST"}))
        assert all(c["method"] == "POST" for c in data["candidates"])

    async def test_empty_intent_is_rejected(self, server):
        async with client_for(server) as session:
            message = failure(await session.call_tool(
                "graph_search_operations", {"intent": "   "}))
        assert "empty" in message.lower()


class TestDescribe:
    async def test_describes_an_operation(self, server):
        async with client_for(server) as session:
            data = structured(await session.call_tool(
                "graph_describe_operation", {"operation_id": "GET /me/messages"}))
        assert data["response_type"] == "microsoft.graph.message"
        assert data["returns_collection"] is True
        assert data["permissions"]["least"]["delegated_work"] == ["Mail.ReadBasic"]

    async def test_unknown_operation_id_is_an_error(self, server):
        async with client_for(server) as session:
            message = failure(await session.call_tool(
                "graph_describe_operation", {"operation_id": "GET /nope"}))
        assert "Unknown operation_id" in message

    async def test_flags_writes_blocked_by_policy(self, server):
        async with client_for(server) as session:
            data = structured(await session.call_tool(
                "graph_describe_operation", {"operation_id": "POST /me/sendMail"}))
        assert data["writable"] is False
        assert any("disabled" in note for note in data["notes"])


class TestDescribeType:
    async def test_resolves_inherited_properties(self, server):
        async with client_for(server) as session:
            data = structured(await session.call_tool(
                "graph_describe_type", {"type_name": "user"}))
        names = {p["name"] for p in data["properties"]}
        assert "displayName" in names
        # 'id' is declared on directoryObject; without inheritance it vanishes.
        assert "id" in names
        assert data["base_type"] == "microsoft.graph.directoryObject"

    async def test_accepts_a_fully_qualified_name(self, server):
        async with client_for(server) as session:
            data = structured(await session.call_tool(
                "graph_describe_type", {"type_name": "microsoft.graph.message"}))
        assert data["name"] == "microsoft.graph.message"

    async def test_reports_enum_members(self, server):
        async with client_for(server) as session:
            data = structured(await session.call_tool(
                "graph_describe_type", {"type_name": "importance"}))
        assert data["kind"] == "enum"
        assert data["members"] == ["low", "normal", "high"]

    async def test_unknown_type_suggests_alternatives(self, server):
        async with client_for(server) as session:
            message = failure(await session.call_tool(
                "graph_describe_type", {"type_name": "mesage"}))
        assert "Unknown type" in message and "Did you mean" in message


class TestGraphGet:
    async def test_reads_a_collection(self, server):
        async with client_for(server) as session:
            data = structured(await session.call_tool("graph_get", {"path": "/me/messages"}))
        assert data["status"] == 200
        assert data["item_count"] == 5

    async def test_applies_a_default_select(self, server):
        async with client_for(server) as session:
            data = structured(await session.call_tool("graph_get", {"path": "/me/messages"}))
        assert "$select=" in data["request"]["url"]
        assert any("default $select" in note for note in data["notes"])

    async def test_default_select_applies_to_camel_case_paths(self, server, transport):
        # Regression: route templates are lowercased ("/me/mailfolders") while
        # catalog keys keep the spec's casing ("/me/mailFolders"). Looking one
        # up with the other found nothing, silently disabling default $select
        # and required-scope reporting for every camelCase path.
        async with client_for(server) as session:
            data = structured(await session.call_tool(
                "graph_get", {"path": "/me/mailFolders"}))
        assert "$select=" in data["request"]["url"]
        assert "displayName" in transport.sent[-1].query["$select"]

    async def test_explicit_select_is_not_overridden(self, server, transport):
        async with client_for(server) as session:
            await session.call_tool("graph_get", {"path": "/me/messages", "select": ["subject"]})
        assert transport.sent[-1].query["$select"] == "subject"

    async def test_omits_heavy_fields_that_were_not_selected(self, server):
        async with client_for(server) as session:
            data = structured(await session.call_tool("graph_get", {"path": "/me/messages"}))
        assert all("body" not in item for item in data["data"]["value"])

    async def test_leading_slash_is_optional(self, server):
        async with client_for(server) as session:
            data = structured(await session.call_tool("graph_get", {"path": "me/messages"}))
        assert data["status"] == 200

    async def test_rejects_paths_outside_the_catalog(self, server):
        async with client_for(server) as session:
            message = failure(await session.call_tool(
                "graph_get", {"path": "/deviceManagement/managedDevices"}))
        assert "does not match any known Graph operation" in message

    async def test_suggests_near_misses(self, server):
        async with client_for(server) as session:
            message = failure(await session.call_tool("graph_get", {"path": "/me/mesages"}))
        assert "/me/messages" in message

    async def test_rejects_a_method_the_route_does_not_offer(self, server):
        async with client_for(server) as session:
            message = failure(await session.call_tool("graph_get", {"path": "/me/sendMail"}))
        assert "GET is not available" in message

    async def test_adds_consistency_level_for_directory_queries(self, server, transport):
        async with client_for(server) as session:
            await session.call_tool("graph_get", {"path": "/users", "search": "anna"})
        sent = transport.sent[-1]
        assert sent.headers["ConsistencyLevel"] == "eventual"
        assert sent.query["$count"] == "true"

    async def test_no_consistency_level_for_workload_queries(self, server, transport):
        async with client_for(server) as session:
            await session.call_tool("graph_get", {"path": "/me/messages", "top": 3})
        assert "ConsistencyLevel" not in transport.sent[-1].headers

    async def test_translates_a_403_into_actionable_guidance(self, server):
        async with client_for(server) as session:
            message = failure(await session.call_tool(
                "graph_get", {"path": "/auditLogs/signIns"}))
        assert "403" in message
        assert "AuditLog.Read.All" in message

    async def test_reports_the_request_without_credentials(self, server):
        async with client_for(server) as session:
            data = structured(await session.call_tool("graph_get", {"path": "/me"}))
        assert data["request"]["url"].startswith("https://graph.microsoft.com/v1.0/me")
        assert "authorization" not in {k.lower() for k in data["request"]["headers"]}


class TestPaging:
    async def test_returns_a_cursor_when_more_results_exist(self, server):
        async with client_for(server) as session:
            data = structured(await session.call_tool("graph_get", {"path": "/me/messages"}))
        assert data["has_more"] is True
        assert data["cursor"].startswith("c_")

    async def test_cursor_fetches_the_following_page(self, server):
        async with client_for(server) as session:
            first = structured(await session.call_tool("graph_get", {"path": "/me/messages"}))
            second = structured(await session.call_tool(
                "graph_next_page", {"cursor": first["cursor"]}))
        first_ids = {m["id"] for m in first["data"]["value"]}
        second_ids = {m["id"] for m in second["data"]["value"]}
        assert first_ids and second_ids and not (first_ids & second_ids)

    async def test_next_link_never_reaches_the_caller(self, server):
        async with client_for(server) as session:
            data = structured(await session.call_tool("graph_get", {"path": "/me/messages"}))
        assert "@odata.nextLink" not in data["data"]

    async def test_unknown_cursor_is_an_error(self, server):
        async with client_for(server) as session:
            message = failure(await session.call_tool(
                "graph_next_page", {"cursor": "c_deadbeef"}))
        assert "unknown or expired" in message


class TestWrites:
    async def test_writes_are_refused_when_disabled(self, server, transport):
        async with client_for(server) as session:
            message = failure(await session.call_tool(
                "graph_write", {"method": "POST", "path": "/me/sendMail", "body": {}}))
        assert "disabled" in message
        assert transport.sent == []

    async def test_dry_run_is_the_default_and_sends_nothing(self, writable_server, transport):
        async with client_for(writable_server) as session:
            data = structured(await session.call_tool(
                "graph_write", {"method": "POST", "path": "/me/sendMail",
                                "body": {"message": {"subject": "hi"}}}))
        assert data["executed"] is False
        assert data["confirm_token"]
        assert transport.sent == []

    async def test_execution_requires_the_confirm_token(self, writable_server, transport):
        async with client_for(writable_server) as session:
            message = failure(await session.call_tool(
                "graph_write", {"method": "POST", "path": "/me/sendMail",
                                "body": {"message": {"subject": "hi"}}, "dry_run": False}))
        assert "confirm token" in message
        assert transport.sent == []

    async def test_executes_with_a_valid_token(self, writable_server, transport):
        body = {"message": {"subject": "hi"}}
        async with client_for(writable_server) as session:
            plan = structured(await session.call_tool(
                "graph_write", {"method": "POST", "path": "/me/sendMail", "body": body}))
            result = structured(await session.call_tool(
                "graph_write", {"method": "POST", "path": "/me/sendMail", "body": body,
                                "dry_run": False, "confirm": plan["confirm_token"]}))
        assert result["executed"] is True
        assert transport.sent[-1].method == "POST"

    async def test_a_token_does_not_authorise_a_different_request(self, writable_server, transport):
        # The whole point of binding the token to the request: a benign plan
        # must not be reusable to execute something else.
        async with client_for(writable_server) as session:
            plan = structured(await session.call_tool(
                "graph_write", {"method": "POST", "path": "/me/sendMail",
                                "body": {"message": {"subject": "benign"}}}))
            message = failure(await session.call_tool(
                "graph_write", {"method": "POST", "path": "/me/sendMail",
                                "body": {"message": {"subject": "TAMPERED"}},
                                "dry_run": False, "confirm": plan["confirm_token"]}))
        assert "confirm token" in message
        assert transport.sent == []

    async def test_operations_outside_the_allowlist_are_refused(self, writable_server):
        async with client_for(writable_server) as session:
            message = failure(await session.call_tool(
                "graph_write", {"method": "DELETE", "path": "/me/messages/msg-1"}))
        assert "not in the write allowlist" in message

    async def test_unknown_paths_are_refused(self, writable_server):
        async with client_for(writable_server) as session:
            message = failure(await session.call_tool(
                "graph_write", {"method": "POST", "path": "/deviceManagement/wipe"}))
        assert "does not match any known Graph operation" in message
