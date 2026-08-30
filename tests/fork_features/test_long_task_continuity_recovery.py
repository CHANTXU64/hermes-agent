"""Core decoupling tests for continuity recovery delivery."""

from __future__ import annotations

from types import SimpleNamespace

from fork_features.request_context import apply_request_only_turn_context


def _codex_agent(**extra):
    return SimpleNamespace(
        api_mode="codex_responses",
        provider="openai-codex",
        _base_url_hostname="chatgpt.com",
        _base_url_lower="https://chatgpt.com/backend-api/codex",
        **extra,
    )


def test_core_ignores_removed_continuity_agent_side_channel():
    agent = _codex_agent(
        _post_compression_continuity_context="MUST NOT BE INJECTED BY CORE"
    )
    api_messages = [{"role": "user", "content": "REAL USER WORDS"}]

    apply_request_only_turn_context(
        agent,
        api_messages,
        current_turn_user_idx=0,
        ext_prefetch_cache="",
        plugin_user_context="",
        plugin_request_context="",
    )

    assert api_messages == [{"role": "user", "content": "REAL USER WORDS"}]


def test_existing_plugin_and_memory_request_context_semantics_are_unchanged():
    agent = _codex_agent()
    api_messages = [{"role": "user", "content": "REAL USER WORDS"}]

    apply_request_only_turn_context(
        agent,
        api_messages,
        current_turn_user_idx=0,
        ext_prefetch_cache="MEMORY CONTEXT",
        plugin_user_context="",
        plugin_request_context="OTHER PLUGIN CONTEXT",
    )

    assert api_messages[0] == {"role": "user", "content": "REAL USER WORDS"}
    assert api_messages[1]["role"] == "developer"
    assert "OTHER PLUGIN CONTEXT" in api_messages[1]["content"]
    assert "<memory-context>" in api_messages[1]["content"]
    assert "MEMORY CONTEXT" in api_messages[1]["content"]
