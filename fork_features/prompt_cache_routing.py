"""Fork policy for Codex logical cache scope and backend routing headers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


def resolve_codex_prompt_cache_scope(agent: Any, session_id: str | None) -> str | None:
    """Return the existing Fork logical scope without merging branch/delegate roots.

    Gateway routing keys survive physical session rotation. Non-gateway
    compression children reuse only the compression-lineage root. Known ordinary
    sessions return ``None`` so the transport retains its content-addressed key;
    unavailable lineage lookup falls back to the physical session id.
    """
    gateway_key = str(
        getattr(agent, "_gateway_session_key", "") or ""
    ).strip()
    if gateway_key:
        return gateway_key

    original = str(session_id or "").strip()
    if not original:
        return None

    db = getattr(agent, "_session_db", None)
    if db is None or not hasattr(db, "get_compression_lineage"):
        return original

    try:
        lineage = db.get_compression_lineage(original)
    except Exception:
        return original

    if (
        isinstance(lineage, list)
        and len(lineage) > 1
        and original in lineage
        and isinstance(lineage[0], str)
        and lineage[0]
    ):
        return lineage[0]
    return None


def _first_bounded_key(
    bound_key: Callable[[Any], str | None],
    *values: Any,
) -> str | None:
    for value in values:
        key = bound_key(value)
        if key:
            return key
    return None


def apply_codex_backend_cache_routing(
    kwargs: Mapping[str, Any],
    *,
    session_id: str,
    request_overrides: Mapping[str, Any] | None,
    fallback_cache_key: Any,
    bound_key: Callable[[Any], str | None],
) -> dict[str, Any]:
    """Return Codex kwargs whose body and HTTP headers share one bounded key.

    Explicit top-level overrides win over their ``extra_body`` spelling. Invalid
    values cannot shadow a later valid key. The duplicate body field is removed
    before SDK merging, and the obsolete ``session-id`` header is never emitted.
    """
    routed = dict(kwargs)
    existing_extra_body = routed.get("extra_body")
    extra_body_cache_key = None
    if isinstance(existing_extra_body, dict):
        copied_extra_body = dict(existing_extra_body)
        extra_body_cache_key = copied_extra_body.pop("prompt_cache_key", None)
        if copied_extra_body:
            routed["extra_body"] = copied_extra_body
        else:
            routed.pop("extra_body", None)

    override_cache_key = None
    override_extra_body_cache_key = None
    if isinstance(request_overrides, Mapping):
        override_cache_key = request_overrides.get("prompt_cache_key")
        override_extra_body = request_overrides.get("extra_body")
        if isinstance(override_extra_body, Mapping):
            override_extra_body_cache_key = override_extra_body.get(
                "prompt_cache_key"
            )

    final_cache_key = _first_bounded_key(
        bound_key,
        override_cache_key,
        override_extra_body_cache_key,
        extra_body_cache_key,
        routed.get("prompt_cache_key"),
        fallback_cache_key,
    )
    if final_cache_key:
        routed["prompt_cache_key"] = final_cache_key
    else:
        routed.pop("prompt_cache_key", None)

    existing_extra_headers = routed.get("extra_headers")
    merged_extra_headers: dict[str, str] = {}
    if isinstance(existing_extra_headers, dict):
        merged_extra_headers.update(
            {
                str(key): str(value)
                for key, value in existing_extra_headers.items()
                if key and value is not None
            }
        )
    merged_extra_headers.pop("session-id", None)
    if session_id:
        merged_extra_headers["session_id"] = session_id
    if final_cache_key:
        merged_extra_headers["thread-id"] = final_cache_key
        merged_extra_headers["x-client-request-id"] = final_cache_key
    if merged_extra_headers:
        routed["extra_headers"] = merged_extra_headers
    else:
        routed.pop("extra_headers", None)
    return routed
