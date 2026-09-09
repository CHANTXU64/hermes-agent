"""Fork boundaries for request-local context and Codex cache routing."""

from __future__ import annotations

import copy
from types import SimpleNamespace

from fork_features.prompt_cache_routing import (
    apply_codex_backend_cache_routing,
    resolve_codex_prompt_cache_scope,
)
from fork_features.request_context import (
    apply_request_only_turn_context,
    compose_user_api_content,
    strip_legacy_api_content,
)


def _codex_agent(**extra):
    values = {
        "api_mode": "codex_responses",
        "provider": "openai-codex",
        "_base_url_hostname": "chatgpt.com",
        "_base_url_lower": "https://chatgpt.com/backend-api/codex",
    }
    values.update(extra)
    return SimpleNamespace(**values)


def test_core_hosts_use_fork_policy_without_turn_context_reexports():
    import inspect

    from agent import turn_context
    from agent.transports import codex

    assert "apply_request_only_turn_context" in inspect.getsource(
        turn_context.build_api_messages
    )
    assert "apply_codex_backend_cache_routing" in inspect.getsource(
        codex.ResponsesApiTransport.build_kwargs
    )
    assert "apply_request_only_turn_context" not in vars(turn_context)


def test_codex_request_context_is_after_clean_user_and_does_not_mutate_history():
    durable = [
        {"role": "assistant", "content": "previous answer"},
        {"role": "user", "content": "current question"},
    ]
    api_messages = copy.deepcopy(durable)

    resolved = apply_request_only_turn_context(
        _codex_agent(),
        api_messages,
        current_turn_user_idx=1,
        ext_prefetch_cache="REMEMBERED FACT",
        plugin_user_context="ORDINARY PLUGIN",
        plugin_request_context="REQUEST POLICY",
    )

    assert resolved == 1
    assert durable == [
        {"role": "assistant", "content": "previous answer"},
        {"role": "user", "content": "current question"},
    ]
    assert api_messages[1] == {
        "role": "user",
        "content": "current question\n\nORDINARY PLUGIN",
    }
    assert api_messages[2]["role"] == "developer"
    assert api_messages[2]["content"].startswith("REQUEST POLICY\n\n<memory-context>")
    assert "REMEMBERED FACT" in api_messages[2]["content"]


def test_non_codex_request_context_uses_only_current_user_copy():
    durable = [{"role": "user", "content": "current question"}]
    api_messages = copy.deepcopy(durable)

    apply_request_only_turn_context(
        SimpleNamespace(api_mode="chat_completions", provider="custom"),
        api_messages,
        current_turn_user_idx=0,
        ext_prefetch_cache="REMEMBERED FACT",
        plugin_user_context="ORDINARY PLUGIN",
        plugin_request_context="REQUEST POLICY",
    )

    assert durable == [{"role": "user", "content": "current question"}]
    assert len(api_messages) == 1
    assert api_messages[0]["role"] == "user"
    assert api_messages[0]["content"].startswith(
        "current question\n\nORDINARY PLUGIN\n\nREQUEST POLICY\n\n<memory-context>"
    )
    assert "REMEMBERED FACT" in api_messages[0]["content"]


def test_legacy_api_content_is_removed_without_replacing_clean_content():
    api_message = {
        "role": "user",
        "content": "clean user text",
        "api_content": "clean user text\n\nSTALE RECALL",
    }

    strip_legacy_api_content(api_message)

    assert api_message == {"role": "user", "content": "clean user text"}


def test_cache_scope_preserves_gateway_and_compression_boundaries():
    gateway = SimpleNamespace(
        _gateway_session_key="agent:main:telegram:dm:42",
        _session_db=None,
    )
    assert (
        resolve_codex_prompt_cache_scope(gateway, "physical-after-compression")
        == "agent:main:telegram:dm:42"
    )

    compressed = SimpleNamespace(
        _gateway_session_key="",
        _session_db=SimpleNamespace(
            get_compression_lineage=lambda _sid: ["root", "child", "tip"]
        ),
    )
    assert resolve_codex_prompt_cache_scope(compressed, "tip") == "root"

    branch = SimpleNamespace(
        _gateway_session_key="",
        _session_db=SimpleNamespace(
            get_compression_lineage=lambda sid: [sid]
        ),
    )
    assert resolve_codex_prompt_cache_scope(branch, "branch-session") is None


def test_codex_backend_header_policy_keeps_body_and_headers_on_one_key():
    original = {
        "prompt_cache_key": "generated-key",
        "extra_body": {
            "prompt_cache_key": "extra-body-key",
            "other": "kept",
        },
        "extra_headers": {
            "session-id": "obsolete",
            "x-custom": "kept",
        },
    }

    routed = apply_codex_backend_cache_routing(
        original,
        session_id="physical-session",
        request_overrides={"prompt_cache_key": "explicit-key"},
        fallback_cache_key="fallback-key",
        bound_key=lambda value: value if isinstance(value, str) and value else None,
    )

    assert original["extra_body"]["prompt_cache_key"] == "extra-body-key"
    assert routed["prompt_cache_key"] == "explicit-key"
    assert routed["extra_body"] == {"other": "kept"}
    assert routed["extra_headers"] == {
        "x-custom": "kept",
        "session_id": "physical-session",
        "thread-id": "explicit-key",
        "x-client-request-id": "explicit-key",
    }


def test_invalid_top_level_override_cannot_shadow_valid_extra_body_key():
    routed = apply_codex_backend_cache_routing(
        {
            "prompt_cache_key": 123,
            "extra_body": {"prompt_cache_key": "valid-extra-key"},
        },
        session_id="physical-session",
        request_overrides={
            "prompt_cache_key": 123,
            "extra_body": {"prompt_cache_key": "valid-extra-key"},
        },
        fallback_cache_key="fallback-key",
        bound_key=lambda value: value if isinstance(value, str) and value else None,
    )

    assert routed["prompt_cache_key"] == "valid-extra-key"
    assert "extra_body" not in routed
    assert routed["extra_headers"]["thread-id"] == "valid-extra-key"
