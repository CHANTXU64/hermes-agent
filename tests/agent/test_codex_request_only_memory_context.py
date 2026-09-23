"""Fork regressions for request-only Codex memory-context placement."""

from __future__ import annotations

import json
from types import SimpleNamespace

from tests.agent.test_run_agent_codex_responses import _build_agent


class _MemoryManager:
    def on_turn_start(self, *args, **kwargs):
        pass

    def prefetch_all(self, query, **kwargs):
        return f"# Hindsight Memory\n\n- remembered fact for {query}"

    def sync_all(self, *args, **kwargs):
        pass

    def queue_prefetch_all(self, *args, **kwargs):
        pass


def _configure(agent, api_call):
    agent.api_mode = "codex_responses"
    agent.provider = "openai-codex"
    agent.base_url = "https://chatgpt.com/backend-api/codex"
    agent._base_url_lower = agent.base_url.lower()
    agent._base_url_hostname = "chatgpt.com"
    agent.model = "gpt-5.5"
    agent._memory_manager = _MemoryManager()
    agent._interruptible_api_call = api_call
    agent._persist_session = lambda *args, **kwargs: None
    agent._save_trajectory = lambda *args, **kwargs: None
    agent._cleanup_task_resources = lambda *args, **kwargs: None


def _final_response(text: str):
    return SimpleNamespace(
        status="completed",
        output=[
            SimpleNamespace(
                type="message",
                status="completed",
                content=[SimpleNamespace(type="output_text", text=text)],
            )
        ],
    )


def _input_text(item: dict) -> str:
    """Flatten a Responses API input item's typed text parts for assertions."""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") in {"input_text", "output_text"}
        )
    return ""


def test_current_recall_uses_developer_after_clean_user(monkeypatch):
    agent = _build_agent(monkeypatch)
    captured = {}
    prompt = "Explain the database migration plan"

    def _api_call(api_kwargs):
        captured.update(api_kwargs)
        return _final_response("ok")

    _configure(agent, _api_call)
    result = agent.run_conversation(prompt)

    assert result["completed"] is True
    assert "remembered fact" not in captured["instructions"]
    input_items = captured["input"]
    user_index = next(
        i for i, item in enumerate(input_items)
        if item.get("role") == "user" and _input_text(item) == prompt
    )
    developer = input_items[user_index + 1]
    assert developer["role"] == "developer"
    developer_text = _input_text(developer)
    assert developer_text.startswith("<memory-context>")
    assert f"remembered fact for {prompt}" in developer_text
    assert result["messages"][0]["content"] == prompt
    assert "<memory-context>" not in json.dumps(result["messages"])


def test_lcm_request_context_precedes_memory_in_one_developer_after_clean_user(
    monkeypatch,
):
    agent = _build_agent(monkeypatch)
    captured = {}
    prompt = "Explain the database migration plan"
    lcm_policy = "HERMES-LCM-POLICY-SENTINEL"

    def _api_call(api_kwargs):
        captured.update(api_kwargs)
        return _final_response("ok")

    def _invoke_hook(name, **_kwargs):
        if name == "pre_llm_call":
            return [{"context": lcm_policy, "target": "request_context"}]
        return []

    _configure(agent, _api_call)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", _invoke_hook)
    result = agent.run_conversation(prompt)

    assert result["completed"] is True
    input_items = captured["input"]
    user_index = next(i for i, item in enumerate(input_items) if item.get("role") == "user")
    assert _input_text(input_items[user_index]) == prompt
    developer_items = [item for item in input_items if item.get("role") == "developer"]
    assert len(developer_items) == 1
    developer = input_items[user_index + 1]
    assert developer["role"] == "developer"
    developer_text = _input_text(developer)
    assert developer_text.index(lcm_policy) < developer_text.index("<memory-context>")
    assert f"remembered fact for {prompt}" in developer_text
    assert lcm_policy not in json.dumps(result["messages"])


def test_ordinary_plugin_context_stays_on_openai_user_request_copy(monkeypatch):
    agent = _build_agent(monkeypatch)
    captured = {}
    prompt = "Explain the database migration plan"
    plugin_context = "ORDINARY-PLUGIN-CONTEXT-SENTINEL"

    def _api_call(api_kwargs):
        captured.update(api_kwargs)
        return _final_response("ok")

    def _invoke_hook(name, **_kwargs):
        if name == "pre_llm_call":
            return [{"context": plugin_context}]
        return []

    _configure(agent, _api_call)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", _invoke_hook)
    result = agent.run_conversation(prompt)

    assert result["completed"] is True
    input_items = captured["input"]
    user_index = next(i for i, item in enumerate(input_items) if item.get("role") == "user")
    assert _input_text(input_items[user_index]) == prompt + "\n\n" + plugin_context
    developer = input_items[user_index + 1]
    assert developer["role"] == "developer"
    assert plugin_context not in _input_text(developer)
    assert plugin_context not in json.dumps(result["messages"])


def test_current_recall_developer_position_is_stable_across_tool_loop(monkeypatch):
    agent = _build_agent(monkeypatch)
    captured = []
    prompt = "Review the database migration plan"
    lcm_policy = "LCM-TOOL-LOOP-POLICY"

    def _api_call(api_kwargs):
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
        return _final_response("done")

    def _execute_tool_calls(assistant_message, messages, *args):
        messages.append(
            {
                "role": "tool",
                "name": "read_file",
                "tool_call_id": "call_read",
                "content": "tool result",
            }
        )

    def _invoke_hook(name, **_kwargs):
        if name == "pre_llm_call":
            return [{"context": lcm_policy, "target": "request_context"}]
        return []

    _configure(agent, _api_call)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", _invoke_hook)
    setattr(agent, "_execute_tool_calls", _execute_tool_calls)
    getattr(agent, "valid_tool_names").add("read_file")
    result = agent.run_conversation(prompt)

    assert result["completed"] is True
    assert len(captured) == 2
    second_input = captured[1]["input"]
    user_index = next(i for i, item in enumerate(second_input) if item.get("role") == "user")
    developer_index = next(i for i, item in enumerate(second_input) if item.get("role") == "developer")
    function_call_index = next(
        i for i, item in enumerate(second_input) if item.get("type") == "function_call"
    )
    function_output_index = next(
        i for i, item in enumerate(second_input) if item.get("type") == "function_call_output"
    )
    assert developer_index == user_index + 1
    assert developer_index < function_call_index < function_output_index
    assert _input_text(second_input[user_index]) == prompt
    developer_text = _input_text(second_input[developer_index])
    assert developer_text.index(lcm_policy) < developer_text.index("<memory-context>")
    assert f"remembered fact for {prompt}" in developer_text
    assert second_input[-1].get("role") != "developer"


def test_next_turn_drops_prior_recall_and_keeps_only_current_developer(monkeypatch):
    agent = _build_agent(monkeypatch)
    captured = []

    def _api_call(api_kwargs):
        captured.append(api_kwargs)
        return _final_response(f"done-{len(captured)}")

    def _invoke_hook(name, **kwargs):
        if name == "pre_llm_call":
            return [
                {
                    "context": f"LCM-POLICY-{kwargs['user_message']}",
                    "target": "request_context",
                }
            ]
        return []

    _configure(agent, _api_call)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", _invoke_hook)
    first = agent.run_conversation("first turn")
    second = agent.run_conversation(
        "second turn",
        conversation_history=first["messages"],
    )

    assert first["completed"] is True
    assert second["completed"] is True
    assert len(captured) == 2

    first_input = captured[0]["input"]
    first_user_index = next(
        i for i, item in enumerate(first_input)
        if item.get("role") == "user" and _input_text(item) == "first turn"
    )
    assert first_input[first_user_index + 1]["role"] == "developer"
    first_developer_text = _input_text(first_input[first_user_index + 1])
    assert "LCM-POLICY-first turn" in first_developer_text
    assert "remembered fact for first turn" in first_developer_text

    second_input = captured[1]["input"]
    serialized_second = json.dumps(second_input)
    assert "remembered fact for first turn" not in serialized_second
    assert "LCM-POLICY-first turn" not in serialized_second
    developer_indices = [
        i for i, item in enumerate(second_input) if item.get("role") == "developer"
    ]
    assert len(developer_indices) == 1
    second_user_index = next(
        i for i, item in enumerate(second_input)
        if item.get("role") == "user" and _input_text(item) == "second turn"
    )
    assert developer_indices[0] == second_user_index + 1
    second_developer_text = _input_text(second_input[developer_indices[0]])
    assert "LCM-POLICY-second turn" in second_developer_text
    assert "remembered fact for second turn" in second_developer_text
    assert "<memory-context>" not in json.dumps(second["messages"])


def test_max_iteration_summary_keeps_codex_recall_after_clean_user(monkeypatch):
    from agent.chat_completion_helpers import handle_max_iterations

    agent = _build_agent(monkeypatch)
    captured = {}

    def _summary_call(api_kwargs):
        captured.update(api_kwargs)
        return _final_response("summary")

    _configure(agent, _summary_call)
    agent._run_codex_stream = _summary_call
    messages = [
        {"role": "user", "content": "current question"},
        {"role": "assistant", "content": "still working"},
    ]

    out = handle_max_iterations(
        agent,
        messages,
        5,
        current_turn_user_idx=0,
        ext_prefetch_cache="CODEX-SUMMARY-RECALL-SENTINEL",
        plugin_user_context="CODEX-SUMMARY-PLUGIN-SENTINEL",
        plugin_request_context="CODEX-SUMMARY-LCM-SENTINEL",
    )

    assert out == "summary"
    input_items = captured["input"]
    current_user_index = next(
        index
        for index, item in enumerate(input_items)
        if item.get("role") == "user"
        and "current question" in _input_text(item)
    )
    current_user = input_items[current_user_index]
    current_user_text = _input_text(current_user)
    assert "CODEX-SUMMARY-PLUGIN-SENTINEL" in current_user_text
    assert "CODEX-SUMMARY-LCM-SENTINEL" not in current_user_text
    assert "CODEX-SUMMARY-RECALL-SENTINEL" not in current_user_text
    developer = input_items[current_user_index + 1]
    assert developer["role"] == "developer"
    developer_text = _input_text(developer)
    assert developer_text.index("CODEX-SUMMARY-LCM-SENTINEL") < developer_text.index(
        "<memory-context>"
    )
    assert "CODEX-SUMMARY-RECALL-SENTINEL" in developer_text
    assert messages[0] == {"role": "user", "content": "current question"}
    assert "CODEX-SUMMARY-RECALL-SENTINEL" not in json.dumps(messages)


def test_openai_api_provider_uses_generic_user_suffix(monkeypatch):
    """Ordinary openai-api routes keep request-only context in the user item."""
    agent = _build_agent(monkeypatch)
    captured = {}
    prompt = "List the deployment steps"

    def _api_call(api_kwargs):
        captured.update(api_kwargs)
        return _final_response("ok")

    _configure(agent, _api_call)
    agent.provider = "openai-api"
    agent.base_url = "https://generic.example.test/v1"
    agent._base_url_lower = agent.base_url.lower()
    agent._base_url_hostname = "generic.example.test"
    result = agent.run_conversation(prompt)

    assert result["completed"] is True
    assert "remembered fact" not in captured["instructions"]
    input_items = captured["input"]
    user_index = next(
        i for i, item in enumerate(input_items)
        if item.get("role") == "user"
        and str(item.get("content", "")).startswith(prompt)
    )
    assert f"remembered fact for {prompt}" in input_items[user_index]["content"]
    assert not any(
        item.get("role") == "developer"
        and "remembered fact" in str(item.get("content", ""))
        for item in input_items[user_index + 1:]
    )
