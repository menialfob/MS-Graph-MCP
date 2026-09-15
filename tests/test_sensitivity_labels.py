"""Characterization tests: how the server treats Purview-labeled content.

These tests document what the server does *today* with content carrying a
Microsoft Purview sensitivity label such as "Personal information". They are
deliberately written as assertions about current behaviour, not about desired
behaviour: nothing in this codebase reads, checks or filters on a label, so a
labeled item is returned exactly like any other.

If label-aware filtering is ever implemented, these tests are expected to fail.
That failure is the point -- it marks the behaviour change. See
docs/SENSITIVITY-LABELS.md for the full investigation.
"""

from __future__ import annotations

import pytest

from graph_mcp.graph.labels import PS_PUBLIC_STRINGS
from graph_mcp.graph.transport import FakeGraphTransport
from tests.mcp_client import client_for, structured

pytestmark = pytest.mark.anyio

# The GUID Purview assigns to the built-in "Personal information" label.
PI_LABEL = "defa4170-0d19-0005-0007-bc88714345d2"

# A Purview label on a mail item lives in MSIP_Label_<guid>_* named MAPI
# properties in the PS_PUBLIC_STRINGS namespace -- the GUID in braces is that
# namespace, not the label. `sensitivity` is the separate legacy Outlook flag.
_LABELED_MESSAGE = {
    "id": "msg-personal-info",
    "subject": "Payroll: Q3 salary and national ID for J. Lind",
    "from": {"emailAddress": {"address": "hr@contoso.com"}},
    "receivedDateTime": "2026-09-11T09:00:00Z",
    "isRead": False,
    "sensitivity": "personal",
    "singleValueExtendedProperties": [
        {"id": f"String {PS_PUBLIC_STRINGS} Name MSIP_Label_{PI_LABEL}_Name",
         "value": "Personal information"},
        {"id": f"String {PS_PUBLIC_STRINGS} Name MSIP_Label_{PI_LABEL}_Enabled",
         "value": "True"},
    ],
    "body": {"contentType": "text",
             "content": "Salary 812,000 DKK. National ID 010190-1234."},
}


@pytest.fixture
def labeled_server(runtime, fixture_data):
    """A server whose mailbox holds one clearly labeled, PII-bearing message."""
    data = dict(fixture_data)
    data["responses"] = dict(data["responses"])
    data["responses"]["GET /me/messages"] = {"value": [_LABELED_MESSAGE]}
    transport = FakeGraphTransport(data, page_size=5)
    runtime.transport_factory = lambda caller: transport

    from graph_mcp.server import create_server
    return create_server(runtime)


class TestLabeledContentIsReturned:
    async def test_labeled_message_is_returned_not_withheld(self, labeled_server):
        """A "Personal information" label does not prevent retrieval."""
        async with client_for(labeled_server) as session:
            result = structured(await session.call_tool(
                "graph_get", {"path": "/me/messages"}))

        items = result["data"]["value"]
        assert len(items) == 1, "the labeled message was not filtered out"
        assert items[0]["id"] == "msg-personal-info"

    async def test_subject_leaks_pii_even_under_the_default_select(self, labeled_server):
        """The default $select still returns the subject, which carries PII."""
        async with client_for(labeled_server) as session:
            result = structured(await session.call_tool(
                "graph_get", {"path": "/me/messages"}))

        subject = result["data"]["value"][0]["subject"]
        assert "salary" in subject.lower() and "national ID" in subject

    async def test_default_select_asks_graph_for_no_label_property(self, labeled_server):
        """Worse than unfiltered: the label is never even requested.

        config/select_defaults.yaml lists no label-bearing property for any
        type, so against real Graph the label never comes back at all and a
        downstream consumer that *wanted* to filter has nothing to filter on.

        This asserts on the outgoing request rather than the response, because
        that is the server's actual behaviour -- FakeGraphTransport does not
        model $select projection, real Graph does.
        """
        async with client_for(labeled_server) as session:
            result = structured(await session.call_tool(
                "graph_get", {"path": "/me/messages"}))

        url = result["request"]["url"]
        assert "$select=" in url
        selected = url.split("$select=")[1].split("&")[0].lower()
        for label_property in ("sensitivity", "label", "extendedproperties",
                               "classification"):
            assert label_property not in selected, (
                f"unexpected label-bearing property in the default $select: {selected}"
            )

    async def test_labeled_body_is_readable_with_an_explicit_select(self, labeled_server):
        """The heavy-field drop is a token-budget heuristic, not a control."""
        async with client_for(labeled_server) as session:
            result = structured(await session.call_tool(
                "graph_get",
                {"path": "/me/messages", "select": ["id", "subject", "body", "content"]}))

        body = result["data"]["value"][0]["body"]["content"]
        assert "010190-1234" in body, "the labeled body was returned in full"


class TestLabelsCanBeTargeted:
    async def test_sensitivity_filter_is_passed_through_to_graph(self, labeled_server):
        """The legacy `sensitivity` flag works as a selector *for* such items.

        `graph.sensitivity` (normal|personal|private|confidential) is the old
        Outlook item flag, not a Purview label -- but the server forwards a
        filter on it unchanged, so it narrows results *to* flagged content
        rather than away from it.
        """
        async with client_for(labeled_server) as session:
            result = structured(await session.call_tool(
                "graph_get",
                {"path": "/me/messages", "filter": "sensitivity eq 'personal'",
                 "select": ["id", "subject", "sensitivity"]}))

        assert "sensitivity%20eq%20'personal'" in result["request"]["url"]


class TestLabelWritesAreNotReachable:
    """The one genuine mitigation: labels cannot be changed through this server."""

    async def test_assign_sensitivity_label_is_not_allowlisted(self, writable_server):
        async with client_for(writable_server) as session:
            result = await session.call_tool("graph_write", {
                "method": "POST",
                "path": "/me/drive/items/file-1/microsoft.graph.assignSensitivityLabel",
                "body": {"sensitivityLabelId": PI_LABEL},
                "dry_run": True,
            })
        assert result.is_error, "label assignment must not be executable"
