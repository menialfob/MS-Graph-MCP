"""The sensitivity-label gate, exercised with the feature switch ON.

tests/test_sensitivity_labels.py characterises the default (gate off) behaviour;
this file is about what changes when a deployment opts in. Everything runs over
the fixture tenant, including the label-reading POSTs, so the whole gate is
covered without a tenant or a Purview policy.
"""

from __future__ import annotations

import pytest

from graph_mcp.graph.labels import PS_PUBLIC_STRINGS
from graph_mcp.graph.transport import FakeGraphTransport
from graph_mcp.policy.labels import LabelGate, LabelPolicy, PurviewSettings
from graph_mcp.policy.routes import RouteTable
from graph_mcp.server import create_server
from tests.mcp_client import client_for, structured

pytestmark = pytest.mark.anyio

PERSONAL = "defa4170-0d19-0005-0007-bc88714345d2"
CONFIDENTIAL = "aaaa1111-0d19-0005-0007-bc88714345d2"


def _msip(label_id: str, enabled: str = "True") -> list[dict]:
    return [{
        "id": f"String {PS_PUBLIC_STRINGS} Name MSIP_Label_{label_id}_Enabled",
        "value": enabled,
    }]


def _message(ident: str, subject: str, label_id: str | None) -> dict:
    item = {"id": ident, "subject": subject, "isRead": False,
            "body": {"contentType": "text", "content": f"body of {ident}"}}
    if label_id is not None:
        item["singleValueExtendedProperties"] = _msip(label_id)
    else:
        item["singleValueExtendedProperties"] = []
    return item


LABEL_DEFINITIONS = {"value": [
    {"id": PERSONAL, "name": "Personal information", "hasProtection": False,
     "priority": 1},
    {"id": CONFIDENTIAL, "name": "Confidential", "hasProtection": True,
     "priority": 3},
]}

ROUTES = {
    "/me": ["GET"],
    "/me/messages": ["GET", "POST"],
    "/me/messages/{}": ["GET"],
    "/me/drive/root/children": ["GET"],
    "/me/chats/{}/messages": ["GET"],
}

ENTRY_MESSAGE = {
    "key": "GET /me/messages", "path": "/me/messages", "method": "GET",
    "operation_id": "me.ListMessages", "tags": [], "path_params": [],
    "query_params": ["$select", "$expand", "$top"],
    "response_type": "microsoft.graph.message", "request_type": "",
    "returns_collection": True, "summary": "", "spec_description": "",
    "title": "List messages", "description": "Messages in the mailbox.",
    "intro": "", "doc_url": "d", "permissions": {"least": {}, "higher": {}},
    "utterances": [], "overloads": 1, "indexed": True,
}
ENTRY_DRIVE = {
    **ENTRY_MESSAGE,
    "key": "GET /me/drive/root/children", "path": "/me/drive/root/children",
    "operation_id": "me.drive.children",
    "response_type": "microsoft.graph.driveItem",
    "title": "List children", "description": "Files in the root folder.",
}
ENTRY_USER = {
    **ENTRY_MESSAGE,
    "key": "GET /me", "path": "/me", "operation_id": "me.get",
    "response_type": "microsoft.graph.user", "returns_collection": False,
    "title": "Get me", "description": "The signed-in user.",
}
ENTRY_ONE_MESSAGE = {
    **ENTRY_MESSAGE,
    "key": "GET /me/messages/{}", "path": "/me/messages/{}",
    "operation_id": "me.getMessage", "path_params": ["message-id"],
    "returns_collection": False,
    "title": "Get message", "description": "One message.",
}
ENTRY_CHAT = {
    **ENTRY_MESSAGE,
    "key": "GET /me/chats/{}/messages", "path": "/me/chats/{}/messages",
    "operation_id": "me.chats.messages", "path_params": ["chat-id"],
    "response_type": "microsoft.graph.chatMessage",
    "title": "List chat messages", "description": "Messages in a chat.",
}


@pytest.fixture
def gated(runtime, fixture_data):
    """Build a server whose gate is configured per-test."""
    from graph_mcp.retrieval.index import IndexMeta, RetrievalIndex

    entries = [ENTRY_MESSAGE, ENTRY_DRIVE, ENTRY_CHAT, ENTRY_USER,
               ENTRY_ONE_MESSAGE]
    texts = [f"{e['title']} {e['description']}" for e in entries]
    meta = IndexMeta(profile="t", graph_version="v1.0", embedder="none",
                     paraphraser="none", built_at="2026-01-01T00:00:00+00:00",
                     n_operations=len(entries), n_texts=len(texts),
                     join_coverage=1.0, ablations=[])

    def build(policy: LabelPolicy, responses: dict):
        data = dict(fixture_data)
        data["responses"] = {
            **data["responses"],
            "GET /security/dataSecurityAndGovernance/sensitivityLabels":
                LABEL_DEFINITIONS,
            **responses,
        }
        transport = FakeGraphTransport(data, page_size=50)
        runtime.index = RetrievalIndex(entries, texts, list(range(len(texts))),
                                       None, meta)
        runtime.routes = RouteTable(ROUTES)
        runtime.labels = LabelGate(policy)
        runtime.transport_factory = lambda caller: transport
        return create_server(runtime), transport

    return build


def _policy(**kw) -> LabelPolicy:
    base = dict(enabled=True, blocked_labels=["Personal information"],
                require_extract_right=False)
    base.update(kw)
    return LabelPolicy(**base)


class TestFeatureSwitch:
    async def test_disabled_gate_changes_nothing(self, gated):
        server, transport = gated(
            LabelPolicy(enabled=False),
            {"GET /me/messages": {"value": [
                _message("m1", "Payroll", PERSONAL)]}},
        )
        async with client_for(server) as session:
            result = structured(await session.call_tool(
                "graph_get", {"path": "/me/messages"}))

        assert result["data"]["value"][0]["id"] == "m1"
        # No label machinery ran at all.
        assert not any(
            "sensitivityLabels" in r.path or "extractSensitivityLabels" in r.path
            for r in transport.sent
        )

    async def test_whoami_reports_the_policy(self, gated):
        server, _ = gated(_policy(), {})
        async with client_for(server) as session:
            result = structured(await session.call_tool("graph_whoami", {}))
        policy = result["label_policy"]
        assert policy["enabled"] is True
        assert policy["blocked_labels"] == ["Personal information"]


class TestMail:
    async def test_labeled_message_is_withheld_and_others_survive(self, gated):
        server, _ = gated(_policy(), {"GET /me/messages": {"value": [
            _message("m-clean", "Team offsite", None),
            _message("m-personal", "Payroll: national ID", PERSONAL),
        ]}})
        async with client_for(server) as session:
            result = structured(await session.call_tool(
                "graph_get", {"path": "/me/messages"}))

        clean, withheld = result["data"]["value"]
        assert clean["subject"] == "Team offsite"
        assert withheld["withheldByPolicy"] is True
        assert "Personal information" in withheld["withheldReason"]
        # The subject and body are gone; the id stays so the caller can see
        # that something was removed rather than silently getting a short list.
        assert "subject" not in withheld and "body" not in withheld
        assert withheld["id"] == "m-personal"
        assert any("Withheld 1 of 2" in n for n in result["notes"])

    async def test_label_expansion_is_added_to_the_request(self, gated):
        server, transport = gated(_policy(), {"GET /me/messages": {"value": []}})
        async with client_for(server) as session:
            await session.call_tool("graph_get", {"path": "/me/messages"})

        sent = [r for r in transport.sent if r.path == "/me/messages"][0]
        expand = sent.query["$expand"]
        assert PS_PUBLIC_STRINGS in expand
        assert f"MSIP_Label_{PERSONAL}_Enabled" in expand

    async def test_a_disabled_label_stamp_does_not_block(self, gated):
        """Exchange leaves MSIP properties behind with value False."""
        item = _message("m1", "Fine", None)
        item["singleValueExtendedProperties"] = _msip(PERSONAL, enabled="False")
        server, _ = gated(_policy(), {"GET /me/messages": {"value": [item]}})
        async with client_for(server) as session:
            result = structured(await session.call_tool(
                "graph_get", {"path": "/me/messages"}))
        assert result["data"]["value"][0]["subject"] == "Fine"


class TestFiles:
    def _files(self, *ids):
        return {"GET /me/drive/root/children": {
            "value": [{"id": i, "name": f"{i}.xlsx"} for i in ids]}}

    async def test_labeled_file_is_withheld(self, gated):
        server, _ = gated(_policy(), {
            **self._files("f-clean", "f-personal"),
            "POST /me/drive/items/f-clean/extractSensitivityLabels":
                {"value": {"labels": []}},
            "POST /me/drive/items/f-personal/extractSensitivityLabels":
                {"value": {"labels": [{"sensitivityLabelId": PERSONAL,
                                       "assignmentMethod": "standard"}]}},
        })
        async with client_for(server) as session:
            result = structured(await session.call_tool(
                "graph_get", {"path": "/me/drive/root/children"}))

        clean, withheld = result["data"]["value"]
        assert clean["name"] == "f-clean.xlsx"
        assert withheld["withheldByPolicy"] is True
        assert withheld["id"] == "f-personal"

    async def test_locked_file_fails_closed(self, gated):
        """423 Locked is an unread label, not an absent one."""
        server, _ = gated(_policy(), {
            **self._files("f-locked"),
            "POST /me/drive/items/f-locked/extractSensitivityLabels": {
                "@status": 423,
                "error": {"code": "fileDoubleKeyEncrypted", "message": "locked"},
            },
        })
        async with client_for(server) as session:
            result = structured(await session.call_tool(
                "graph_get", {"path": "/me/drive/root/children"}))

        item = result["data"]["value"][0]
        assert item["withheldByPolicy"] is True
        assert "could not be determined" in item["withheldReason"]
        assert "fileDoubleKeyEncrypted" in item["withheldReason"]

    async def test_fail_open_allows_an_undetermined_file(self, gated):
        server, _ = gated(_policy(fail_closed=False), {
            **self._files("f-locked"),
            "POST /me/drive/items/f-locked/extractSensitivityLabels": {
                "@status": 423, "error": {"code": "fileDoubleKeyEncrypted"},
            },
        })
        async with client_for(server) as session:
            result = structured(await session.call_tool(
                "graph_get", {"path": "/me/drive/root/children"}))
        assert result["data"]["value"][0]["name"] == "f-locked.xlsx"

    async def test_budget_exhaustion_withholds_the_remainder(self, gated):
        server, _ = gated(_policy(max_file_checks=1), {
            **self._files("f1", "f2"),
            "POST /me/drive/items/f1/extractSensitivityLabels":
                {"value": {"labels": []}},
        })
        async with client_for(server) as session:
            result = structured(await session.call_tool(
                "graph_get", {"path": "/me/drive/root/children"}))

        first, second = result["data"]["value"]
        assert first["name"] == "f1.xlsx"
        assert "budget" in second["withheldReason"]
        assert any("$top" in n for n in result["notes"])


class TestExtractRight:
    """Copilot's own rule, ported: no EXTRACT means an AI app may not use it."""

    def _confidential_file(self):
        return {
            "GET /me/drive/root/children": {"value": [{"id": "f1", "name": "a.docx"}]},
            "POST /me/drive/items/f1/extractSensitivityLabels":
                {"value": {"labels": [{"sensitivityLabelId": CONFIDENTIAL}]}},
        }

    async def test_missing_extract_right_withholds(self, gated):
        server, _ = gated(_policy(require_extract_right=True), {
            **self._confidential_file(),
            "POST /security/dataSecurityAndGovernance/sensitivityLabels"
            "/computeRightsAndInheritance": {"contentRights": [{"rights": "view,print"}]},
        })
        async with client_for(server) as session:
            result = structured(await session.call_tool(
                "graph_get", {"path": "/me/drive/root/children"}))

        item = result["data"]["value"][0]
        assert item["withheldByPolicy"] is True
        assert "EXTRACT" in item["withheldReason"]

    async def test_extract_plus_view_allows(self, gated):
        server, _ = gated(_policy(require_extract_right=True), {
            **self._confidential_file(),
            "POST /security/dataSecurityAndGovernance/sensitivityLabels"
            "/computeRightsAndInheritance":
                {"contentRights": [{"rights": "view,extract,print"}]},
        })
        async with client_for(server) as session:
            result = structured(await session.call_tool(
                "graph_get", {"path": "/me/drive/root/children"}))
        assert result["data"]["value"][0]["name"] == "a.docx"

    async def test_unprotected_label_skips_the_rights_call(self, gated):
        """Only encryption-backed labels carry usage rights."""
        server, transport = gated(
            _policy(blocked_labels=[], require_extract_right=True),
            {"GET /me/drive/root/children": {"value": [{"id": "f1", "name": "a.docx"}]},
             "POST /me/drive/items/f1/extractSensitivityLabels":
                 {"value": {"labels": [{"sensitivityLabelId": PERSONAL}]}}},
        )
        async with client_for(server) as session:
            result = structured(await session.call_tool(
                "graph_get", {"path": "/me/drive/root/children"}))

        assert result["data"]["value"][0]["name"] == "a.docx"
        assert not any("computeRights" in r.path for r in transport.sent)


class TestUnsupportedSurfaces:
    async def test_chat_messages_fail_closed(self, gated):
        """Teams chat has no label read path in v1.0 at all."""
        server, _ = gated(_policy(), {
            "GET /me/chats/c1/messages": {"value": [{"id": "cm1", "body": {}}]},
        })
        async with client_for(server) as session:
            result = structured(await session.call_tool(
                "graph_get", {"path": "/me/chats/c1/messages"}))

        item = result["data"]["value"][0]
        assert item["withheldByPolicy"] is True
        assert "no label read path" in item["withheldReason"]


class TestAnnotateMode:
    async def test_annotate_reports_without_removing(self, gated):
        server, _ = gated(_policy(mode="annotate"), {
            "GET /me/messages": {"value": [
                _message("m-personal", "Payroll", PERSONAL)]},
        })
        async with client_for(server) as session:
            result = structured(await session.call_tool(
                "graph_get", {"path": "/me/messages"}))

        item = result["data"]["value"][0]
        assert item["subject"] == "Payroll"
        assert "withheldByPolicy" not in item
        assert any("would be withheld" in n for n in result["notes"])


class TestLookupFailure:
    async def test_unreadable_label_list_withholds_everything(self, gated):
        """If the gate cannot learn what to block, it must not pass content."""
        server, _ = gated(_policy(), {
            "GET /security/dataSecurityAndGovernance/sensitivityLabels": {
                "@status": 403, "error": {"code": "Authorization_RequestDenied"},
            },
            "GET /me/messages": {"value": [_message("m1", "Anything", None)]},
        })
        async with client_for(server) as session:
            result = structured(await session.call_tool(
                "graph_get", {"path": "/me/messages"}))

        item = result["data"]["value"][0]
        assert item["withheldByPolicy"] is True
        assert any("fail-closed" in n for n in result["notes"])


class TestPurview:
    def _mail(self):
        return {"GET /me/messages": {"value": [_message("m1", "Quote", None)]}}

    def _policy(self):
        return _policy(purview=PurviewSettings(
            enabled=True, application_location_id="app-guid"))

    async def test_block_verdict_withholds(self, gated):
        server, _ = gated(self._policy(), {
            **self._mail(),
            "POST /me/dataSecurityAndGovernance/protectionScopes/compute":
                {"value": [{"activities": "downloadText"}]},
            "POST /me/dataSecurityAndGovernance/processContent":
                {"policyActions": [{"action": "blockAccess"}]},
        })
        async with client_for(server) as session:
            result = structured(await session.call_tool(
                "graph_get", {"path": "/me/messages"}))

        item = result["data"]["value"][0]
        assert item["withheldByPolicy"] is True
        assert "DLP" in item["withheldReason"]

    async def test_no_policy_in_scope_skips_the_content_calls(self, gated):
        server, transport = gated(self._policy(), {
            **self._mail(),
            "POST /me/dataSecurityAndGovernance/protectionScopes/compute":
                {"value": []},
        })
        async with client_for(server) as session:
            result = structured(await session.call_tool(
                "graph_get", {"path": "/me/messages"}))

        assert result["data"]["value"][0]["subject"] == "Quote"
        assert not any("processContent" in r.path for r in transport.sent)


class TestScopeOfTheGate:
    """What the gate must NOT touch, and what it must not let slip past."""

    async def test_directory_objects_pass_through(self, gated):
        """A user has no content, so it cannot carry a content label.

        Screening it would withhold every directory read for a label that can
        never exist -- which is what an earlier version of this gate did.
        """
        server, transport = gated(_policy(), {})
        async with client_for(server) as session:
            result = structured(await session.call_tool("graph_get", {"path": "/me"}))

        assert result["data"]["displayName"]
        assert "withheldByPolicy" not in result["data"]
        # And it cost nothing: no label machinery ran.
        assert not any("SensitivityLabel" in r.path or "sensitivityLabels" in r.path
                       for r in transport.sent)

    async def test_write_echo_is_not_screened(self):
        """A write returns content the caller just supplied.

        graph_get is GET-only, so the screen is exercised directly. Passing no
        transport also proves the gate makes no Graph call on this path.
        """
        from graph_mcp.graph.request import GraphRequest

        screened = await LabelGate(_policy()).screen(
            transport=None,
            request=GraphRequest("POST", "/me/messages"),
            body={"id": "draft-1", "subject": "Draft"},
            response_type="microsoft.graph.message",
        )
        assert screened.body["subject"] == "Draft"
        assert screened.withheld == 0

    async def test_a_projection_without_id_is_still_screened(self, gated):
        """$select that drops `id` must not slip past the gate."""
        server, _ = gated(_policy(), {
            "GET /me/messages/m1": {"body": {"contentType": "text",
                                             "content": "secret"}},
        })
        async with client_for(server) as session:
            result = structured(await session.call_tool(
                "graph_get", {"path": "/me/messages/m1", "select": ["body"]}))

        assert result["data"]["withheldByPolicy"] is True
        assert "could not be determined" in result["data"]["withheldReason"]
