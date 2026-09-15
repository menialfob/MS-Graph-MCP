"""Turn Graph errors into something a model can act on.

Graph's error bodies are accurate but rarely actionable. A 403 says
"Insufficient privileges to complete the operation" without naming the scope
that was missing; a 400 on a bad $filter does not mention that the property is
not filterable. A model handed those retries the identical request.

Each translation states what failed, why, and the next concrete step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TranslatedError:
    status: int
    code: str
    message: str
    guidance: str
    retryable: bool = False
    retry_after: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = {
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "guidance": self.guidance,
            "retryable": self.retryable,
        }
        if self.retry_after is not None:
            out["retry_after_seconds"] = self.retry_after
        if self.details:
            out["details"] = self.details
        return out


def _graph_error(body: dict[str, Any]) -> tuple[str, str]:
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("code", "")), str(error.get("message", ""))
    return "", ""


def translate(
    status: int,
    body: dict[str, Any],
    headers: dict[str, str] | None = None,
    *,
    required_scopes: list[str] | None = None,
    granted_scopes: list[str] | None = None,
) -> TranslatedError:
    headers = headers or {}
    code, message = _graph_error(body)

    if status == 401:
        return TranslatedError(
            status, code or "unauthenticated", message,
            "The token was rejected. It may have expired, or the request needs a "
            "claims challenge (conditional access). Re-authenticate; do not retry "
            "this request unchanged.",
        )

    if status == 403:
        missing = [s for s in (required_scopes or []) if s not in set(granted_scopes or [])]
        if missing:
            guidance = (
                f"Missing delegated scope(s): {', '.join(missing)}. "
                f"The token grants: {', '.join(granted_scopes or []) or 'none'}. "
                "This needs consent for the additional scope -- it cannot be retried."
            )
        else:
            guidance = (
                "The token carries the required scope, so this is the signed-in "
                "user's own privileges or a tenant policy denying access. Acting "
                "on another user's data usually needs an admin role."
            )
        return TranslatedError(status, code or "accessDenied", message, guidance,
                               details={"required_scopes": required_scopes or []})

    if status == 404:
        return TranslatedError(
            status, code or "itemNotFound", message,
            "The path is valid but the item does not exist, or is not visible to "
            "this user. Check the id; list the parent collection to find a real one.",
        )

    if status == 429:
        retry_after = float(headers.get("Retry-After", 30))
        return TranslatedError(
            status, code or "tooManyRequests", message,
            f"Throttled by Graph. Wait {retry_after:.0f}s before retrying. Reduce "
            "page size or the number of concurrent calls.",
            retryable=True, retry_after=retry_after,
        )

    if status == 400:
        lowered = message.lower()
        if "consistencylevel" in lowered or "advanced query" in lowered:
            guidance = (
                "This directory query needs 'ConsistencyLevel: eventual' and "
                "'$count=true'. The server normally adds these automatically; "
                "if you set headers manually, keep both."
            )
        elif "$filter" in lowered or "filter" in lowered:
            guidance = (
                "The $filter is not valid for this resource. Not every property "
                "is filterable -- call graph_describe_type to see which are, and "
                "note that string comparisons are case-sensitive."
            )
        elif "$select" in lowered or "$expand" in lowered:
            guidance = (
                "A requested property does not exist on this type. Call "
                "graph_describe_type for the real property names."
            )
        else:
            guidance = (
                "Graph rejected the request shape. Call graph_describe_operation "
                "for the expected parameters and body."
            )
        return TranslatedError(status, code or "badRequest", message, guidance)

    if status in (502, 503, 504):
        return TranslatedError(
            status, code or "serviceUnavailable", message,
            "Transient Graph failure. Retry with backoff; if it persists the "
            "workload may be degraded.",
            retryable=True, retry_after=float(headers.get("Retry-After", 5)),
        )

    if status == 500:
        return TranslatedError(status, code or "internalServerError", message,
                               "Graph failed internally. Retry once with backoff.",
                               retryable=True)

    return TranslatedError(
        status, code or f"http{status}", message or "Unexpected response.",
        "Unrecognised Graph response. The raw error code and message are above.",
    )
