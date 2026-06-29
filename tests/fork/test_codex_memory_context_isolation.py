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


def test_auto_memory_recall_uses_codex_developer_after_user_not_instructions_or_user_suffix(agent):
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
    user_index = input_items.index(user_item)
    developer_item = input_items[user_index + 1]
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


def test_auto_memory_recall_codex_developer_stays_after_user_across_tool_loop(agent):
    class _MemoryManager:
        def on_turn_start(self, *args, **kwargs):
            pass

        def prefetch_all(self, query, *, session_id=""):
            return "# Hindsight Memory\n\n- remembered fact"

        def sync_all(self, *args, **kwargs):
            pass

        def queue_prefetch_all(self, *args, **kwargs):
            pass

    captured = []

    def _fake_api_call(api_kwargs):
        captured.append(api_kwargs)
        if len(captured) == 1:
            return SimpleNamespace(
                status="completed",
                output=[
                    SimpleNamespace(
                        type="function_call",
                        status="completed",
                        name="read_file",
                        arguments='{"path":"/tmp/example"}',
                        call_id="call_read",
                        id="fc_read",
                    )
                ],
            )
        return SimpleNamespace(
            status="completed",
            output=[
                SimpleNamespace(
                    type="message",
                    status="completed",
                    content=[SimpleNamespace(type="output_text", text="done")],
                )
            ],
        )

    def _fake_execute_tool_calls(assistant_message, messages, *args):
        messages.append({
            "role": "tool",
            "name": "read_file",
            "tool_call_id": "call_read",
            "content": "tool result",
        })

    agent.api_mode = "codex_responses"
    agent.provider = "openai-codex"
    agent.base_url = "https://chatgpt.com/backend-api/codex"
    agent._base_url_lower = agent.base_url.lower()
    agent._base_url_hostname = "chatgpt.com"
    agent.model = "gpt-5.5"
    agent._memory_manager = _MemoryManager()
    agent._interruptible_api_call = _fake_api_call
    agent._execute_tool_calls = _fake_execute_tool_calls
    agent.valid_tool_names.add("read_file")
    agent._persist_session = lambda *args, **kwargs: None
    agent._save_trajectory = lambda *args, **kwargs: None
    agent._cleanup_task_resources = lambda *args, **kwargs: None

    result = agent.run_conversation("hello")

    assert result["completed"] is True
    assert len(captured) == 2
    second_input = captured[1]["input"]
    user_index = next(i for i, item in enumerate(second_input) if item.get("role") == "user")
    developer_index = next(i for i, item in enumerate(second_input) if item.get("role") == "developer")
    function_call_index = next(i for i, item in enumerate(second_input) if item.get("type") == "function_call")
    function_output_index = next(i for i, item in enumerate(second_input) if item.get("type") == "function_call_output")
    assert developer_index == user_index + 1
    assert developer_index < function_call_index < function_output_index
    assert second_input[developer_index]["content"].startswith("<memory-context>")
    assert "remembered fact" in second_input[developer_index]["content"]
    assert second_input[-1].get("role") != "developer"
    assert result["messages"][0]["content"] == "hello"
    assert "<memory-context>" not in json.dumps(result["messages"])


def test_auto_memory_recall_codex_replays_prior_turn_developer_for_cache_affinity(agent, monkeypatch):
    """Regression from aa645: next turn must not rewrite the prior memory slot.

    The simplified same-turn test above only catches `user -> developer -> tool`
    within one loop.  The Langfuse aa645 drop happened across turns: the prior
    request ended with tool history plus a request-only developer memory block,
    but the next turn rebuilt history without that developer item and rewrote the
    same prefix position with assistant/user items.  That destroys append-only
    prompt-cache affinity even when prompt_cache_key and instructions are stable.
    """

    class _MemoryManager:
        def on_turn_start(self, *args, **kwargs):
            pass

        def prefetch_all(self, query, *, session_id=""):
            return f"# Hindsight Memory\n\n- remembered fact for {query}"

        def sync_all(self, *args, **kwargs):
            pass

        def queue_prefetch_all(self, *args, **kwargs):
            pass

    captured = []
    dumped_bodies = []

    def _fake_api_call(api_kwargs):
        captured.append(api_kwargs)
        if len(captured) == 1:
            return SimpleNamespace(
                status="completed",
                output=[
                    SimpleNamespace(
                        type="reasoning",
                        status="completed",
                        encrypted_content="encrypted-first-turn",
                        summary=[SimpleNamespace(text="need a tool")],
                    ),
                    SimpleNamespace(
                        type="function_call",
                        status="completed",
                        name="read_file",
                        arguments='{"path":"/tmp/example"}',
                        call_id="call_read",
                        id="fc_read",
                    ),
                ],
            )
        if len(captured) == 2:
            return SimpleNamespace(
                status="completed",
                output=[
                    SimpleNamespace(
                        type="message",
                        status="completed",
                        id="msg_first_final",
                        content=[SimpleNamespace(type="output_text", text="first done")],
                    ),
                ],
            )
        return SimpleNamespace(
            status="completed",
            output=[
                SimpleNamespace(
                    type="message",
                    status="completed",
                    id="msg_second_final",
                    content=[SimpleNamespace(type="output_text", text="second done")],
                ),
            ],
        )

    def _fake_execute_tool_calls(assistant_message, messages, *args):
        messages.append({
            "role": "tool",
            "name": "read_file",
            "tool_call_id": "call_read",
            "content": "tool result",
        })

    def _capture_dump(api_kwargs, *, reason, error=None):
        dumped_bodies.append(json.loads(json.dumps(api_kwargs)))

    agent.api_mode = "codex_responses"
    agent.provider = "openai-codex"
    agent.base_url = "https://chatgpt.com/backend-api/codex"
    agent._base_url_lower = agent.base_url.lower()
    agent._base_url_hostname = "chatgpt.com"
    agent.model = "gpt-5.5"
    agent._memory_manager = _MemoryManager()
    agent._interruptible_api_call = _fake_api_call
    agent._execute_tool_calls = _fake_execute_tool_calls
    agent._dump_api_request_debug = _capture_dump
    agent.valid_tool_names.add("read_file")
    agent._persist_session = lambda *args, **kwargs: None
    agent._save_trajectory = lambda *args, **kwargs: None
    agent._cleanup_task_resources = lambda *args, **kwargs: None
    monkeypatch.setenv("HERMES_DUMP_REQUESTS", "1")

    first_result = agent.run_conversation("first turn")
    second_result = agent.run_conversation(
        "second turn",
        conversation_history=first_result["messages"],
    )

    assert first_result["completed"] is True
    assert second_result["completed"] is True
    assert len(captured) == 3
    assert len(dumped_bodies) == 3

    first_final_request = dumped_bodies[1]["input"]
    second_turn_request = dumped_bodies[2]["input"]
    first_user_index = next(
        i for i, item in enumerate(first_final_request)
        if item.get("role") == "user" and item.get("content") == "first turn"
    )
    first_developer_index = next(
        i for i, item in enumerate(first_final_request)
        if item.get("role") == "developer"
    )
    first_reasoning_index = next(
        i for i, item in enumerate(first_final_request)
        if item.get("type") == "reasoning"
    )
    first_function_call_index = next(
        i for i, item in enumerate(first_final_request)
        if item.get("type") == "function_call"
    )
    first_function_output_index = next(
        i for i, item in enumerate(first_final_request)
        if item.get("type") == "function_call_output"
    )
    assert first_developer_index == first_user_index + 1
    assert (
        first_developer_index
        < first_reasoning_index
        < first_function_call_index
        < first_function_output_index
    )
    first_developer_item = first_final_request[first_developer_index]

    assert "remembered fact for first turn" in first_developer_item["content"]
    assert second_turn_request[first_developer_index] == first_developer_item

    developer_indices = [
        i for i, item in enumerate(second_turn_request)
        if item.get("role") == "developer"
    ]
    assert len(developer_indices) == 2
    second_user_index = next(
        i for i, item in enumerate(second_turn_request)
        if item.get("role") == "user" and item.get("content") == "second turn"
    )
    assert developer_indices[-1] == second_user_index + 1
    assert "remembered fact for second turn" in second_turn_request[developer_indices[-1]]["content"]
    assert "<memory-context>" not in json.dumps(second_result["messages"])


def test_auto_memory_recall_codex_preflight_dump_keeps_developer_after_repaired_user(agent, monkeypatch):
    """The dumped final request body must use the post-repair user index.

    Production aa645 dumps showed the request-only developer item moving to the
    tail after reasoning/function_call/function_call_output.  That happens when
    pre-call repair removes a row before the current user and the memory marker
    still targets the stale pre-repair index.
    """

    class _MemoryManager:
        def on_turn_start(self, *args, **kwargs):
            pass

        def prefetch_all(self, query, *, session_id=""):
            return "# Hindsight Memory\n\n- remembered fact"

        def sync_all(self, *args, **kwargs):
            pass

        def queue_prefetch_all(self, *args, **kwargs):
            pass

    api_calls = []
    dumped_bodies = []

    def _fake_api_call(api_kwargs):
        api_calls.append(api_kwargs)
        if len(api_calls) == 1:
            return SimpleNamespace(
                status="completed",
                output=[
                    SimpleNamespace(
                        type="reasoning",
                        status="completed",
                        encrypted_content="encrypted-turn",
                        summary=[SimpleNamespace(text="need a tool")],
                    ),
                    SimpleNamespace(
                        type="function_call",
                        status="completed",
                        name="read_file",
                        arguments='{"path":"/tmp/example"}',
                        call_id="call_read",
                        id="fc_read",
                    ),
                ],
            )
        return SimpleNamespace(
            status="completed",
            output=[
                SimpleNamespace(
                    type="message",
                    status="completed",
                    id="msg_final",
                    content=[SimpleNamespace(type="output_text", text="done")],
                ),
            ],
        )

    def _fake_execute_tool_calls(assistant_message, messages, *args):
        messages.append({
            "role": "tool",
            "name": "read_file",
            "tool_call_id": "call_read",
            "content": "tool result",
        })

    def _capture_dump(api_kwargs, *, reason, error=None):
        dumped_bodies.append(json.loads(json.dumps(api_kwargs)))

    agent.api_mode = "codex_responses"
    agent.provider = "openai-codex"
    agent.base_url = "https://chatgpt.com/backend-api/codex"
    agent._base_url_lower = agent.base_url.lower()
    agent._base_url_hostname = "chatgpt.com"
    agent.model = "gpt-5.5"
    agent._memory_manager = _MemoryManager()
    agent._interruptible_api_call = _fake_api_call
    agent._execute_tool_calls = _fake_execute_tool_calls
    agent._dump_api_request_debug = _capture_dump
    agent.valid_tool_names.add("read_file")
    agent._persist_session = lambda *args, **kwargs: None
    agent._save_trajectory = lambda *args, **kwargs: None
    agent._cleanup_task_resources = lambda *args, **kwargs: None
    monkeypatch.setenv("HERMES_DUMP_REQUESTS", "1")

    result = agent.run_conversation(
        "hello",
        conversation_history=[
            {
                "role": "tool",
                "tool_call_id": "orphaned_before_current_user",
                "content": "stale orphan",
            }
        ],
    )

    assert result["completed"] is True
    assert len(dumped_bodies) == 2

    final_input = dumped_bodies[1]["input"]
    user_index = next(
        i for i, item in enumerate(final_input)
        if item.get("role") == "user" and item.get("content") == "hello"
    )
    developer_index = next(
        i for i, item in enumerate(final_input)
        if item.get("role") == "developer"
    )
    reasoning_index = next(
        i for i, item in enumerate(final_input)
        if item.get("type") == "reasoning"
    )
    function_call_index = next(
        i for i, item in enumerate(final_input)
        if item.get("type") == "function_call"
    )
    function_output_index = next(
        i for i, item in enumerate(final_input)
        if item.get("type") == "function_call_output"
    )

    assert developer_index == user_index + 1
    assert (
        developer_index
        < reasoning_index
        < function_call_index
        < function_output_index
    )
    assert final_input[-1].get("role") != "developer"
    assert "remembered fact" in final_input[developer_index]["content"]
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


def test_chat_messages_to_responses_input_preserves_developer_item(monkeypatch):
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
