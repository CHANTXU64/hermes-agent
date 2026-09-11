"""Prepared request adoption policy shared by proactive and failed-wire compression.

Only the host's tool-call canonicalizer is supplied as a capability. Provider
conversion uses its defining modules, never the conversation-loop facade.
"""
from __future__ import annotations
import copy
from typing import Any, Callable, Dict, List, Optional

from agent.message_sanitization import _sanitize_messages_surrogates
from utils import base_url_host_matches
from fork_features.request_fork import rematerialize_codex_request_after_adopt


def build_adopt_rematerializer(
    agent: Any,
    *,
    prepared_request: Any,
    original_messages: List[Dict[str, Any]],
    current_turn_user_idx: int,
    canonicalize_tool_calls: Callable[[list], None],
) -> Optional[Callable[[list], Any]]:
    """Capture a pure durable-parent adoption factory from one prepared request."""
    if prepared_request is None or not (
        0 <= current_turn_user_idx < len(original_messages)
    ):
        return None
    prepared_body = prepared_request.clone_body()
    prepared_input = prepared_body.get("input")
    if not isinstance(prepared_input, list):
        return None
    current_user_item = next(
        (
            copy.deepcopy(item)
            for item in reversed(prepared_input)
            if isinstance(item, dict) and item.get("role") == "user"
        ),
        None,
    )
    if current_user_item is None:
        return None

    provider = str(getattr(agent, "provider", "") or "")
    base_url = str(getattr(agent, "base_url", "") or "")
    is_github_responses = (
        base_url_host_matches(base_url, "models.github.ai")
        or base_url_host_matches(base_url, "githubcopilot.com")
    )
    is_codex_backend = bool(agent._is_codex_backend())
    is_xai_responses = (
        provider in {"xai", "xai-oauth"}
        or base_url_host_matches(base_url, "api.x.ai")
    )
    replay_encrypted_reasoning = bool(
        getattr(agent, "_codex_reasoning_replay_enabled", True)
    )
    needs_reasoning_pad = bool(agent._needs_thinking_reasoning_pad())
    original_snapshot = copy.deepcopy(list(original_messages))

    from agent.codex_responses_adapter import (
        _chat_messages_to_responses_input,
        _classify_responses_issuer,
    )

    issuer = _classify_responses_issuer(
        is_xai_responses=is_xai_responses,
        is_github_responses=is_github_responses,
        is_codex_backend=is_codex_backend,
        base_url=base_url,
    )

    def _convert(messages: List[Dict[str, Any]]) -> list[dict[str, Any]]:
        from agent.message_sanitization import apply_reasoning_content_policy

        prepared: List[Dict[str, Any]] = []
        for source in messages:
            api_message: Dict[str, Any] = copy.deepcopy(source)
            apply_reasoning_content_policy(
                source,
                api_message,
                needs_thinking_pad=needs_reasoning_pad,
            )
            api_message.pop("reasoning", None)
            content = api_message.get("content")
            if isinstance(content, str):
                api_message["content"] = content.strip()
            prepared.append(api_message)
        canonicalize_tool_calls(prepared)
        _sanitize_messages_surrogates(prepared)
        return _chat_messages_to_responses_input(
            prepared,
            is_xai_responses=is_xai_responses,
            is_github_responses=is_github_responses,
            replay_encrypted_reasoning=replay_encrypted_reasoning,
            current_issuer_kind=issuer,
        )

    current_user_input = [current_user_item]

    def _rematerialize(adopted_messages: list) -> Any:
        return rematerialize_codex_request_after_adopt(
            prepared_request,
            original_messages=original_snapshot,
            adopted_messages=adopted_messages,
            live_tail_start=current_turn_user_idx,
            current_user_input=current_user_input,
            convert_added_messages=_convert,
        )

    return _rematerialize
