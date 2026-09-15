"""OData query construction and the advanced-query rules.

The rule that bites every Graph integration: on directory objects (users,
groups, applications, devices, service principals and their relationships),
``$count``, ``$search``, and the less common ``$filter`` operators are only
served when the request carries BOTH ``ConsistencyLevel: eventual`` and
``$count=true``. Without them Graph returns a 400 whose message does not
mention consistency level at all, so a model left to itself will retry the same
broken request.

We detect the condition and add the header and parameter, rather than making
the caller know. Everything added this way is reported back in the response
metadata so the behaviour is visible rather than magic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from graph_mcp.graph.request import GraphRequest

# Collections served by the directory (Entra) rather than a workload. Only
# these need the advanced-query dance.
_DIRECTORY_ROOTS = (
    "/users", "/groups", "/applications", "/servicePrincipals", "/devices",
    "/directoryObjects", "/directoryRoles", "/administrativeUnits", "/contacts",
)
_DIRECTORY_SEGMENTS = (
    "/members", "/owners", "/memberOf", "/transitiveMembers",
    "/transitiveMemberOf", "/directReports", "/appRoleAssignments",
)

# Operators that force advanced query on directory objects.
_ADVANCED_FILTER_RE = re.compile(
    r"\b(endsWith|ne|not)\b|\bstartsWith\(.*\)\s*eq\s*false", re.IGNORECASE
)

ODATA_PARAMS = ("select", "filter", "expand", "orderby", "search", "top", "skip", "count")


@dataclass
class QueryPlan:
    request: GraphRequest
    notes: list[str]


def is_directory_resource(path: str) -> bool:
    lowered = path.lower()
    if any(lowered.startswith(root.lower()) for root in _DIRECTORY_ROOTS):
        return True
    return any(seg.lower() in lowered for seg in _DIRECTORY_SEGMENTS)


def needs_advanced_query(path: str, filter_: str | None, search: str | None, count: bool) -> bool:
    if not is_directory_resource(path):
        return False
    if search or count:
        return True
    return bool(filter_ and _ADVANCED_FILTER_RE.search(filter_))


def build(
    method: str,
    path: str,
    *,
    version: str = "v1.0",
    select: list[str] | None = None,
    filter: str | None = None,
    expand: list[str] | None = None,
    orderby: list[str] | None = None,
    search: str | None = None,
    top: int | None = None,
    skip: int | None = None,
    count: bool = False,
    body: dict | None = None,
    extra_headers: dict[str, str] | None = None,
) -> QueryPlan:
    """Assemble a GraphRequest, applying the advanced-query rules."""
    query: dict[str, str] = {}
    notes: list[str] = []

    if select:
        query["$select"] = ",".join(select)
    if filter:
        query["$filter"] = filter
    if expand:
        query["$expand"] = ",".join(expand)
    if orderby:
        query["$orderby"] = ",".join(orderby)
    if search:
        # Graph requires the search term quoted; quote it if the caller did not.
        query["$search"] = search if search.startswith('"') else f'"{search}"'
    if top is not None:
        query["$top"] = str(top)
    if skip is not None:
        query["$skip"] = str(skip)
    if count:
        query["$count"] = "true"

    headers = dict(extra_headers or {})

    if needs_advanced_query(path, filter, search, count):
        if headers.get("ConsistencyLevel") != "eventual":
            headers["ConsistencyLevel"] = "eventual"
            notes.append(
                "Added 'ConsistencyLevel: eventual': $search, $count and operators "
                "like endsWith/ne on directory objects require advanced queries."
            )
        if query.get("$count") != "true":
            query["$count"] = "true"
            notes.append("Added '$count=true', required alongside ConsistencyLevel: eventual.")

    return QueryPlan(
        GraphRequest(
            method=method.upper(), path=path, query=query,
            headers=headers, body=body, version=version,
        ),
        notes,
    )
