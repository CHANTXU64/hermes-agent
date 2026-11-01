"""Continuity uses official hook/user context, never a hidden agent-side channel."""
from __future__ import annotations

from types import SimpleNamespace

from agent.turn_context import build_api_messages


def _codex_agent(**extra):
    return SimpleNamespace(
        api_mode="codex_responses", provider="openai-codex",
        _base_url_hostname="chatgpt.com", _base_url_lower="https://chatgpt.com/backend-api/codex",
        _current_turn_timestamp=1, ephemeral_system_prompt="",
        _copy_reasoning_content_for_api=lambda *args: None,
        _should_sanitize_tool_calls=lambda: False,
        **extra,
    )


def _build(agent, *, memory="", plugin=""):
    source = [{"role": "user", "content": "REAL USER WORDS"}]
    result, _ = build_api_messages(
        agent, source, current_turn_user_idx=0,
        ext_prefetch_cache=memory, plugin_user_context=plugin,
        moa_config=None, active_system_prompt="",
    )
    assert source == [{"role": "user", "content": "REAL USER WORDS"}]
    return result


def test_core_ignores_removed_continuity_agent_side_channel():
    agent = _codex_agent(_post_compression_continuity_context="MUST NOT BE INJECTED BY CORE")
    assert _build(agent) == [{"role": "user", "content": "REAL USER WORDS"}]


def test_plugin_and_memory_context_use_official_user_suffix():
    result = _build(_codex_agent(), memory="MEMORY CONTEXT", plugin="OTHER PLUGIN CONTEXT")
    assert len(result) == 1
    assert result[0]["role"] == "user"
    text = result[0]["content"]
    assert text.startswith("REAL USER WORDS\n\n<memory-context>")
    assert "MEMORY CONTEXT" in text
    assert text.endswith("OTHER PLUGIN CONTEXT")
