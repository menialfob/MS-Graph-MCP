"""Reading Microsoft Purview sensitivity labels from Graph.

Every label-reading API in Graph v1.0 is a **POST**, including the ones that
are semantically reads. That is why none of this goes through `graph_get`
(GET-only) or `graph_write` (a gated mutation path, disabled by default): a
label check is a read the server performs on its own behalf, and it must not be
something a caller can decline. These helpers are called internally by the
label gate, never exposed as tools.

Four surfaces, each with a different shape:

* **Label definitions** -- `GET /security/dataSecurityAndGovernance/sensitivityLabels`
  turns a configured name such as "Personal information" into the GUID that
  actually appears on content, and reports `hasProtection`, which says whether
  the label applies encryption.
* **Files** -- `POST /drives/{d}/items/{i}/extractSensitivityLabels`. One call
  per item; `driveItem` carries no label property in v1.0, so there is no
  cheaper way.
* **Mail** -- no label property and no action either. The label lives in
  `MSIP_Label_{guid}_*` named MAPI properties, which come back through
  `$expand=singleValueExtendedProperties`. That expansion is injected into the
  caller's own request, so mail costs no extra round trip.
* **Usage rights** -- `POST .../sensitivityLabels/computeRightsAndInheritance`
  returns the caller's `usageRights` for a label. Testing for `extract` is what
  Microsoft 365 Copilot does for encryption-backed content, and is the one part
  of this that is a faithful port of first-party behaviour.

See docs/SENSITIVITY-LABELS-BLOCKING.md for the measurements behind the choice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from graph_mcp.graph.request import GraphRequest

# Named MAPI properties live in the PS_PUBLIC_STRINGS namespace. This GUID is
# the namespace, and is the same for every tenant and every label -- the label's
# own GUID appears in the property *name*, not here. Getting these two confused
# produces a filter that silently matches nothing.
PS_PUBLIC_STRINGS = "{00020329-0000-0000-C000-000000000046}"

_LABEL_PROPERTY = re.compile(
    r"MSIP_Label_([0-9a-fA-F-]{36})_Enabled\b"
)

SENSITIVITY_LABELS_PATH = "/security/dataSecurityAndGovernance/sensitivityLabels"
COMPUTE_RIGHTS_PATH = f"{SENSITIVITY_LABELS_PATH}/computeRightsAndInheritance"

# usageRights is a flags enum; `extract` is the right Copilot requires alongside
# `view` before it will use protected content.
EXTRACT_RIGHT = "extract"
VIEW_RIGHT = "view"


@dataclass(frozen=True)
class LabelDefinition:
    id: str
    name: str
    has_protection: bool = False
    priority: int | None = None


@dataclass
class LabelReadout:
    """What could be determined about one item's labels."""

    label_ids: set[str] = field(default_factory=set)
    # None when the question was never asked; a string when it was asked and
    # failed. A failed readout is what drives fail-closed behaviour.
    error: str | None = None
    determined: bool = True


# --------------------------------------------------------------------------
# Label definitions
# --------------------------------------------------------------------------


async def list_label_definitions(transport) -> list[LabelDefinition]:
    """Every sensitivity label defined in the tenant.

    Tenant metadata, not user data -- safe for the caller-agnostic cache in the
    gate, unlike anything derived from a specific user's content.
    """
    response = await transport.send(
        GraphRequest("GET", SENSITIVITY_LABELS_PATH, query={"$top": "200"})
    )
    if not response.ok:
        raise LabelLookupError(
            f"could not list sensitivity labels: {response.status} "
            f"{_error_code(response.body)}"
        )
    out: list[LabelDefinition] = []
    for raw in response.body.get("value", []) or []:
        label_id = raw.get("id")
        if not label_id:
            continue
        out.append(
            LabelDefinition(
                id=str(label_id),
                # v1.0 returns `name`; some builds also carry `displayName`.
                name=str(raw.get("name") or raw.get("displayName") or ""),
                has_protection=bool(raw.get("hasProtection")),
                priority=raw.get("priority"),
            )
        )
        for sub in raw.get("sublabels", []) or []:
            if sub.get("id"):
                out.append(
                    LabelDefinition(
                        id=str(sub["id"]),
                        name=str(sub.get("name") or sub.get("displayName") or ""),
                        has_protection=bool(sub.get("hasProtection")),
                        priority=sub.get("priority"),
                    )
                )
    return out


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


async def extract_file_labels(transport, item_path: str) -> LabelReadout:
    """Labels assigned to one driveItem.

    `item_path` is the item's own Graph path, e.g. '/me/drive/items/{id}'.
    A 423 Locked means the file could not be opened to read its label
    (double-key encrypted, decryption unsupported or deferred) -- that is an
    undetermined readout, not an absent label.
    """
    response = await transport.send(
        GraphRequest("POST", f"{item_path}/extractSensitivityLabels")
    )
    if not response.ok:
        code = _error_code(response.body)
        return LabelReadout(
            determined=False,
            error=f"{response.status} {code}" if code else str(response.status),
        )

    # The action returns {"value": {"labels": [...]}}; some responses hoist
    # `labels` to the top level.
    payload = response.body.get("value") or response.body
    if not isinstance(payload, dict):
        return LabelReadout(determined=False, error="unrecognised response shape")
    ids = {
        str(entry["sensitivityLabelId"])
        for entry in payload.get("labels", []) or []
        if isinstance(entry, dict) and entry.get("sensitivityLabelId")
    }
    return LabelReadout(label_ids=ids)


# --------------------------------------------------------------------------
# Mail
# --------------------------------------------------------------------------


def mail_label_expand(label_ids: list[str]) -> str | None:
    """`$expand` clause that brings back the MSIP_Label properties we care about.

    Graph's extended-property filter supports `id eq '...'` joined by `or`, with
    no prefix matching, so each label of interest has to be named explicitly.
    That means this sees only the labels it was told to look for: it answers
    "does this message carry a blocked label?" and cannot answer "what label
    does this message carry?". Sufficient for blocking, which is the feature.
    """
    if not label_ids:
        return None
    clauses = " or ".join(
        f"id eq 'String {PS_PUBLIC_STRINGS} Name MSIP_Label_{label_id}_Enabled'"
        for label_id in label_ids
    )
    return f"singleValueExtendedProperties($filter={clauses})"


def mail_labels_from_item(item: dict[str, Any]) -> LabelReadout:
    """Label GUIDs carried by an expanded message.

    An absent `singleValueExtendedProperties` key means the expansion did not
    run, which is undetermined rather than unlabeled.
    """
    if "singleValueExtendedProperties" not in item:
        return LabelReadout(determined=False, error="label properties not expanded")

    ids: set[str] = set()
    for prop in item.get("singleValueExtendedProperties") or []:
        if not isinstance(prop, dict):
            continue
        match = _LABEL_PROPERTY.search(str(prop.get("id", "")))
        # Exchange writes the string "True"; a label that was applied and later
        # removed can linger with "False".
        if match and str(prop.get("value", "")).strip().lower() == "true":
            ids.add(match.group(1).lower())
    return LabelReadout(label_ids=ids)


# --------------------------------------------------------------------------
# Usage rights -- the Copilot-equivalent check
# --------------------------------------------------------------------------


async def compute_usage_rights(
    transport, label_id: str, user_email: str
) -> tuple[set[str], str | None]:
    """The caller's usage rights for content carrying `label_id`.

    Returns (rights, error). An error is an undetermined answer and the caller
    decides what to do with it; it is never an implicit grant.
    """
    request = GraphRequest(
        "POST",
        COMPUTE_RIGHTS_PATH,
        body={
            "protectedContents": [{"labelId": label_id, "format": "file"}],
            "delegatedUserEmail": user_email,
            "supportedContentFormats": ["file"],
        },
    )
    response = await transport.send(request)
    if not response.ok:
        code = _error_code(response.body)
        return set(), f"{response.status} {code}" if code else str(response.status)

    rights: set[str] = set()
    for entry in response.body.get("contentRights", []) or []:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("rights")
        # A flags enum arrives as a comma-separated string.
        if isinstance(raw, str):
            rights.update(part.strip().lower() for part in raw.split(",") if part.strip())
        elif isinstance(raw, list):
            rights.update(str(part).strip().lower() for part in raw)
    return rights, None


def has_extract_right(rights: set[str]) -> bool:
    """Whether these rights permit an AI app to use the content.

    Mirrors Copilot: both VIEW and EXTRACT are required. `owner` implies the
    full set. Any of the exception sentinels means the rights could not be
    established, which is not a grant.
    """
    if "owner" in rights:
        return True
    return EXTRACT_RIGHT in rights and VIEW_RIGHT in rights


class LabelLookupError(RuntimeError):
    """A label lookup that the gate cannot proceed without."""


def _error_code(body: Any) -> str:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("code", ""))
    return ""
