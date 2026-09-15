"""The sensitivity-label gate: withhold content carrying a blocked label.

Off by default. Everything in this module is inert until `label_policy.yaml`
sets `enabled: true`, so the server's behaviour is unchanged unless a deployment
opts in. See docs/SENSITIVITY-LABELS-BLOCKING.md for why it is built this way
and what it does not cover.

Three things are worth knowing before reading the code.

**The enforcement point is the response, not the request.** Refusing to make the
call would be a stronger boundary, but a label belongs to an item and items are
not known until the response arrives. So the gate screens what came back and
withholds items before they reach the model. The data crosses the wire into the
process; it does not cross into the context window. That is the honest limit of
what a client-side gate can do against Graph v1.0, and the reason the Copilot
Retrieval API -- which can filter before the query runs -- is the better answer
where a Copilot licence exists.

**Undetermined is treated as blocked.** A 423 Locked file, an unexpanded mail
property, a lookup that errored: each is a label the gate could not read, not a
label that is absent. With `fail_closed: true` these are withheld. Otherwise
every failure mode becomes a bypass, and the control is decorative.

**Metadata still leaks.** Withheld items keep their id so the caller knows
something was removed rather than silently receiving a short list. Subjects and
filenames are dropped with the rest of the item, but they were already disclosed
by any listing that ran before this gate was switched on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from graph_mcp.graph import labels as graph_labels
from graph_mcp.graph.request import GraphRequest

# Keys the gate writes onto a withheld item. Deliberately not `@`-prefixed:
# those read as OData annotations and this is not one.
WITHHELD_KEY = "withheldByPolicy"
REASON_KEY = "withheldReason"

_MAX_CONTENT_CHARS = 8000

# Entity types whose *content* can carry a Purview sensitivity label. A
# directory object cannot: a user, group or subscribedSku has no content to
# label, so screening one would withhold it for a label that can never exist.
# Types in this set but with no v1.0 read path (chatMessage, listItem) are
# undetermined, and so withheld when fail_closed is set -- that is a real gap,
# not an oversight. Calendar events are deliberately absent: Purview does not
# label calendar items, and Microsoft's own Copilot DLP location does not
# cover them either.
LABEL_BEARING_TYPES = frozenset(
    {"message", "driveitem", "listitem", "chatmessage", "post"}
)


@dataclass
class PurviewSettings:
    """Optional second stage: ask the tenant's own DLP engine.

    Inert unless an admin has authored a DLP policy targeting this application's
    location in Purview. With no policy configured the API returns no actions
    and this stage blocks nothing.
    """

    enabled: bool = False
    app_name: str = "graph-mcp"
    app_version: str = "0.1.0"
    application_location_id: str = ""
    max_content_checks: int = 25


@dataclass
class LabelPolicy:
    enabled: bool = False
    # "block" withholds; "annotate" reports what would have been withheld and
    # returns everything, which is how a deployment measures the blast radius
    # before turning enforcement on.
    mode: str = "block"
    fail_closed: bool = True
    blocked_labels: list[str] = field(default_factory=list)
    blocked_label_ids: list[str] = field(default_factory=list)
    require_extract_right: bool = True
    max_file_checks: int = 25
    purview: PurviewSettings = field(default_factory=PurviewSettings)

    @classmethod
    def load(cls, path: Path) -> "LabelPolicy":
        if not path.exists():
            return cls()
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        purview = raw.get("purview") or {}
        return cls(
            enabled=bool(raw.get("enabled", False)),
            mode=str(raw.get("mode", "block")).lower(),
            fail_closed=bool(raw.get("fail_closed", True)),
            blocked_labels=list(raw.get("blocked_labels") or []),
            blocked_label_ids=[
                str(i).lower() for i in (raw.get("blocked_label_ids") or [])
            ],
            require_extract_right=bool(raw.get("require_extract_right", True)),
            max_file_checks=int(raw.get("max_file_checks", 25)),
            purview=PurviewSettings(
                enabled=bool(purview.get("enabled", False)),
                app_name=str(purview.get("app_name", "graph-mcp")),
                app_version=str(purview.get("app_version", "0.1.0")),
                application_location_id=str(purview.get("application_location_id", "")),
                max_content_checks=int(purview.get("max_content_checks", 25)),
            ),
        )


@dataclass
class ScreenResult:
    body: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    withheld: int = 0


class LabelGate:
    """Decides what a caller may see, and performs the label reads to find out."""

    def __init__(self, policy: LabelPolicy):
        self.policy = policy
        # Label definitions are tenant metadata, not user data, so this cache is
        # deliberately caller-agnostic -- unlike anything derived from content,
        # which is never cached. See caller.py on why that distinction matters.
        self._definitions: dict[str, graph_labels.LabelDefinition] | None = None
        self._blocked_ids: set[str] | None = None
        self._purview_scope_checked = False
        self._purview_in_scope = True

    @classmethod
    def load(cls, path: Path) -> "LabelGate":
        return cls(LabelPolicy.load(path))

    @property
    def enabled(self) -> bool:
        return self.policy.enabled

    @property
    def enforcing(self) -> bool:
        return self.policy.enabled and self.policy.mode == "block"

    def describe(self) -> dict[str, Any]:
        """Policy state, for graph_whoami. No secrets, no tenant content."""
        return {
            "enabled": self.policy.enabled,
            "mode": self.policy.mode,
            "fail_closed": self.policy.fail_closed,
            "blocked_labels": list(self.policy.blocked_labels),
            "pinned_label_ids": len(self.policy.blocked_label_ids),
            "require_extract_right": self.policy.require_extract_right,
            "purview_process_content": self.policy.purview.enabled,
        }

    # ---- label resolution --------------------------------------------

    async def blocked_ids(self, transport) -> set[str]:
        """GUIDs of the labels this deployment blocks.

        Pinned ids are used as-is. Names are resolved against the tenant's label
        list, which is the only way a human-readable config entry can match the
        GUID that actually appears on content.
        """
        if self._blocked_ids is not None:
            return self._blocked_ids

        ids = set(self.policy.blocked_label_ids)
        if self.policy.blocked_labels:
            definitions = await self._load_definitions(transport)
            wanted = {n.strip().lower() for n in self.policy.blocked_labels}
            for definition in definitions.values():
                if definition.name.strip().lower() in wanted:
                    ids.add(definition.id.lower())
        self._blocked_ids = ids
        return ids

    async def _load_definitions(
        self, transport
    ) -> dict[str, graph_labels.LabelDefinition]:
        if self._definitions is None:
            found = await graph_labels.list_label_definitions(transport)
            self._definitions = {d.id.lower(): d for d in found}
        return self._definitions

    async def _label_name(self, transport, label_id: str) -> str:
        """Display name for a blocked label, falling back to its GUID.

        Cosmetic only -- the block decision is already made, so a failed lookup
        here must not turn into an error that loses it.
        """
        try:
            definitions = await self._load_definitions(transport)
        except graph_labels.LabelLookupError:
            return label_id
        definition = definitions.get(label_id.lower())
        return definition.name if definition and definition.name else label_id

    # ---- pre-flight ---------------------------------------------------

    async def mail_expand(self, transport, response_type: str) -> str | None:
        """`$expand` to add to a mail request so its labels come back with it.

        Costs no extra round trip, which is why mail is the one surface this
        gate screens cheaply.
        """
        if not self.enabled or not _is_message(response_type):
            return None
        try:
            ids = await self.blocked_ids(transport)
        except graph_labels.LabelLookupError:
            # Pre-flight only. Returning None here means the labels are not
            # expanded, so screen() finds them undetermined and fails closed
            # with a message -- better than turning a policy problem into an
            # opaque tool error before the request is even sent.
            return None
        return graph_labels.mail_label_expand(sorted(ids))

    # ---- screening ----------------------------------------------------

    async def screen(
        self,
        *,
        transport,
        request: GraphRequest,
        body: dict[str, Any],
        response_type: str = "",
    ) -> ScreenResult:
        if not self.enabled:
            return ScreenResult(body)

        # Writes echo back content the caller just supplied; screening it
        # protects nothing and would hand back a stub for their own draft.
        if request.method != "GET":
            return ScreenResult(body)

        kind = _classify(response_type, request.path)
        if kind not in LABEL_BEARING_TYPES:
            return ScreenResult(body)

        items, is_collection = _items_of(body)
        if not items:
            return ScreenResult(body)

        try:
            blocked = await self.blocked_ids(transport)
        except graph_labels.LabelLookupError as exc:
            # The gate could not learn what to block. Failing open here would
            # silently disable the whole control.
            return self._fail_whole_response(body, str(exc))

        notes: list[str] = []
        decisions: list[tuple[int, str]] = []  # (index, reason)

        readouts = await self._read_labels(
            transport, kind, items, request, notes
        )

        for index, (item, readout) in enumerate(zip(items, readouts)):
            reason = await self._verdict(transport, readout, blocked)
            if reason:
                decisions.append((index, reason))

        if self.policy.purview.enabled:
            survivors = [
                i for i in range(len(items)) if i not in {d[0] for d in decisions}
            ]
            decisions.extend(
                await self._purview_verdicts(transport, items, survivors)
            )

        return self._apply(body, items, is_collection, decisions, notes)

    async def _read_labels(
        self, transport, kind: str, items: list[dict], request: GraphRequest,
        notes: list[str],
    ) -> list[graph_labels.LabelReadout]:
        if kind == "message":
            return [graph_labels.mail_labels_from_item(i) for i in items]

        if kind == "driveitem":
            out: list[graph_labels.LabelReadout] = []
            budget = self.policy.max_file_checks
            for position, item in enumerate(items):
                if position >= budget:
                    out.append(
                        graph_labels.LabelReadout(
                            determined=False,
                            error=f"label-check budget of {budget} items exhausted",
                        )
                    )
                    continue
                item_path = _drive_item_path(request.path, item)
                if item_path is None:
                    out.append(
                        graph_labels.LabelReadout(
                            determined=False, error="could not locate the item's drive"
                        )
                    )
                    continue
                out.append(await graph_labels.extract_file_labels(transport, item_path))
            if len(items) > budget:
                notes.append(
                    f"Only the first {budget} items could be label-checked; "
                    "narrow the request with $top to check the rest."
                )
            return out

        # Anything else has no label read path in v1.0 -- Teams chat especially.
        return [
            graph_labels.LabelReadout(
                determined=False, error=f"no label read path for '{kind}' in Graph v1.0"
            )
            for _ in items
        ]

    async def _verdict(
        self, transport, readout: graph_labels.LabelReadout, blocked: set[str]
    ) -> str | None:
        """Why this item should be withheld, or None to allow it."""
        if not readout.determined:
            if not self.policy.fail_closed:
                return None
            return f"sensitivity label could not be determined ({readout.error})"

        hit = {i.lower() for i in readout.label_ids} & blocked
        if hit:
            name = await self._label_name(transport, sorted(hit)[0])
            return f"carries the blocked sensitivity label '{name}'"

        if self.policy.require_extract_right and readout.label_ids:
            return await self._rights_verdict(transport, readout.label_ids)
        return None

    async def _rights_verdict(self, transport, label_ids: set[str]) -> str | None:
        """Copilot's rule: no EXTRACT right means an AI app may not use it."""
        try:
            definitions = await self._load_definitions(transport)
        except graph_labels.LabelLookupError as exc:
            # Without definitions there is no way to tell which labels apply
            # encryption, so no way to know whose rights need checking.
            if self.policy.fail_closed:
                return f"usage rights could not be checked ({exc})"
            return None
        for label_id in sorted(label_ids):
            definition = definitions.get(label_id.lower())
            # Only encryption-backed labels carry usage rights at all.
            if definition is None or not definition.has_protection:
                continue
            email = await _caller_email(transport)
            if not email:
                if self.policy.fail_closed:
                    return "usage rights could not be checked (no caller identity)"
                continue
            rights, error = await graph_labels.compute_usage_rights(
                transport, label_id, email
            )
            if error:
                if self.policy.fail_closed:
                    return f"usage rights could not be determined ({error})"
                continue
            if not graph_labels.has_extract_right(rights):
                return (
                    f"the label '{definition.name or label_id}' applies encryption "
                    "and you do not hold the EXTRACT usage right"
                )
        return None

    # ---- Purview processContent ---------------------------------------

    async def _purview_verdicts(
        self, transport, items: list[dict], survivors: list[int],
    ) -> list[tuple[int, str]]:
        """Second stage: the tenant's DLP policy decides.

        One call per item, because processContentResponse reports policyActions
        for the request as a whole with no per-entry attribution -- batching
        would mean one match withholding everything.
        """
        if not await self._purview_applies(transport):
            return []

        out: list[tuple[int, str]] = []
        budget = self.policy.purview.max_content_checks
        for position, index in enumerate(survivors):
            if position >= budget:
                if self.policy.fail_closed:
                    out.append((index, f"DLP check budget of {budget} items exhausted"))
                continue
            text = _text_of(items[index])
            if not text:
                continue
            reason = await self._process_content(transport, items[index], text)
            if reason:
                out.append((index, reason))
        return out

    async def _purview_applies(self, transport) -> bool:
        """Skip the per-item calls entirely when no policy is in scope."""
        if self._purview_scope_checked:
            return self._purview_in_scope
        request = GraphRequest(
            "POST",
            "/me/dataSecurityAndGovernance/protectionScopes/compute",
            body={
                "activities": "downloadText",
                "locations": _app_location(self.policy.purview),
            },
        )
        response = await transport.send(request)
        self._purview_scope_checked = True
        if not response.ok:
            # Cannot tell whether policy applies; fail closed means keep asking.
            self._purview_in_scope = self.policy.fail_closed
            return self._purview_in_scope
        scopes = response.body.get("value") or response.body.get("scopes") or []
        self._purview_in_scope = bool(scopes)
        return self._purview_in_scope

    async def _process_content(self, transport, item: dict, text: str) -> str | None:
        settings = self.policy.purview
        request = GraphRequest(
            "POST",
            "/me/dataSecurityAndGovernance/processContent",
            body={
                "contentToProcess": {
                    "contentEntries": [
                        {
                            "@odata.type": "microsoft.graph.processContentMetadataBase",
                            "identifier": str(item.get("id", "")),
                            "name": str(item.get("subject") or item.get("name") or ""),
                            "content": {
                                "@odata.type": "microsoft.graph.textContent",
                                "data": text[:_MAX_CONTENT_CHARS],
                            },
                            "isTruncated": len(text) > _MAX_CONTENT_CHARS,
                        }
                    ],
                    "activityMetadata": {"activity": "downloadText"},
                    "integratedAppMetadata": {
                        "name": settings.app_name,
                        "version": settings.app_version,
                    },
                }
            },
        )
        response = await transport.send(request)
        if not response.ok:
            if self.policy.fail_closed:
                return f"DLP evaluation failed ({response.status})"
            return None
        for action in response.body.get("policyActions", []) or []:
            if _is_block_action(action):
                return "blocked by a Microsoft Purview DLP policy"
        return None

    # ---- applying the decision ----------------------------------------

    def _apply(
        self, body: dict, items: list[dict], is_collection: bool,
        decisions: list[tuple[int, str]], notes: list[str],
    ) -> ScreenResult:
        if not decisions:
            return ScreenResult(body, notes)

        reasons = {index: reason for index, reason in decisions}

        if self.policy.mode == "annotate":
            notes.append(
                f"{len(reasons)} of {len(items)} items would be withheld by the "
                "sensitivity-label policy (mode: annotate, nothing was removed): "
                + "; ".join(sorted({r for r in reasons.values()}))
            )
            return ScreenResult(body, notes, withheld=0)

        screened = [
            _stub(item, reasons[index]) if index in reasons else item
            for index, item in enumerate(items)
        ]
        out = dict(body)
        if is_collection:
            out["value"] = screened
        else:
            out = screened[0]

        notes.append(
            f"Withheld {len(reasons)} of {len(items)} items under the "
            "sensitivity-label policy: "
            + "; ".join(sorted({r for r in reasons.values()}))
        )
        return ScreenResult(out, notes, withheld=len(reasons))

    def _fail_whole_response(self, body: dict, error: str) -> ScreenResult:
        if not self.policy.fail_closed:
            return ScreenResult(
                body, [f"Sensitivity-label policy could not be applied ({error})."]
            )
        items, is_collection = _items_of(body)
        reason = f"sensitivity-label policy could not be applied ({error})"
        stubs = [_stub(i, reason) for i in items]
        out = dict(body)
        if is_collection:
            out["value"] = stubs
        elif stubs:
            out = stubs[0]
        return ScreenResult(
            out,
            [
                f"Withheld every item: {reason}. This deployment is configured "
                "fail-closed."
            ],
            withheld=len(stubs),
        )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _items_of(body: dict[str, Any]) -> tuple[list[dict], bool]:
    """The entities in a response, and whether it was a collection.

    Only reached for a label-bearing type, so any non-empty body is one entity.
    Keying off `id` here instead would let `$select=body` -- a projection with
    no id -- slip past the gate entirely.
    """
    value = body.get("value")
    if isinstance(value, list):
        return [i for i in value if isinstance(i, dict)], True
    substantive = {k: v for k, v in body.items() if not k.startswith("@odata.")}
    return ([body], False) if substantive else ([], False)


def _classify(response_type: str, path: str) -> str:
    """Entity kind, preferring the catalog's declared response type."""
    leaf = (response_type or "").rsplit(".", 1)[-1].lower()
    if leaf:
        return leaf
    lowered = path.lower()
    if "/messages" in lowered or "/mailfolders" in lowered:
        return "message"
    if "/drive/" in lowered or "/drives/" in lowered or lowered.endswith("/drive"):
        return "driveitem"
    return "unknown"


def _is_message(response_type: str) -> bool:
    return (response_type or "").rsplit(".", 1)[-1].lower() == "message"


def _drive_item_path(request_path: str, item: dict) -> str | None:
    """Graph path for one driveItem, for the extractSensitivityLabels POST."""
    item_id = item.get("id")
    if not item_id:
        return None
    parent = item.get("parentReference")
    if isinstance(parent, dict) and parent.get("driveId"):
        return f"/drives/{parent['driveId']}/items/{item_id}"

    # Otherwise derive the drive from the request path: everything up to and
    # including the '/drive' or '/drives/{id}' segment.
    segments = request_path.strip("/").split("/")
    for position, segment in enumerate(segments):
        if segment.lower() == "drive":
            base = "/" + "/".join(segments[: position + 1])
            return f"{base}/items/{item_id}"
        if segment.lower() == "drives" and position + 1 < len(segments):
            base = "/" + "/".join(segments[: position + 2])
            return f"{base}/items/{item_id}"
    return None


def _text_of(item: dict) -> str:
    parts = [str(item.get("subject") or item.get("name") or "")]
    body = item.get("body")
    if isinstance(body, dict) and body.get("content"):
        parts.append(str(body["content"]))
    return "\n".join(p for p in parts if p).strip()


def _app_location(settings: PurviewSettings) -> list[dict]:
    if not settings.application_location_id:
        return []
    return [
        {
            "@odata.type": "microsoft.graph.policyLocationApplication",
            "value": settings.application_location_id,
        }
    ]


def _is_block_action(action: Any) -> bool:
    if not isinstance(action, dict):
        return False
    name = str(action.get("action", "")).lower()
    if name in ("blockaccess", "restrictaccess"):
        return True
    return str(action.get("restrictionAction", "")).lower() == "block"


def _stub(item: dict, reason: str) -> dict:
    """What replaces a withheld item.

    Keeps the id so the caller can tell that something was removed rather than
    silently receiving a shorter list -- a missing item the model cannot see is
    indistinguishable from one that never existed.
    """
    out: dict[str, Any] = {WITHHELD_KEY: True, REASON_KEY: reason}
    if item.get("id"):
        out["id"] = item["id"]
    return out


async def _caller_email(transport) -> str:
    try:
        identity = await transport.identity()
    except Exception:  # pragma: no cover - transport-specific failures
        return ""
    return str(identity.get("user_principal_name") or identity.get("mail") or "")
