"""Manual checkpoint forks retain the last native prefix, never a chat replay sidecar."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.conversation_loop import _canonicalize_api_tool_calls  # bootstrap before the per-test home guard
from agent.transports.codex import ResponsesApiTransport
from agent.turn_api_call import perform_api_call
from agent.turn_retry_state import TurnRetryState


def _source():
    transport = ResponsesApiTransport()
    return SimpleNamespace(
        api_mode="codex_responses", provider="custom", model="gpt-test",
        base_url="https://api.example.test/v1", session_id="session-one",
        platform="telegram", is_subagent=False, tools=[],
        _disable_streaming=True, thinking_callback=None,
        _is_copilot_url=lambda: False, _is_codex_backend=lambda: False,
        _get_transport=lambda: transport, _has_pending_redirect=lambda: False,
        _needs_thinking_reasoning_pad=lambda: False,
    )


def _physical_call(source, history, body):
    capture = {}
    response = SimpleNamespace(choices=[])
    source._interruptible_api_call = lambda request: response
    with (
        patch("hermes_cli.lifecycle.has_hook", return_value=True),
        patch("hermes_cli.middleware.run_llm_execution_middleware",
              side_effect=lambda request, call, **kw: call(request)),
        patch("agent.relay_llm.execute", side_effect=lambda request, call, **kw: call(request)),
    ):
        verdict = perform_api_call(
            source, api_kwargs=body, _original_api_kwargs=deepcopy(body),
            _llm_middleware_trace=[], _moa_prepared_request=None,
            _retry=TurnRetryState(), thinking_spinner=None, retry_count=0,
            api_call_count=1, api_request_id="request-one", effective_task_id="session-one",
            turn_id="turn-one", interrupted=False, messages=history,
            current_turn_user_idx=len(history) - 1, _physical_request_capture=capture,
        )
    assert verdict.response is response
    assert capture.get("request_fork") is not None
    return capture


def test_physical_parent_prefix_survives_turn_for_manual_checkpoint():
    source = _source()
    history = [{"role": "user", "content": "commit please"}]
    body = {
        "model": "gpt-test", "instructions": "SYSTEM + LIVE GATEWAY CONTEXT",
        "input": [{"role": "user", "content": "commit please\nCURRENT RECALL"}],
        "tools": [{"type": "function", "name": "read_file", "parameters": {}}],
        "prompt_cache_key": "original-cache-identity",
    }
    capture = _physical_call(source, history, body)
    assert getattr(source, "_continuity_manual_request", None) is not None, (
        "the real physical request is discarded at turn end; manual checkpoint would rebuild "
        "from the memory-only temporary agent instead of reusing its native prefix"
    )
    assert source._continuity_manual_request.request is capture["request_fork"]


def test_optional_retention_failure_cannot_erase_overflow_capture():
    source = _source()
    history = [{"role": "user", "content": "hello"}]
    body = {"model": "gpt-test", "instructions": "system", "input": deepcopy(history)}
    with patch("fork_features.request_fork.manual._fingerprint", side_effect=ValueError("retention only")):
        capture = _physical_call(source, history, body)
    assert capture["request_fork"].clone_body()["input"]
    assert getattr(source, "_continuity_manual_request", None) is None


def _saved_parent(mode="codex_responses"):
    from fork_features.request_fork import FrozenRequest
    from fork_features.request_fork.manual import remember_manual_request

    parent = _source()
    parent.api_mode = mode
    if mode == "chat_completions":
        from agent.transports.chat_completions import ChatCompletionsTransport
        transport = ChatCompletionsTransport()
        parent._get_transport = lambda: transport
    elif mode == "anthropic_messages":
        from agent.transports.anthropic import AnthropicTransport
        transport = AnthropicTransport()
        parent._get_transport = lambda: transport
    history = [{"role": "user", "content": "clean user message"}]
    body = {"model": "gpt-test", "prompt_cache_key": "actual-key",
            "tools": [{"name": "original-tool"}], "extra_headers": {"x-prefix": "same"},
            "input" if mode == "codex_responses" else "messages": [
                {"role": "user", "content": "clean user message\nCURRENT RECALL"}]}
    if mode == "anthropic_messages":
        body["system"] = [{"type": "text", "text": "gateway system"}]
    else:
        body["instructions"] = "gateway system"
    frozen = FrozenRequest(body=body, fidelity="failed_wire", api_mode=mode,
                           captured_session_id=parent.session_id)
    remember_manual_request(parent, frozen, history)
    target = _source()
    target.api_mode = mode
    return parent, target, history, frozen


@pytest.mark.parametrize("mode", ["codex_responses", "chat_completions", "anthropic_messages"])
def test_manual_source_keeps_native_prefix_and_appends_only_reply(mode):
    from fork_features.request_fork.manual import reuse_manual_request

    parent, target, history, frozen = _saved_parent(mode)
    history.append({"role": "assistant", "content": "done", "api_content": "MUST NOT REPLAY"})
    before = frozen.clone_body()
    result = reuse_manual_request(parent, target, history)
    assert result is not None
    after = result.clone_body()
    key = result.input_key
    assert after[key][:-1] == before[key]
    assert "done" in str(after[key][-1]) and "MUST NOT REPLAY" not in str(after)
    assert {k: v for k, v in after.items() if k != key} == {k: v for k, v in before.items() if k != key}
    assert frozen.clone_body() == before
    assert history[0]["content"] == "clean user message"


@pytest.mark.parametrize("change", ["new_user", "tool_tail", "rewind", "edit_prefix", "reasoning_edit",
                                    "source_session", "target_session", "model", "provider", "url", "mode"])
def test_stale_or_foreign_manual_source_is_rejected(change):
    from fork_features.request_fork.manual import reuse_manual_request

    parent, target, history, _ = _saved_parent()
    if change == "new_user":
        history.append({"role": "user", "content": "another turn"})
    elif change == "tool_tail":
        history.append({"role": "tool", "content": "new tool result"})
    elif change == "rewind":
        history.clear()
    elif change == "edit_prefix":
        history[0]["content"] = "edited"
    elif change == "reasoning_edit":
        history[0]["codex_reasoning_items"] = [{"encrypted_content": "changed"}]
    else:
        obj, field = (parent, "session_id") if change == "source_session" else (target, {
            "target_session": "session_id", "model": "model", "provider": "provider",
            "url": "base_url", "mode": "api_mode"}[change])
        setattr(obj, field, "different")
    assert reuse_manual_request(parent, target, history) is None


def test_reload_metadata_does_not_invalidate_native_source():
    from fork_features.request_fork.manual import reuse_manual_request

    parent, target, history, _ = _saved_parent()
    history[0].update(_row_id=42, _db_persisted=True, timestamp=100, api_content="legacy")
    assert reuse_manual_request(parent, target, history) is not None


def test_missing_source_uses_existing_reconstruction_and_disabled_plugin_stays_off():
    from fork_features.request_fork import build_out_of_turn_compression_request_snapshot

    parent, target, history, _ = _saved_parent()
    sentinel = object()
    with (
        patch("hermes_cli.lifecycle.has_hook", return_value=True),
        patch("fork_features.request_fork.materialize_request_for_compression", return_value=sentinel) as rebuild,
    ):
        assert build_out_of_turn_compression_request_snapshot(target, history, source_agent=None) is sentinel
        assert rebuild.call_count == 1
    with patch("hermes_cli.lifecycle.has_hook", return_value=False):
        assert build_out_of_turn_compression_request_snapshot(target, history, source_agent=parent) is None


def test_new_capture_replaces_old_request_and_none_clears_it():
    from fork_features.request_fork.manual import remember_manual_request, reuse_manual_request

    parent, target, history, _ = _saved_parent()
    history.append({"role": "user", "content": "next user"})
    remember_manual_request(parent, None, [])
    assert reuse_manual_request(parent, target, history) is None


def test_real_turn_then_manual_fork_preserves_official_recall_replay(monkeypatch):
    from run_agent import AIAgent
    from fork_features.request_fork import build_out_of_turn_compression_request_snapshot

    monkeypatch.setattr("model_tools.get_tool_definitions", lambda **kw: [])
    monkeypatch.setattr("model_tools.check_toolset_requirements", dict)
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda name: name == "on_compression_start")
    agent = AIAgent(
        model="gpt-6.1-sol", provider="custom", api_mode="codex_responses",
        base_url="https://api.example.test/v1", api_key="test", quiet_mode=True,
        skip_context_files=True, skip_memory=True, max_iterations=2,
        platform="telegram", ephemeral_system_prompt="LIVE GATEWAY CONTEXT",
    )
    agent._disable_streaming = True
    agent._cleanup_task_resources = lambda task_id: None
    agent._save_trajectory = lambda *a, **kw: None
    agent._cached_system_prompt = "STABLE TEST SYSTEM"
    bodies = []
    injection = [True]
    def hook(name, **kwargs):
        if name == "pre_llm_call" and injection[0]:
            return [{"context": "CURRENT-RECALL-SENTINEL"}]
        return []

    def call(body):
        bodies.append(deepcopy(body))
        return SimpleNamespace(
            output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text="OK")])],
            usage=SimpleNamespace(input_tokens=20, output_tokens=2, total_tokens=22),
            status="completed", model="gpt-6.1-sol",
        )

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", hook)
    monkeypatch.setattr(agent, "_interruptible_api_call", call)
    first = agent.run_conversation("first question")
    assert first["completed"] is True
    assert "CURRENT-RECALL-SENTINEL" in str(bodies[-1])
    assert first["messages"][0]["content"] == "first question"
    assert "CURRENT-RECALL-SENTINEL" in first["messages"][0]["api_content"]
    assert getattr(agent, "_continuity_manual_request", None) is not None
    target = SimpleNamespace(**{key: getattr(agent, key) for key in
                             ("session_id", "api_mode", "provider", "base_url", "model", "platform")})
    target.is_subagent = False
    target._build_api_kwargs = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("unexpected reconstruction"))
    snapshot = build_out_of_turn_compression_request_snapshot(target, first["messages"], source_agent=agent)
    assert snapshot is not None and snapshot.fidelity == "reused_out_of_turn"
    fork = snapshot.clone_body()
    assert fork["instructions"] == bodies[-1]["instructions"]
    assert fork["prompt_cache_key"] == bodies[-1]["prompt_cache_key"]
    assert fork["input"][:len(bodies[-1]["input"])] == bodies[-1]["input"]
    injection[0] = False
    second = agent.run_conversation("second question", conversation_history=first["messages"])
    assert second["completed"] is True
    assert "CURRENT-RECALL-SENTINEL" in str(bodies[-1])
    assert bodies[-1]["input"][:len(bodies[0]["input"])] == bodies[0]["input"]
    agent.close()


def test_real_agent_manual_reconstruction_uses_current_preflight_api(monkeypatch):
    from run_agent import AIAgent
    from fork_features.request_fork import build_out_of_turn_compression_request_snapshot

    monkeypatch.setattr("model_tools.get_tool_definitions", lambda **kw: [])
    monkeypatch.setattr("model_tools.check_toolset_requirements", dict)
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda name: name == "on_compression_start")
    agent = AIAgent(
        model="gpt-6.1-sol", provider="custom", api_mode="codex_responses",
        base_url="https://api.example.test/v1", api_key="test", quiet_mode=True,
        skip_context_files=True, skip_memory=True, platform="telegram",
    )
    try:
        history = [{"role": "user", "content": "manual reconstruction sentinel"}]
        snapshot = build_out_of_turn_compression_request_snapshot(agent, history)
        assert snapshot is not None
        assert snapshot.fidelity == "reconstructed_out_of_turn"
        assert "manual reconstruction sentinel" in str(snapshot.clone_body()["input"])
    finally:
        agent.close()


@pytest.mark.parametrize("mode", ["codex_responses", "anthropic_messages"])
@pytest.mark.parametrize("padded", [False, True])
def test_manual_prefix_survives_real_database_roundtrip(tmp_path, mode, padded):
    from hermes_state import SessionDB
    from fork_features.request_fork import FrozenRequest
    from fork_features.request_fork.manual import remember_manual_request, reuse_manual_request

    parent, target, _, template = _saved_parent(mode)
    history = [
        {"role": "user", "content": "read the file"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call-one", "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"example.txt"}'},
        }]},
        {"role": "tool", "tool_call_id": "call-one", "content": "file contents"},
        {"role": "user", "content": "what did it say?"},
    ]
    if padded:
        for row in history:
            if row["role"] in {"user", "assistant"}:
                row["content"] = " \n" + row["content"] + "\n "
    if mode == "anthropic_messages":
        history[1]["reasoning_details"] = [{"type": "thinking", "thinking": "read first", "signature": "sig"}]
        history[1]["anthropic_content_blocks"] = [
            {"type": "thinking", "thinking": "read first", "signature": "sig"},
            {"type": "tool_use", "id": "call-one", "name": "read_file", "input": {"path": "example.txt"}},
        ]
    else:
        history[1]["codex_reasoning_items"] = [{
            "type": "reasoning", "id": "reason-one", "summary": [], "encrypted_content": "opaque",
        }]
    converted = parent._get_transport().convert_messages(
        deepcopy(history), model=parent.model, base_url=parent.base_url,
        is_codex_backend=False, is_github_responses=False,
    )
    body = template.clone_body()
    body[template.input_key] = converted[1] if mode == "anthropic_messages" else converted
    frozen = FrozenRequest(body=body, api_mode=mode, fidelity="failed_wire",
                           captured_session_id=parent.session_id)
    remember_manual_request(parent, frozen, history)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(parent.session_id, source="telegram")
        db.append_messages_batch(parent.session_id, [*deepcopy(history), {"role": "assistant", "content": "done"}])
        reloaded = db.get_messages_as_conversation(parent.session_id)
        assert len(reloaded) == len(history) + 1
        if mode == "anthropic_messages":
            assert "anthropic_content_blocks" not in reloaded[1]
        result = reuse_manual_request(parent, target, reloaded)
        assert result is not None, "ordinary DB reload must not silently fall back to the reduced-tool rebuild"
        assert result.fidelity == "reused_out_of_turn"
        after = result.clone_body()
        assert after[result.input_key][:-1] == body[result.input_key]
        assert "done" in str(after[result.input_key][-1])
        assert frozen.clone_body() == body
        tampered = deepcopy(reloaded)
        tampered[0]["content"] = "a different user instruction"
        assert reuse_manual_request(parent, target, tampered) is None
        tampered = deepcopy(reloaded)
        reasoning_key = "reasoning_details" if mode == "anthropic_messages" else "codex_reasoning_items"
        tampered[1][reasoning_key] = [{"type": "reasoning", "signature": "changed"}]
        assert reuse_manual_request(parent, target, tampered) is None
    finally:
        db.close()
