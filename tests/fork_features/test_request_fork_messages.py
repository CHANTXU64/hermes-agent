"""Native message protocols use the same isolated compression fork contract."""
import copy
from types import SimpleNamespace

import pytest

from fork_features.request_fork import (
    RequestForkService, compression_request_fork_enabled,
    current_request_fork_scope, freeze_request_for_compression,
)
from agent.transports.anthropic import AnthropicTransport
from agent.transports.chat_completions import ChatCompletionsTransport


@pytest.mark.parametrize("mode", ["anthropic_messages", "chat_completions"])
def test_messages_fork_keeps_native_prefix_and_returns_tools_without_execution(monkeypatch, mode):
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _: True)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    transport = AnthropicTransport() if mode == "anthropic_messages" else ChatCompletionsTransport()
    agent = SimpleNamespace(api_mode=mode, provider="test", platform="telegram",
                            session_id="s", _client_kwargs={}, _api_max_retries=1,
                            _get_transport=lambda: transport)
    body = {"model": "test-model", "messages": [{"role": "user", "content": "full prefix"}],
            "max_tokens": 1024}
    if mode == "anthropic_messages":
        body["system"] = [{"type": "text", "text": "original system", "cache_control": {"type": "ephemeral"}}]
        body["tools"] = [{"name": "dangerous_tool", "input_schema": {"type": "object"}}]
    else:
        body["tools"] = [{"type": "function", "function": {"name": "dangerous_tool", "parameters": {"type": "object"}}}]
    baseline = copy.deepcopy(body)
    assert compression_request_fork_enabled(agent)
    frozen = freeze_request_for_compression(agent, body, fidelity="prepared_parent")
    assert frozen is not None
    received = []
    closed = []
    response = (SimpleNamespace(content=[SimpleNamespace(type="text", text='{"ok":true}'),
                                        SimpleNamespace(type="tool_use", id="t", name="dangerous_tool", input={})],
                                stop_reason="tool_use", usage=SimpleNamespace(input_tokens=10, output_tokens=5))
                if mode == "anthropic_messages" else
                SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}',
                    tool_calls=[SimpleNamespace(id="t", function=SimpleNamespace(name="dangerous_tool", arguments="{}"))]),
                    finish_reason="tool_calls")], usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5)))
    def send(client, kwargs, *, mode, progress=None):
        received.append(copy.deepcopy(kwargs))
        return response
    # Patch only the physical provider boundary; freezing/normalization/ownership are real.
    monkeypatch.setattr("fork_features.request_fork.message_protocols.send_messages", send, raising=False)
    with current_request_fork_scope(agent, frozen_request=frozen,
                                    client_factory=lambda: object(), client_close=closed.append):
        fork = RequestForkService().capture_current()
    result = fork.call(append_message={"role": "user", "content": "checkpoint"}, request_id="test")
    assert result.raw_output == '{"ok":true}'
    assert len(result.tool_calls) == 1
    assert received[0]["messages"] == baseline["messages"] + [{"role": "user", "content": "checkpoint"}]
    assert {k:v for k,v in received[0].items() if k != "messages"} == {k:v for k,v in baseline.items() if k != "messages"}
    assert frozen.clone_body() == baseline == body
    assert len(closed) == 1


@pytest.mark.parametrize("mode", ["anthropic_messages", "chat_completions"])
def test_native_adoption_preserves_live_tool_tail_and_request_only_context(monkeypatch, mode):
    from fork_features.request_fork import FrozenRequest
    from fork_features.request_fork.prepared_request import build_adopt_rematerializer
    transport = AnthropicTransport() if mode == "anthropic_messages" else ChatCompletionsTransport()
    agent = SimpleNamespace(api_mode=mode, provider="test", base_url="https://provider.invalid",
                            _get_transport=lambda: transport, _needs_thinking_reasoning_pad=lambda: False,
                            _is_codex_backend=lambda: False)
    prior = {"role": "user", "content": "prior"}
    live = {"role": "user", "content": "live"}
    tool = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "t", "type": "function", "function": {"name": "read", "arguments": "{}"}}]}
    result = {"role": "tool", "tool_call_id": "t", "content": "result"}
    original = [prior, {"role": "assistant", "content": "old answer"}, live, tool, result]
    native = transport.convert_messages(copy.deepcopy(original), base_url=agent.base_url)
    if mode == "anthropic_messages":
        native = native[1]
    # Request-only context belongs to the live user, not durable history.
    anchor = copy.deepcopy(native[2])
    if isinstance(anchor["content"], str):
        anchor["content"] += "\nrequest-only context"
    else:
        anchor["content"].append({"type": "text", "text": "request-only context"})
    native[2] = anchor
    body = {"model": "test", "messages": native, "tools": [], "max_tokens": 1024}
    frozen = FrozenRequest(body=body, fidelity="prepared_parent", api_mode=mode)
    rebuild = build_adopt_rematerializer(agent, prepared_request=frozen, original_messages=original,
                                         current_turn_user_idx=2, canonicalize_tool_calls=lambda _: None)
    assert callable(rebuild)
    added = [{"role": "user", "content": "added"}, {"role": "assistant", "content": "added answer"}]
    adopted = original[:2] + added + original[2:]
    rebuilt = rebuild(adopted)
    assert rebuilt is not None
    messages = rebuilt.clone_body()["messages"]
    assert messages[-3:] == body["messages"][-3:]
    assert "added answer" in str(messages[:-3])
    assert frozen.clone_body() == body


@pytest.mark.parametrize("mode", ["anthropic_messages", "chat_completions"])
def test_manual_snapshot_uses_real_native_transport_without_codex_preflight(monkeypatch, mode):
    from fork_features.request_fork import build_out_of_turn_compression_request_snapshot
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _: True)
    transport = AnthropicTransport() if mode == "anthropic_messages" else ChatCompletionsTransport()
    agent = SimpleNamespace(api_mode=mode,provider="test",platform="cli",session_id="s",tools=[],
                            _cached_system_prompt="system",_get_transport=lambda:transport,
                            _build_api_kwargs=lambda messages,tools_for_api:transport.build_kwargs("m",messages,tools_for_api))
    history=[{"role":"user","content":"full history"}]
    frozen=build_out_of_turn_compression_request_snapshot(agent,history)
    assert frozen.api_mode==mode
    assert "full history" in str(frozen.clone_body()["messages"])
    assert "system" in str(frozen.clone_body())
