"""Shared fixtures: a fully functional server over a fixture tenant.

No network, no credentials, no index rebuild. The retrieval index is stubbed
with a handful of operations so server tests stay fast and independent of the
built artifact; retrieval quality itself is measured by eval/, not here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from graph_mcp.graph.shaping import Shaper
from graph_mcp.graph.transport import FakeGraphTransport
from graph_mcp.policy.routes import RouteTable
from graph_mcp.policy.writes import WritePolicy
from graph_mcp.retrieval.index import IndexMeta, RetrievalIndex
from graph_mcp.runtime import Runtime
from graph_mcp.schema.csdl import TypeIndex
from graph_mcp.server import create_server

FIXTURE_TENANT = Path(__file__).parent.parent / "src/graph_mcp/fixtures/tenant.json"
CONFIG = Path(__file__).parent.parent / "config"

_ENTRIES = [
    {
        "key": "GET /me/messages", "path": "/me/messages", "method": "GET",
        "operation_id": "me.ListMessages", "tags": ["users.message"],
        "path_params": [], "query_params": ["$select", "$filter", "$top"],
        "response_type": "microsoft.graph.message", "request_type": "",
        "returns_collection": True, "summary": "Get messages",
        "spec_description": "", "title": "List messages",
        "description": "Get the messages in the signed-in user's mailbox.",
        "intro": "", "doc_url": "https://learn.microsoft.com/graph/api/user-list-messages",
        "permissions": {"least": {"delegated_work": ["Mail.ReadBasic"]},
                        "higher": {"delegated_work": ["Mail.Read"]}},
        "utterances": [], "overloads": 1, "indexed": True,
    },
    {
        "key": "GET /me", "path": "/me", "method": "GET",
        "operation_id": "me.GetUser", "tags": ["users.user"],
        "path_params": [], "query_params": ["$select"],
        "response_type": "microsoft.graph.user", "request_type": "",
        "returns_collection": False, "summary": "Get user", "spec_description": "",
        "title": "Get user", "description": "Retrieve the signed-in user.",
        "intro": "", "doc_url": "https://learn.microsoft.com/graph/api/user-get",
        "permissions": {"least": {"delegated_work": ["User.Read"]}, "higher": {}},
        "utterances": [], "overloads": 1, "indexed": True,
    },
    {
        "key": "POST /me/sendMail", "path": "/me/sendMail", "method": "POST",
        "operation_id": "me.sendMail", "tags": ["users.user"],
        "path_params": [], "query_params": [],
        "response_type": "", "request_type": "microsoft.graph.message",
        "returns_collection": False, "summary": "Invoke action sendMail",
        "spec_description": "", "title": "user: sendMail",
        "description": "Send the message specified in the request body.",
        "intro": "", "doc_url": "https://learn.microsoft.com/graph/api/user-sendmail",
        "permissions": {"least": {"delegated_work": ["Mail.Send"]}, "higher": {}},
        "utterances": [], "overloads": 1, "indexed": True,
    },
    {
        # camelCase path: the route table lowercases templates, so this is the
        # shape that exposed the catalog-lookup casing bug.
        "key": "GET /me/mailFolders", "path": "/me/mailFolders", "method": "GET",
        "operation_id": "me.ListMailFolders", "tags": ["users.mailFolder"],
        "path_params": [], "query_params": ["$select", "$top"],
        "response_type": "microsoft.graph.mailFolder", "request_type": "",
        "returns_collection": True, "summary": "Get mailFolders",
        "spec_description": "", "title": "List mailFolders",
        "description": "Get the mail folder collection under the root folder.",
        "intro": "", "doc_url": "", "overloads": 1, "indexed": True,
        "permissions": {"least": {"delegated_work": ["Mail.ReadBasic"]}, "higher": {}},
        "utterances": [],
    },
    {
        "key": "GET /auditLogs/signIns", "path": "/auditLogs/signIns", "method": "GET",
        "operation_id": "auditLogs.ListSignIns", "tags": ["auditLogs"],
        "path_params": [], "query_params": ["$filter"],
        "response_type": "microsoft.graph.signIn", "request_type": "",
        "returns_collection": True, "summary": "Get signIns", "spec_description": "",
        "title": "List signIns", "description": "Retrieve sign-in events.",
        "intro": "", "doc_url": "", "overloads": 1, "indexed": True,
        "permissions": {"least": {"delegated_work": ["AuditLog.Read.All"]}, "higher": {}},
        "utterances": [],
    },
]

_ROUTES = {
    "/me": ["GET"],
    "/me/messages": ["GET", "POST"],
    "/me/messages/{}": ["GET", "PATCH", "DELETE"],
    "/me/sendmail": ["POST"],
    "/me/events": ["GET", "POST"],
    "/me/manager": ["GET"],
    "/me/mailfolders": ["GET"],
    "/me/presence": ["GET"],
    "/users": ["GET"],
    "/users/{}": ["GET"],
    "/groups": ["GET"],
    "/auditlogs/signins": ["GET"],
}


@pytest.fixture
def transport() -> FakeGraphTransport:
    return FakeGraphTransport.load(FIXTURE_TENANT, page_size=5)


@pytest.fixture
def runtime(transport: FakeGraphTransport) -> Runtime:
    texts = [f"{e['title']} {e['description']}" for e in _ENTRIES]
    meta = IndexMeta(
        profile="test", graph_version="v1.0", embedder="none", paraphraser="none",
        built_at="2026-01-01T00:00:00+00:00", n_operations=len(_ENTRIES),
        n_texts=len(texts), join_coverage=1.0, ablations=[],
    )
    # vectors=None keeps these tests lexical-only: fast, deterministic, and no
    # embedding model needed.
    index = RetrievalIndex(_ENTRIES, texts, list(range(len(texts))), None, meta)
    return Runtime(
        index=index,
        routes=RouteTable(_ROUTES),
        types=TypeIndex(Path(".cache/metadata-v1.0.xml")),
        shaper=Shaper.load(CONFIG / "select_defaults.yaml"),
        writes=WritePolicy.load(CONFIG / "write_allowlist.yaml"),
        transport=transport,
    )


@pytest.fixture
def server(runtime: Runtime):
    return create_server(runtime)


@pytest.fixture
def writable_runtime(runtime: Runtime) -> Runtime:
    runtime.writes.enabled = True
    return runtime


@pytest.fixture
def writable_server(writable_runtime: Runtime):
    return create_server(writable_runtime)


@pytest.fixture(autouse=True)
def quiet_logs():
    """The server logs tool errors; tests provoke those deliberately."""
    import logging

    logging.disable(logging.ERROR)
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture
def anyio_backend():
    return "asyncio"
