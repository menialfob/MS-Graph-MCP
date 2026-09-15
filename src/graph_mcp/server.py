"""MCP server for Microsoft Graph.

Graph v1.0 has 17,777 operations, so they cannot be MCP tools. Instead a small
fixed tool surface wraps a retrieval index over the operation catalog:

    search -> describe -> execute

Tool annotations follow the MCP specification: reads are marked read-only,
graph_write is marked destructive, and everything that reaches Graph is
open-world. Structured output schemas are derived from the return types, so
clients that understand structured content get typed results and clients that
do not still get readable text.

This server is hosted remotely over Streamable HTTP, serving many users from
one process. Every tool resolves the calling user from the request's access
token and threads it through to the transport; no identity, token or scope is
ever cached on the server. Run it with `python -m graph_mcp.http`; see
docs/DEPLOYMENT.md.

The server talks to Graph only through `GraphTransport`. It defaults to a
fixture tenant, so it runs and is fully exercisable with no credentials.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from graph_mcp.caller import Caller, current_caller
from graph_mcp.graph import odata
from graph_mcp.graph.errors import translate
from graph_mcp.graph.request import GraphRequest
from graph_mcp.graph.transport import next_link_to_request
from graph_mcp.policy.writes import confirm_token, verify
from graph_mcp.runtime import Runtime, build_runtime

# Server-side logging goes to stderr, not over the protocol: MCP's logging
# capability is deprecated as of protocol version 2026-07-28 (SEP-2577), and
# every request this server makes is already returned to the caller in the
# result's `request` field, which is the record that actually matters.
logger = logging.getLogger("graph_mcp")

# --------------------------------------------------------------------------
# Result types. These become the tools' output schemas.
# --------------------------------------------------------------------------


@dataclass
class OperationHit:
    operation_id: str
    method: str
    path: str
    title: str
    summary: str
    score: float
    least_privilege_scope: list[str]
    caller_has_scope: bool | None
    returns_collection: bool
    doc_url: str


@dataclass
class SearchResult:
    query: str
    candidates: list[OperationHit]
    note: str = ""


@dataclass
class OperationDetail:
    operation_id: str
    method: str
    path: str
    title: str
    description: str
    path_parameters: list[str]
    query_parameters: list[str]
    request_type: str
    response_type: str
    returns_collection: bool
    permissions: dict[str, Any]
    doc_url: str
    writable: bool
    notes: list[str] = field(default_factory=list)


@dataclass
class PropertyInfo:
    name: str
    type: str
    nullable: bool
    is_collection: bool


@dataclass
class TypeDetail:
    name: str
    kind: str
    base_type: str
    properties: list[PropertyInfo]
    navigation_properties: list[PropertyInfo]
    members: list[str]
    note: str = ""


@dataclass
class GraphResult:
    request: dict[str, Any]
    status: int
    data: dict[str, Any]
    item_count: int | None = None
    cursor: str | None = None
    has_more: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class WritePlan:
    executed: bool
    request: dict[str, Any]
    confirm_token: str | None = None
    status: int | None = None
    data: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class WhoAmI:
    id: str
    display_name: str
    user_principal_name: str
    granted_scopes: list[str]
    catalog_profile: str
    graph_version: str
    indexed_operations: int
    executable_routes: int
    writes_enabled: bool
    transport: str
    label_policy: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _hit(entry: dict, score: float, granted: set[str] | None) -> OperationHit:
    least = entry.get("permissions", {}).get("least", {}).get("delegated_work", [])
    has_scope: bool | None = None
    if granted is not None:
        # Any of the least-privileged scopes, or a higher-privileged
        # alternative, is enough to make the call.
        higher = entry.get("permissions", {}).get("higher", {}).get("delegated_work", [])
        acceptable = set(least) | set(higher)
        has_scope = bool(acceptable & granted) if acceptable else None
    return OperationHit(
        operation_id=entry["key"],
        method=entry["method"],
        path=entry["path"],
        title=entry.get("title") or entry.get("summary", ""),
        summary=(entry.get("description") or entry.get("spec_description") or "")[:240],
        score=round(score, 5),
        least_privilege_scope=least,
        caller_has_scope=has_scope,
        returns_collection=entry.get("returns_collection", False),
        doc_url=entry.get("doc_url", ""),
    )


def _entry_for_route(rt: Runtime, method: str, template: str) -> dict | None:
    """Catalog entry for a matched route.

    Route templates are normalised to lowercase while catalog keys preserve the
    description's camelCase, so these must be matched case-insensitively.
    """
    return rt.entries_by_normalized_key.get(f"{method.upper()} {template.lower()}")


def _lookup(rt: Runtime, operation_id: str) -> dict:
    entry = rt.entries_by_key.get(operation_id)
    if entry is None:
        raise ToolError(
            f"Unknown operation_id '{operation_id}'. It must be exactly as returned "
            "by graph_search_operations, e.g. 'GET /me/messages'."
        )
    return entry


async def _execute(
    rt: Runtime,
    caller: Caller,
    request: GraphRequest,
    notes: list[str],
    entry: dict | None,
    transport=None,
) -> GraphResult:
    """Send a prepared request as `caller` and shape what comes back.

    `transport` is passed in when the caller already built one -- graph_get does,
    because the label gate needs it before the request is assembled.
    """
    transport = transport or rt.transport_for(caller)
    response = await transport.send(request)

    if not response.ok:
        required = (entry or {}).get("permissions", {}).get("least", {}).get(
            "delegated_work", []
        )
        error = translate(
            response.status, response.body, response.headers,
            required_scopes=required, granted_scopes=list(caller.scopes),
        )
        # A Graph-side failure is a tool error: the model should see it and
        # adapt, not receive it as a successful result it might misread.
        raise ToolError(
            f"Graph returned {error.status} ({error.code}): {error.message}\n"
            f"{error.guidance}"
        )

    body = response.body
    if rt.labels.enabled:
        # Withhold labeled items before anything is shaped or returned. This is
        # the last point at which the data is still only in this process.
        screened = await rt.labels.screen(
            transport=transport, request=request, body=body,
            response_type=(entry or {}).get("response_type", ""),
        )
        body, notes = screened.body, [*notes, *screened.notes]

    selected = request.query.get("$select", "").split(",") if request.query.get("$select") else None
    shaped = rt.shaper.shape(body, selected=selected)
    notes = [*notes, *shaped.notes]

    cursor = None
    if link := response.next_link:
        cursor = rt.cursors.put(
            caller.subject, link, f"{request.method} {request.path}", page=1
        )
        notes.append("More results available; pass the cursor to graph_next_page.")
    # The raw nextLink is a long opaque skiptoken URL; the cursor replaces it.
    shaped.body.pop("@odata.nextLink", None)

    items = shaped.body.get("value")
    return GraphResult(
        request=request.describe(),
        status=response.status,
        data=shaped.body,
        item_count=len(items) if isinstance(items, list) else None,
        cursor=cursor,
        has_more=cursor is not None,
        notes=notes,
    )


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------


def create_server(
    runtime: Runtime | None = None,
    *,
    auth_settings=None,
    token_verifier=None,
) -> MCPServer:
    """Build the server.

    `auth_settings` and `token_verifier` are supplied by the HTTP entry point
    (graph_mcp.http). Left unset, every request is attributed to one local
    developer -- only appropriate for a localhost-bound development server.
    """
    rt = runtime or build_runtime()

    server = MCPServer(
        name="microsoft-graph",
        title="Microsoft Graph",
        version="0.1.0",
        auth=auth_settings,
        token_verifier=token_verifier,
        instructions=(
            "Query Microsoft Graph on behalf of the signed-in user.\n\n"
            "Microsoft Graph has ~17,800 operations, so they are not exposed as "
            "individual tools. The workflow is:\n"
            "1. graph_search_operations - describe the intent in natural language "
            "to find candidate operations.\n"
            "2. graph_describe_operation - inspect the chosen operation's "
            "parameters, body and required permissions.\n"
            "3. graph_describe_type - look up an entity's real property names "
            "BEFORE writing a $select or $filter; guessing produces errors that "
            "do not name the offending property.\n"
            "4. graph_get / graph_write - execute.\n\n"
            "If you already know the exact Graph path you may call graph_get "
            "directly; the path is validated either way. Use graph_next_page with "
            "a returned cursor rather than asking for large pages."
        ),
    )

    @server.tool(
        name="graph_search_operations",
        title="Find Graph operations",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def graph_search_operations(
        intent: str,
        top_k: int = 8,
        method: Literal["any", "GET", "POST", "PATCH", "PUT", "DELETE"] = "any",
        only_callable: bool = False,
    ) -> SearchResult:
        """Find Microsoft Graph operations matching a natural-language intent.

        Start here when you do not already know the exact Graph endpoint.
        Describe what the user wants ("what meetings do I have today", "who
        reports to Anna") rather than guessing an API path.

        Returns ranked candidates with their required permission scopes. Pick
        one and pass its operation_id to graph_describe_operation or graph_get.
        """
        if not intent.strip():
            raise ToolError("intent must not be empty.")
        top_k = max(1, min(top_k, 25))

        caller = current_caller(rt.default_caller)
        granted: set[str] | None = set(caller.scopes) if caller.scopes else None
        if granted is None:
            # No scopes on the token (or no auth layer): ask the transport,
            # which in a delegated setup can report them from /me.
            try:
                identity = await rt.transport_for(caller).identity()
                granted = set(identity.get("scopes", [])) or None
            except Exception:  # noqa: BLE001 - scope annotation is best-effort
                granted = None

        results = rt.index.search(
            intent, top_k=top_k * 3, query_vector=rt.embed_query(intent)
        )

        hits: list[OperationHit] = []
        for entry_id, score in results:
            entry = rt.index.entries[entry_id]
            if method != "any" and entry["method"] != method:
                continue
            hit = _hit(entry, score, granted)
            if only_callable and hit.caller_has_scope is False:
                continue
            hits.append(hit)
            if len(hits) >= top_k:
                break

        note = ""
        if granted is None:
            note = "Could not read granted scopes; caller_has_scope is unknown."
        elif not hits:
            note = "No operations matched. Try different wording or a broader intent."
        return SearchResult(query=intent, candidates=hits, note=note)

    @server.tool(
        name="graph_describe_operation",
        title="Describe a Graph operation",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def graph_describe_operation(operation_id: str) -> OperationDetail:
        """Full detail for one operation: parameters, body type, permissions.

        Call this before executing an unfamiliar operation, especially one that
        takes a request body or path parameters.
        """
        entry = _lookup(rt, operation_id)
        notes: list[str] = []

        if entry["path_params"]:
            notes.append(
                "Supply path parameters as real ids: "
                + ", ".join(f"{{{p}}}" for p in entry["path_params"])
            )
        if entry.get("returns_collection"):
            notes.append(
                "Returns a collection: prefer $top plus the cursor over fetching "
                "everything at once."
            )
        if odata.is_directory_resource(entry["path"]):
            notes.append(
                "Directory resource: $search, $count and operators such as "
                "endsWith require advanced queries. graph_get adds the required "
                "ConsistencyLevel header and $count automatically."
            )
        if entry.get("overloads", 1) > 1:
            notes.append(
                f"This function has {entry['overloads']} overloads differing in "
                "their parameters; see the documentation link."
            )

        template = entry["path"]
        writable, why = rt.writes.permits(entry["method"], rt.routes.match(template).template
                                          if rt.routes.match(template) else template)
        if not writable and entry["method"] != "GET":
            notes.append(why)

        return OperationDetail(
            operation_id=entry["key"],
            method=entry["method"],
            path=entry["path"],
            title=entry.get("title", ""),
            description=entry.get("description") or entry.get("spec_description", ""),
            path_parameters=entry["path_params"],
            query_parameters=entry["query_params"],
            request_type=entry.get("request_type", ""),
            response_type=entry.get("response_type", ""),
            returns_collection=entry.get("returns_collection", False),
            permissions=entry.get("permissions", {}),
            doc_url=entry.get("doc_url", ""),
            writable=writable,
            notes=notes,
        )

    @server.tool(
        name="graph_describe_type",
        title="Describe a Graph entity type",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def graph_describe_type(type_name: str) -> TypeDetail:
        """Properties and relationships of a Graph entity type.

        Use this before composing $select, $filter or $orderby. Property names
        are case-sensitive and often differ from what seems natural, and Graph's
        error for an unknown property does not say which property was wrong.

        Accepts 'user', 'message' or the full 'microsoft.graph.user'.
        """
        info = rt.types.resolve(type_name)
        if info is None:
            near = rt.types.search(type_name)
            raise ToolError(
                f"Unknown type '{type_name}'."
                + (f" Did you mean: {', '.join(near[:5])}?" if near else "")
            )
        note = ""
        if info.base_type:
            note = f"Includes properties inherited from {info.base_type}."
        return TypeDetail(
            name=info.name,
            kind=info.kind,
            base_type=info.base_type,
            properties=[PropertyInfo(**p.to_dict()) for p in info.properties],
            navigation_properties=[
                PropertyInfo(**p.to_dict()) for p in info.navigation_properties
            ],
            members=info.members,
            note=note,
        )

    @server.tool(
        name="graph_get",
        title="Read from Graph",
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True),
    )
    async def graph_get(
        path: str,
        select: list[str] | None = None,
        filter: str | None = None,
        expand: list[str] | None = None,
        orderby: list[str] | None = None,
        search: str | None = None,
        top: int | None = None,
        count: bool = False,
    ) -> GraphResult:
        """Execute a read against Microsoft Graph.

        `path` is a Graph path such as '/me/messages' or
        '/users/{id}/manager' with real ids substituted. It is validated
        against the known operation catalog before any call is made.

        Pass OData options as separate arguments rather than embedding them in
        the path. A sensible $select is applied automatically when you do not
        supply one, to keep responses small.
        """
        path = "/" + path.strip().lstrip("/")
        allowed, match = rt.routes.allows("GET", path)
        if match is None:
            raise ToolError(
                f"'{path}' does not match any known Graph operation in this "
                f"deployment's catalog. Closest known paths: "
                f"{', '.join(rt.routes.suggest(path)) or 'none'}. "
                "Use graph_search_operations to find the right one."
            )
        if not allowed:
            raise ToolError(
                f"GET is not available on '{match.template}'. Allowed methods: "
                f"{', '.join(match.methods)}."
            )

        entry = _entry_for_route(rt, "GET", match.template)
        notes: list[str] = []
        if select is None and entry:
            if default := rt.shaper.default_select(entry.get("response_type", "")):
                select = default
                notes.append(
                    "Applied a default $select (" + ", ".join(default) + "). "
                    "Pass select explicitly to override."
                )

        caller = current_caller(rt.default_caller)
        transport = rt.transport_for(caller)

        if rt.labels.enabled:
            # Mail carries its label in extended properties, so the expansion
            # rides along with the caller's own request rather than costing a
            # second round trip.
            label_expand = await rt.labels.mail_expand(
                transport, (entry or {}).get("response_type", "")
            )
            if label_expand:
                expand = [*(expand or []), label_expand]
                notes.append(
                    "Expanded sensitivity-label properties; a label policy is active."
                )

        plan = odata.build(
            "GET", path, select=select, filter=filter, expand=expand,
            orderby=orderby, search=search, top=top, count=count,
        )
        logger.debug("GET %s (caller=%s)", plan.request.url(), caller.subject)
        return await _execute(
            rt, caller, plan.request, [*notes, *plan.notes], entry, transport
        )

    @server.tool(
        name="graph_next_page",
        title="Fetch the next page",
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True),
    )
    async def graph_next_page(cursor: str) -> GraphResult:
        """Fetch the next page of a previous graph_get result.

        Pass the cursor returned by graph_get or a prior graph_next_page.
        """
        caller = current_caller(rt.default_caller)
        # A cursor belonging to another caller is reported exactly like one
        # that does not exist -- the response must not confirm it is real.
        entry_ref = rt.cursors.get(caller.subject, cursor)
        if entry_ref is None:
            raise ToolError(
                f"Cursor '{cursor}' is unknown or expired. Re-run the original "
                "graph_get to start again."
            )
        request = next_link_to_request(entry_ref.next_link)
        match = rt.routes.match(request.path)
        entry = _entry_for_route(rt, "GET", match.template) if match else None
        result = await _execute(rt, caller, request, [], entry)
        result.notes.insert(0, f"Page {entry_ref.page + 1} of {entry_ref.operation}.")
        return result

    @server.tool(
        name="graph_write",
        title="Write to Graph",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    async def graph_write(
        method: Literal["POST", "PATCH", "PUT", "DELETE"],
        path: str,
        body: dict[str, Any] | None = None,
        dry_run: bool = True,
        confirm: str | None = None,
    ) -> WritePlan:
        """Execute a write against Microsoft Graph. Dry run by default.

        The first call returns the exact request that would be sent, plus a
        confirm token. Review it, then call again with dry_run=false and that
        token to execute. The token is bound to the request: changing the body
        or path invalidates it.
        """
        path = "/" + path.strip().lstrip("/")
        _, match = rt.routes.allows(method, path)
        if match is None:
            raise ToolError(
                f"'{path}' does not match any known Graph operation. Closest: "
                f"{', '.join(rt.routes.suggest(path)) or 'none'}."
            )
        if method not in match.methods:
            raise ToolError(
                f"{method} is not available on '{match.template}'. Allowed: "
                f"{', '.join(match.methods)}."
            )

        permitted, why = rt.writes.permits(method, match.template)
        if not permitted:
            raise ToolError(why)

        caller = current_caller(rt.default_caller)
        request = GraphRequest(method=method, path=path, body=body)
        token = confirm_token(request, caller.subject)

        if dry_run:
            return WritePlan(
                executed=False, request=request.describe(), confirm_token=token,
                notes=[
                    "Dry run - nothing was sent.",
                    "To execute, call again with dry_run=false and confirm set to "
                    "the token above. Check the request carefully first.",
                ],
            )

        if rt.writes.require_confirmation and not verify(
            request, confirm or "", caller.subject
        ):
            raise ToolError(
                "Missing or invalid confirm token. Run with dry_run=true, review "
                "the request, then pass the token it returns. A token only "
                "matches the exact request it was issued for."
            )

        logger.info("Executing %s %s (caller=%s)", method, path, caller.subject)
        entry = _entry_for_route(rt, method, match.template)
        result = await _execute(rt, caller, request, [], entry)
        return WritePlan(
            executed=True, request=result.request, status=result.status,
            data=result.data, notes=result.notes,
        )

    @server.tool(
        name="graph_whoami",
        title="Session and capability info",
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
    )
    async def graph_whoami() -> WhoAmI:
        """Who this server is acting as, and what it can do.

        Call this first in a session to learn the signed-in user and which
        permission scopes the token actually carries, so you do not propose
        operations that will be denied.
        """
        caller = current_caller(rt.default_caller)
        identity = await rt.transport_for(caller).identity()
        meta = rt.index.meta
        return WhoAmI(
            id=identity.get("id", ""),
            display_name=identity.get("display_name", ""),
            user_principal_name=identity.get("user_principal_name", ""),
            granted_scopes=list(caller.scopes) or list(identity.get("scopes", [])),
            catalog_profile=meta.profile,
            graph_version=meta.graph_version,
            indexed_operations=meta.n_operations,
            executable_routes=len(rt.routes.table),
            writes_enabled=rt.writes.enabled,
            transport=type(rt.transport_for(caller)).__name__,
            label_policy=rt.labels.describe(),
        )

    return server
