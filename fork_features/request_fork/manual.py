"""Ephemeral last-wire checkpoint source; never persisted or replayed by normal turns."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from . import FrozenRequest

# Compare only durable message fields; live-only native carriers are already
# preserved inside the frozen request and are not restored by SessionDB.
_IDENTITY_FIELDS = (
    "role", "content", "tool_call_id", "tool_calls", "reasoning_content",
    "reasoning_details", "codex_reasoning_items", "codex_message_items",
)


def _fingerprint(messages: list) -> str:
    from hermes_state_messages import SessionMessagesMixin

    rows = [{key: row[key] for key in _IDENTITY_FIELDS if row.get(key) is not None}
            for row in messages]
    for row in rows:
        if "content" in row:
            # Match exactly the database loader's projection, without changing
            # either the live history or the already-frozen native request.
            row["content"] = SessionMessagesMixin._loaded_view_content(row.get("role", ""), row["content"])
    raw = json.dumps(rows, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _route(agent: Any) -> tuple[str, ...]:
    return tuple(str(getattr(agent, key, "") or "")
                 for key in ("session_id", "api_mode", "provider", "base_url", "model"))


@dataclass(frozen=True)
class ManualRequest:
    request: FrozenRequest
    route: tuple[str, ...]
    message_count: int
    prefix_fingerprint: str


def remember_manual_request(agent: Any, request: FrozenRequest | None, messages: list) -> None:
    """Retain the already-frozen value once, replacing any older source on this agent."""
    agent._continuity_manual_request = None
    if not isinstance(request, FrozenRequest) or not messages:
        return
    try:
        agent._continuity_manual_request = ManualRequest(
            request, _route(agent), len(messages), _fingerprint(messages),
        )
    except (TypeError, ValueError, KeyError):
        # Optional manual reuse must never invalidate the existing overflow capture.
        return


def reuse_manual_request(source: Any, target: Any, messages: list) -> FrozenRequest | None:
    """Reuse only the same route's unchanged priced history plus its assistant reply.

    Any new user/tool row, rewind, compression, route/session switch or missing
    source rejects reuse. Reconstructing an ordinary turn never reads this value.
    """
    saved = getattr(source, "_continuity_manual_request", None)
    if not isinstance(saved, ManualRequest):
        return None
    if saved.route != _route(source) or saved.route != _route(target):
        return None
    request = saved.request
    if request.captured_session_id != saved.route[0] or request.api_mode != saved.route[1]:
        return None
    count = saved.message_count
    if len(messages) < count or _fingerprint(messages[:count]) != saved.prefix_fingerprint:
        return None
    tail = messages[count:]
    if any(row.get("role") != "assistant" or not isinstance(row.get("content"), str)
           for row in tail):
        return None
    body = request.clone_body()
    if tail:
        # Convert ONLY the newly appended reply; never rebuild the priced prefix,
        # rerun middleware, or consume a one-shot setting on the parent agent.
        transport = copy.deepcopy(source._get_transport())
        rows = [{key: copy.deepcopy(row[key])
                 for key in (*_IDENTITY_FIELDS, "anthropic_content_blocks") if key in row}
                for row in tail]
        for row in rows:
            row["content"] = row["content"].strip()
        from agent.message_sanitization import _sanitize_messages_surrogates
        from utils import base_url_host_matches

        _sanitize_messages_surrogates(rows)
        if request.api_mode == "anthropic_messages":
            # Its full-history converter requires an initial user item. This
            # local anchor prevents a synthetic '(empty)' user from leaking
            # between the preserved native prefix and the newly appended reply.
            rows.insert(0, {"role": "user", "content": "checkpoint-tail-conversion-anchor"})
        converted = transport.convert_messages(
            rows, model=str(body.get("model") or ""), base_url=saved.route[3],
            is_codex_backend=source._is_codex_backend(),
            is_github_responses=source._is_copilot_url(),
            is_xai_responses=(saved.route[2] in {"xai", "xai-oauth"}
                              or base_url_host_matches(saved.route[3], "api.x.ai")),
            replay_encrypted_reasoning=bool(getattr(source, "_codex_reasoning_replay_enabled", True)),
        )
        if request.api_mode == "anthropic_messages":
            converted = converted[1][1:]  # discard only the local conversion anchor
        body[request.input_key].extend(converted)
    return FrozenRequest(
        body=body, fidelity="reused_out_of_turn", api_mode=request.api_mode,
        captured_session_id=request.captured_session_id,
    )
