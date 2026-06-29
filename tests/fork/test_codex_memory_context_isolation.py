"""Fork-owned memory-context isolation regressions for Codex paths."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from agent.memory_manager import build_memory_context_block, sanitize_context
from tests.run_agent.test_run_agent import _mock_response, agent as agent  # re-export pytest fixture
from tests.run_agent.test_run_agent_codex_responses import _build_agent


def test_memory_context_block_uses_non_authoritative_system_note():
    block = build_memory_context_block("user likes dark mode")
    assert (
        "[System note: The following is recalled memory context, NOT new user input. "
        "This is the agent's persistent memory from prior sessions, for reference only.]"
    ) in block
    assert "Treat as authoritative reference data" not in block
    assert "should inform all responses" not in block
    assert "user likes dark mode" in block


def test_sanitize_context_strips_current_system_note():
    wrapped_note = (
        "[System note: The following is recalled memory context, NOT new user input. "
        "This is the agent's persistent memory from prior sessions, for reference only.]\n\n"
        "real fact"
    )
    result = sanitize_context(wrapped_note)
    assert "System note" not in result
    assert "real fact" in result


def test_auto_memory_recall_uses_codex_tail_developer_not_instructions_or_user_suffix(agent):
    class _MemoryManager:
        def on_turn_start(self, *args, **kwargs):
            pass

        def prefetch_all(self, query, *, session_id=""):
            return "# Hindsight Memory\n\n- remembered fact"

        def sync_all(self, *args, **kwargs):
            pass

        def queue_prefetch_all(self, *args, **kwargs):
            pass

    captured = {}

    def _fake_api_call(api_kwargs):
        captured.update(api_kwargs)
        return SimpleNamespace(
            status="completed",
            output=[
                SimpleNamespace(
                    type="message",
                    status="completed",
                    content=[SimpleNamespace(type="output_text", text="ok")],
                )
            ],
        )

    agent.api_mode = "codex_responses"
    agent.provider = "openai-codex"
    agent.base_url = "https://chatgpt.com/backend-api/codex"
    agent._base_url_lower = agent.base_url.lower()
    agent._base_url_hostname = "chatgpt.com"
    agent.model = "gpt-5.5"
    agent._memory_manager = _MemoryManager()
    agent._interruptible_api_call = _fake_api_call
    agent._persist_session = lambda *args, **kwargs: None
    agent._save_trajectory = lambda *args, **kwargs: None

    result = agent.run_conversation("hello")

    assert result["completed"] is True
    assert "remembered fact" not in captured["instructions"]
    assert "<memory-context>" not in captured["instructions"]
    input_items = captured["input"]
    user_item = next(m for m in input_items if m.get("role") == "user")
    assert user_item["content"] == "hello"
    assert "remembered fact" not in user_item["content"]
    developer_item = input_items[-1]
    assert developer_item["role"] == "developer"
    assert developer_item["content"].startswith("<memory-context>")
    assert "remembered fact" in developer_item["content"]
    assert not any(
        item.get("type") == "function_call"
        and item.get("name") == "hindsight_recall"
        for item in input_items
    )
    assert result["messages"][0]["content"] == "hello"
    assert "<memory-context>" not in json.dumps(result["messages"])


def test_auto_memory_recall_falls_back_to_user_suffix_for_chat_completions(agent):
    class _MemoryManager:
        def on_turn_start(self, *args, **kwargs):
            pass

        def prefetch_all(self, query, *, session_id=""):
            return "# Hindsight Memory\n\n- remembered fact"

        def sync_all(self, *args, **kwargs):
            pass

        def queue_prefetch_all(self, *args, **kwargs):
            pass

    captured = {}
    hook_calls = []
    estimate_request_inputs = []

    def _fake_api_call(api_kwargs):
        captured["messages"] = api_kwargs["messages"]
        return _mock_response(content="ok")

    def _record_hook(name, **kwargs):
        hook_calls.append((name, kwargs))
        return []

    def _estimate_request_tokens(messages, **kwargs):
        estimate_request_inputs.append(messages)
        return 1

    agent._memory_manager = _MemoryManager()
    agent._interruptible_api_call = _fake_api_call
    agent._persist_session = lambda *args, **kwargs: None
    agent._save_trajectory = lambda *args, **kwargs: None

    with patch(
        "hermes_cli.plugins.has_hook",
        side_effect=lambda name: name == "pre_api_request",
    ), patch(
        "hermes_cli.plugins.invoke_hook",
        side_effect=_record_hook,
    ), patch(
        "agent.conversation_loop.estimate_request_tokens_rough",
        side_effect=_estimate_request_tokens,
    ):
        result = agent.run_conversation("hello")

    assert result["completed"] is True
    api_messages = captured["messages"]
    user_msg = next(m for m in api_messages if m.get("role") == "user")
    assert user_msg["content"].startswith("hello\n\n<memory-context>")
    assert "remembered fact" in user_msg["content"]
    assert not any(
        m.get("role") == "assistant"
        and any(
            tc.get("function", {}).get("name") == "hindsight_recall"
            for tc in (m.get("tool_calls") or [])
        )
        for m in api_messages
    )
    assert result["messages"][0]["content"] == "hello"
    assert "<memory-context>" not in json.dumps(result["messages"])
    assert any("remembered fact" in json.dumps(msgs) for msgs in estimate_request_inputs)
    pre_request_calls = [kw for name, kw in hook_calls if name == "pre_api_request"]
    assert len(pre_request_calls) == 1
    assert "remembered fact" in json.dumps(pre_request_calls[0]["request_messages"])
    assert pre_request_calls[0]["message_count"] == len(pre_request_calls[0]["request_messages"])
    assert pre_request_calls[0]["request_char_count"] == sum(
        len(str(msg)) for msg in pre_request_calls[0]["request_messages"]
    )


def test_auto_memory_recall_fallback_appends_text_part_to_current_multimodal_user(agent):
    class _MemoryManager:
        def on_turn_start(self, *args, **kwargs):
            pass

        def prefetch_all(self, query, *, session_id=""):
            return "# Hindsight Memory\n\n- remembered fact"

        def sync_all(self, *args, **kwargs):
            pass

        def queue_prefetch_all(self, *args, **kwargs):
            pass

    captured = {}
    user_content = [
        {"type": "text", "text": "current multimodal user part 1"},
        {"type": "text", "text": "current multimodal user part 2"},
    ]
    history = [
        {"role": "user", "content": "previous clean user"},
        {"role": "assistant", "content": "previous answer"},
    ]

    def _fake_api_call(api_kwargs):
        captured["messages"] = api_kwargs["messages"]
        return _mock_response(content="ok")

    agent._memory_manager = _MemoryManager()
    agent._interruptible_api_call = _fake_api_call
    agent._persist_session = lambda *args, **kwargs: None
    agent._save_trajectory = lambda *args, **kwargs: None

    result = agent.run_conversation(user_content, conversation_history=history)

    assert result["completed"] is True
    api_messages = captured["messages"]
    previous_user = next(m for m in api_messages if m.get("content") == "previous clean user")
    assert "<memory-context>" not in previous_user["content"]
    current_user = [m for m in api_messages if m.get("role") == "user"][-1]
    assert isinstance(current_user["content"], list)
    assert current_user["content"][:-1] == user_content
    memory_part = current_user["content"][-1]
    assert memory_part["type"] == "text"
    assert memory_part["text"].startswith("<memory-context>")
    assert "remembered fact" in memory_part["text"]
    assert result["messages"][-2]["content"] == user_content


def test_auto_memory_recall_codex_app_server_uses_ephemeral_user_suffix(agent):
    class _MemoryManager:
        def on_turn_start(self, *args, **kwargs):
            pass

        def prefetch_all(self, query, *, session_id=""):
            return "# Hindsight Memory\n\n- remembered fact"

        def sync_all(self, *args, **kwargs):
            pass

        def queue_prefetch_all(self, *args, **kwargs):
            pass

    captured = {}

    def _fake_codex_app_turn(**kwargs):
        captured.update(kwargs)
        return {
            "final_response": "ok",
            "messages": kwargs["messages"],
            "api_calls": 1,
            "completed": True,
            "partial": False,
            "error": None,
        }

    agent.api_mode = "codex_app_server"
    agent._memory_manager = _MemoryManager()
    agent._run_codex_app_server_turn = _fake_codex_app_turn
    agent._persist_session = lambda *args, **kwargs: None
    agent._save_trajectory = lambda *args, **kwargs: None

    result = agent.run_conversation("hello")

    assert result["completed"] is True
    assert captured["user_message"].startswith("hello\n\n<memory-context>")
    assert "remembered fact" in captured["user_message"]
    assert captured["original_user_message"] == "hello"
    assert result["messages"][0]["content"] == "hello"
    assert "<memory-context>" not in json.dumps(result["messages"])


def test_chat_messages_to_responses_input_preserves_developer_tail(monkeypatch):
    _build_agent(monkeypatch)
    from agent.codex_responses_adapter import _chat_messages_to_responses_input

    items = _chat_messages_to_responses_input(
        [
            {"role": "user", "content": "Clean user text"},
            {"role": "developer", "content": "<memory-context>remembered fact</memory-context>"},
        ]
    )

    assert items == [
        {"role": "user", "content": "Clean user text"},
        {"role": "developer", "content": "<memory-context>remembered fact</memory-context>"},
    ]


def test_preflight_codex_api_kwargs_allows_developer_input_item(monkeypatch):
    _build_agent(monkeypatch)
    from agent.codex_responses_adapter import _preflight_codex_api_kwargs

    preflight = _preflight_codex_api_kwargs(
        {
            "model": "gpt-5-codex",
            "instructions": "You are Hermes.",
            "input": [
                {"role": "user", "content": "Clean user text"},
                {"role": "developer", "content": "<memory-context>remembered fact</memory-context>"},
            ],
            "tools": [],
            "store": False,
        }
    )

    assert preflight["input"][-1] == {
        "role": "developer",
        "content": "<memory-context>remembered fact</memory-context>",
    }
