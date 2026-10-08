"""Stream service-level evidence and TTFT are independent of liveness/TTFB."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.codex_runtime import run_codex_stream
from tests.agent.test_codex_first_event_timing import _agent
from tests.agent.test_first_chunk_at_hook import agent, _make_stream_chunk, _run_with_hooks


@pytest.mark.parametrize("delta", [
    {"type": "response.output_text.delta", "delta": "Yes."},
    {"type": "response.reasoning_summary_text.delta", "delta": "Think"},
    {"type": "response.function_call_arguments.delta", "delta": "{}"},
])
def test_codex_ttft_ignores_lifecycle_and_preserves_response_tier(monkeypatch, delta):
    clock = [100.0]
    monkeypatch.setattr("agent.codex_runtime.time.time", lambda: clock[0])
    a = _agent()

    def events():
        yield {"type": "response.created", "response": {"id": "fixture", "status": "in_progress"}}
        clock[0] = 102.0
        yield delta
        clock[0] = 110.0
        yield {"type": "response.completed", "response": {"id": "fixture", "status": "completed", "service_tier": "priority"}}

    response = run_codex_stream(a, {"model": "fixture"}, client=SimpleNamespace(responses=SimpleNamespace(create=lambda **_: events())))
    assert a._last_api_first_chunk_at == 100.0
    assert a._last_api_first_token_at == 102.0
    assert response.service_tier == "priority"


def test_chat_stream_preserves_tier_and_first_content(monkeypatch, agent):
    agent.api_mode = "chat_completions"
    agent._last_api_first_chunk_at = agent._last_api_first_token_at = None
    client = MagicMock()
    chunks = [
        _make_stream_chunk(),  # role-only/liveness, not token generation
        _make_stream_chunk(content="hello"),
        _make_stream_chunk(finish_reason="stop"),
    ]
    chunks[-1].service_tier = "priority"
    client.chat.completions.create.return_value = iter(chunks)
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **_: client)
    monkeypatch.setattr(agent, "_close_request_openai_client", lambda *a, **kw: None)
    response = agent._interruptible_streaming_api_call({})
    assert response.service_tier == "priority"
    assert agent._last_api_first_token_at > agent._last_api_first_chunk_at


def test_nonstream_call_resets_previous_token_time_and_exposes_hook(agent):
    from tests.agent.test_run_agent import _mock_response

    agent._last_api_first_token_at = 1.0
    agent.client.chat.completions.create.return_value = _mock_response(content="done")
    _, posts = _run_with_hooks(agent)
    assert len(posts) == 1
    assert posts[0]["first_token_at"] is None


def test_codex_reconnect_does_not_reuse_failed_attempt_token_time(monkeypatch):
    import httpx

    clock = [100.0]
    monkeypatch.setattr("agent.codex_runtime.time.time", lambda: clock[0])
    a = _agent()
    attempts = []

    def create(**_):
        attempts.append(1)
        if len(attempts) == 1:
            def failed():
                yield {"type": "response.output_text.delta", "delta": "old"}
                raise httpx.ReadError("stream broke")
            return failed()
        clock[0] = 200.0
        return iter([{"type": "response.completed", "response": {"status": "completed"}}])

    run_codex_stream(a, {"model": "fixture"}, client=SimpleNamespace(responses=SimpleNamespace(create=create)))
    assert len(attempts) == 2
    assert a._last_api_first_token_at is None


@pytest.mark.parametrize("chunk,expected", [
    ({"type": "message_start"}, False),
    ({"type": "ping"}, False),
    ({"type": "content_block_delta", "delta": {"text": ""}}, False),
    ({"type": "content_block_delta", "delta": {"text": "hello"}}, True),
    ({"type": "content_block_delta", "delta": {"thinking": "think"}}, True),
    ({"type": "content_block_delta", "delta": {"partial_json": "{}"}}, True),
    ({"type": "content_block_start", "content_block": {"type": "tool_use", "name": "read_file"}}, True),
    ({"choices": [{"delta": {"role": "assistant"}}]}, False),
    ({"choices": [{"delta": {"refusal": "cannot"}}]}, True),
    ({"choices": [{"delta": {"tool_calls": [{"function": {"arguments": "{}"}}]}}]}, True),
])
def test_content_detection_excludes_protocol_only_events(chunk, expected):
    from agent.request_telemetry import stream_chunk_has_token

    assert stream_chunk_has_token(chunk) is expected
