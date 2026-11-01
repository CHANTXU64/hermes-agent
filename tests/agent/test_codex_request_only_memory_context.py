"""Responses routes use official user-suffix memory and exact cross-turn replay."""

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


def _assert_user_suffix(captured, prompt):
    user = next(item for item in captured["input"]
                if item.get("role") == "user" and _input_text(item).startswith(prompt + "\n\n"))
    text = _input_text(user)
    assert f"remembered fact for {prompt}" in text
    assert "<memory-context>" in text
    assert "remembered fact" not in captured["instructions"]
    assert not any(item.get("role") == "developer" for item in captured["input"])
    return text


def test_current_recall_uses_official_user_suffix(monkeypatch):
    agent = _build_agent(monkeypatch)
    captured = {}
    prompt = "Explain the database migration plan"
    def call(kwargs):
        captured.update(kwargs)
        return _final_response("ok")
    _configure(agent, call)
    result = agent.run_conversation(prompt)
    assert result["completed"] is True
    text = _assert_user_suffix(captured, prompt)
    assert result["messages"][0]["content"] == prompt
    assert result["messages"][0]["api_content"] == text


def test_lcm_hook_context_and_recall_follow_official_user_suffix(monkeypatch):
    agent = _build_agent(monkeypatch)
    captured = {}
    prompt = "Explain the database migration plan"
    policy = "HERMES-LCM-POLICY-SENTINEL"
    def call(kwargs):
        captured.update(kwargs)
        return _final_response("ok")
    def hook(name, **kwargs):
        return [{"context": policy, "target": "request_context"}] if name == "pre_llm_call" else []
    _configure(agent, call)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", hook)
    result = agent.run_conversation(prompt)
    assert result["completed"] is True
    text = _assert_user_suffix(captured, prompt)
    assert text.index("<memory-context>") < text.index(policy)
    assert result["messages"][0]["content"] == prompt
    assert result["messages"][0]["api_content"] == text


def test_ordinary_plugin_context_follows_memory_in_user_suffix(monkeypatch):
    agent = _build_agent(monkeypatch)
    captured = {}
    prompt = "Explain the database migration plan"
    context = "ORDINARY-PLUGIN-CONTEXT-SENTINEL"
    def call(kwargs):
        captured.update(kwargs)
        return _final_response("ok")
    def hook(name, **kwargs):
        return [{"context": context}] if name == "pre_llm_call" else []
    _configure(agent, call)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", hook)
    result = agent.run_conversation(prompt)
    assert result["completed"] is True
    text = _assert_user_suffix(captured, prompt)
    assert text.endswith(context)
    assert result["messages"][0]["content"] == prompt
    assert result["messages"][0]["api_content"] == text


def test_recall_user_suffix_is_stable_across_tool_loop(monkeypatch):
    agent = _build_agent(monkeypatch)
    captured = []
    prompt = "Review the database migration plan"
    def call(kwargs):
        captured.append(kwargs)
        if len(captured) == 1:
            return SimpleNamespace(status="completed", output=[SimpleNamespace(
                type="function_call", status="completed", name="read_file",
                arguments='{"path":"example"}', call_id="call_read", id="fc_read")])
        return _final_response("done")
    def tools(assistant, messages, *args):
        messages.append({"role": "tool", "name": "read_file", "tool_call_id": "call_read", "content": "tool result"})
    _configure(agent, call)
    setattr(agent, "_execute_tool_calls", tools)
    getattr(agent, "valid_tool_names").add("read_file")
    result = agent.run_conversation(prompt)
    assert result["completed"] is True
    assert len(captured) == 2
    first_text = _assert_user_suffix(captured[0], prompt)
    assert _assert_user_suffix(captured[1], prompt) == first_text
    items = captured[1]["input"]
    user_idx = next(i for i, item in enumerate(items) if item.get("role") == "user")
    call_idx = next(i for i, item in enumerate(items) if item.get("type") == "function_call")
    result_idx = next(i for i, item in enumerate(items) if item.get("type") == "function_call_output")
    assert user_idx < call_idx < result_idx


def test_next_turn_replays_prior_recall_exactly_and_adds_current_suffix(monkeypatch):
    agent = _build_agent(monkeypatch)
    captured = []
    def call(kwargs):
        captured.append(kwargs)
        return _final_response("done")
    def hook(name, **kwargs):
        return [{"context": "LCM-POLICY-" + kwargs["user_message"], "target": "request_context"}] if name == "pre_llm_call" else []
    _configure(agent, call)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", hook)
    first = agent.run_conversation("first turn")
    second = agent.run_conversation("second turn", conversation_history=first["messages"])
    assert first["completed"] is second["completed"] is True
    assert len(captured) == 2
    first_text = _assert_user_suffix(captured[0], "first turn")
    assert "LCM-POLICY-first turn" in first_text
    assert _assert_user_suffix(captured[1], "first turn") == first_text
    assert "LCM-POLICY-second turn" in _assert_user_suffix(captured[1], "second turn")
    assert all("<memory-context>" not in str(row.get("content", "")) for row in second["messages"])


def test_max_iteration_summary_replays_persisted_user_suffix(monkeypatch):
    from agent.chat_completion_helpers import handle_max_iterations
    from agent.turn_context import compose_user_api_content
    agent = _build_agent(monkeypatch)
    captured = {}
    def call(kwargs):
        captured.update(kwargs)
        return _final_response("summary")
    _configure(agent, call)
    agent._run_codex_stream = call
    sent = compose_user_api_content("current question", "CODEX-SUMMARY-RECALL-SENTINEL", "CODEX-SUMMARY-PLUGIN-SENTINEL\n\nCODEX-SUMMARY-LCM-SENTINEL")
    messages = [{"role": "user", "content": "current question", "api_content": sent},
                {"role": "assistant", "content": "still working"}]
    before = json.dumps(messages, sort_keys=True)
    assert handle_max_iterations(agent, messages, 5) == "summary"
    user = next(item for item in captured["input"] if item.get("role") == "user" and _input_text(item).startswith("current question"))
    assert _input_text(user) == sent
    assert not any(item.get("role") == "developer" for item in captured["input"])
    # Upstream persists its final summary exchange; existing history must stay unchanged.
    assert json.dumps(messages[:2], sort_keys=True) == before
    assert [m["role"] for m in messages[2:]] == ["user", "assistant"]
    assert "maximum number of tool-calling iterations" in messages[2]["content"]
    assert messages[3]["content"] == "summary"


def test_openai_api_provider_uses_same_official_user_suffix(monkeypatch):
    agent = _build_agent(monkeypatch)
    captured = {}
    prompt = "List the deployment steps"
    def call(kwargs):
        captured.update(kwargs)
        return _final_response("ok")
    _configure(agent, call)
    agent.provider = "openai-api"
    agent.base_url = "https://generic.example.test/v1"
    agent._base_url_lower = agent.base_url.lower()
    agent._base_url_hostname = "generic.example.test"
    result = agent.run_conversation(prompt)
    assert result["completed"] is True
    _assert_user_suffix(captured, prompt)
