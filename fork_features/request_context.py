"""Fork policy for volatile context attached to one provider request.

The durable transcript remains authoritative user history. Recall, plugin context,
and Gateway one-turn notes are composed only onto request copies; historical
``api_content`` values are compatibility metadata and are never replayed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent.memory_manager import build_memory_context_block


def compose_user_api_content(
    content: Any,
    ext_prefetch_cache: str,
    plugin_user_context: str,
) -> Optional[Any]:
    """Compose a request-only user suffix without mutating durable content."""
    injections = []
    if ext_prefetch_cache:
        fenced = build_memory_context_block(ext_prefetch_cache)
        if fenced:
            injections.append(fenced)
    if plugin_user_context:
        injections.append(plugin_user_context)
    if not injections:
        return None
    suffix = "\n\n".join(injections)
    if isinstance(content, str):
        return content + "\n\n" + suffix
    if isinstance(content, list):
        return [*content, {"type": "text", "text": "\n\n" + suffix}]
    return None


def uses_openai_memory_developer_after_user(agent: Any) -> bool:
    """Whether this runtime accepts recall as a Responses developer item."""
    if getattr(agent, "api_mode", None) != "codex_responses":
        return False
    provider = str(getattr(agent, "provider", "") or "").strip().lower()
    host = str(getattr(agent, "_base_url_hostname", "") or "").strip().lower()
    base = str(getattr(agent, "_base_url_lower", "") or "").strip().lower()
    return (
        provider in {"openai", "openai-codex"}
        or host == "api.openai.com"
        or (host == "chatgpt.com" and "/backend-api/codex" in base)
    )


def apply_request_only_turn_context(
    agent: Any,
    api_messages: List[Dict[str, Any]],
    *,
    current_turn_user_idx: Optional[int],
    ext_prefetch_cache: str,
    plugin_user_context: str,
    plugin_request_context: str = "",
    force_memory_user_suffix: bool = False,
) -> int:
    """Attach this turn's volatile context to an API-only message copy.

    Ordinary plugin/Gateway context stays on the current user item. Explicit
    request context and recall use the OpenAI Responses developer-item shape
    when supported, except for provider-neutral MoA views where a user suffix is
    required. Returns the resolved current-user index, or ``-1`` when absent.
    """
    resolved_idx = (
        current_turn_user_idx if isinstance(current_turn_user_idx, int) else -1
    )
    if not (
        0 <= resolved_idx < len(api_messages)
        and isinstance(api_messages[resolved_idx], dict)
        and api_messages[resolved_idx].get("role") == "user"
    ):
        resolved_idx = next(
            (
                idx
                for idx, message in enumerate(api_messages)
                if isinstance(message, dict) and message.get("_current_turn_user")
            ),
            -1,
        )
    if resolved_idx < 0:
        resolved_idx = next(
            (
                idx
                for idx in range(len(api_messages) - 1, -1, -1)
                if isinstance(api_messages[idx], dict)
                and api_messages[idx].get("role") == "user"
            ),
            -1,
        )
    if resolved_idx < 0:
        return -1

    current_user = api_messages[resolved_idx]
    plugin_composed = compose_user_api_content(
        current_user.get("content", ""),
        "",
        plugin_user_context,
    )
    if plugin_composed is not None:
        current_user["content"] = plugin_composed

    memory_block = build_memory_context_block(ext_prefetch_cache)
    request_parts = [
        part for part in (plugin_request_context, memory_block) if part
    ]
    if (
        request_parts
        and uses_openai_memory_developer_after_user(agent)
        and not force_memory_user_suffix
    ):
        api_messages.insert(
            resolved_idx + 1,
            {"role": "developer", "content": "\n\n".join(request_parts)},
        )
        return resolved_idx

    request_composed = compose_user_api_content(
        current_user.get("content", ""),
        "",
        str(plugin_request_context or ""),
    )
    if request_composed is not None:
        current_user["content"] = request_composed

    memory_composed = compose_user_api_content(
        current_user.get("content", ""),
        ext_prefetch_cache,
        "",
    )
    if memory_composed is not None:
        current_user["content"] = memory_composed
    return resolved_idx


def strip_legacy_api_content(api_msg: Dict[str, Any]) -> None:
    """Remove an old persisted sidecar without replacing clean history."""
    api_msg.pop("api_content", None)
    api_msg.pop("_request_only_api_content", None)


def consume_request_only_api_content(api_msg: Dict[str, Any]) -> None:
    """Project an explicitly marked active-turn sidecar, dropping every legacy sidecar."""
    sidecar = api_msg.pop("api_content", None)
    request_only = api_msg.pop("_request_only_api_content", False) is True
    if (
        request_only
        and isinstance(sidecar, str)
        and bool(sidecar)
        and api_msg.get("role") in ("user", "assistant")
    ):
        api_msg["content"] = sidecar
