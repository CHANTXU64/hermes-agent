"""Per-request Fast evidence and native Langfuse generation timing."""
from types import SimpleNamespace

import pytest

from agent.api_request_hooks import ApiRequestHooksMixin
from plugins.observability import langfuse as plugin


class Span:
    def __init__(self, **kwargs):
        self.data = kwargs
        self.children = []
        self.ended = None

    def update(self, **kwargs):
        self.data.setdefault("metadata", {}).update(kwargs.pop("metadata", {}))
        self.data.update(kwargs)

    update_trace = update

    def start_observation(self, **kwargs):
        span = Span(**kwargs)
        self.children.append(span)
        return span

    def end(self, **kwargs):
        self.ended = kwargs


@pytest.fixture
def tracing(monkeypatch):
    root = Span()
    client = SimpleNamespace(flush=lambda: None)
    monkeypatch.setattr(plugin, "_get_langfuse", lambda: client)
    monkeypatch.setattr(plugin, "_TRACE_STATE", {})
    monkeypatch.setattr(plugin, "_start_root_trace", lambda *a, **kw: plugin.TraceState("trace", None, root))
    return root


@pytest.mark.parametrize("body,expected,fast", [
    ({"service_tier": "priority"}, {"service_tier": "priority"}, True),
    ({"service_tier": "auto"}, {"service_tier": "auto"}, False),
    ({"service_tier": "default"}, {"service_tier": "default"}, False),
    ({"extra_body": {"speed": "fast"}}, {"speed": "fast"}, True),
    ({"service_tier": "priority", "extra_body": {"service_tier": "default"}}, {"service_tier": "default"}, False),
    ({}, {}, False),
])
def test_generation_records_wire_request_parameters(tracing, body, expected, fast):
    plugin.on_pre_llm_request(task_id="request", request={"body": body})
    gen = tracing.children[-1]
    assert gen.data["model_parameters"] == {"provider": "", "api_mode": "", **expected}
    assert gen.data["metadata"]["fast_requested"] is fast
    assert gen.data["metadata"]["fast_confirmed"] is None


def test_missing_request_is_unknown_not_disabled(tracing):
    plugin.on_pre_llm_request(task_id="request", request={"_truncated": True})
    assert tracing.children[-1].data["metadata"]["fast_requested"] is None


@pytest.mark.parametrize("tier,confirmed", [("priority", True), ("default", False), (None, None)])
def test_response_tier_never_inherits_request(tracing, tier, confirmed):
    agent = ApiRequestHooksMixin()
    agent.provider, agent.api_mode = "openai", "chat_completions"
    message = SimpleNamespace(role="assistant", content="done", tool_calls=[])
    response = SimpleNamespace(model="fixture", usage=None, service_tier=tier)
    payload = agent._api_response_payload_for_hook(response, message, finish_reason="stop")
    plugin.on_pre_llm_request(task_id="request", request={"body": {"service_tier": "priority"}})
    plugin.on_post_llm_call(task_id="request", response=payload, assistant_message=message)
    gen = tracing.children[-1]
    assert gen.data["metadata"]["response_service_tier"] == tier
    assert gen.data["metadata"]["fast_confirmed"] is confirmed
    assert gen.data["metadata"]["fast_requested"] is True


def test_service_evidence_survives_large_payload_hooks(tracing, monkeypatch):
    from agent.turn_api_request import _fire_pre_api_request_hook
    from agent.turn_response_intake import _fire_post_api_request_hook
    from hermes_cli import lifecycle

    class Agent(ApiRequestHooksMixin):
        session_id = "large"
        platform = "test"
        model = "fixture"
        provider = "openai"
        base_url = "http://localhost"
        api_mode = "chat_completions"
        max_tokens = 100
        tools = []

    monkeypatch.setattr(lifecycle, "has_hook", lambda _: True)
    callbacks = {"pre_api_request": plugin.on_pre_llm_request, "post_api_request": plugin.on_post_llm_call}
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda name, **kw: callbacks[name](**kw))
    monkeypatch.setenv("HERMES_PLUGIN_PAYLOAD_MAX_CHARS", "1000")
    # Even the reduced sanitizer exceeds its cap. Metadata must not depend on that body.
    history = [{"role": "user", "content": "x" * 2000} for _ in range(55)]
    body = {"messages": history, "service_tier": "priority"}
    agent = Agent()
    assert agent._api_request_payload_for_hook(body).get("_truncated")
    _fire_pre_api_request_hook(agent, body, history, [], messages=history,
        original_user_message="hi", approx_tokens=100, total_chars=1000, retry_count=0,
        api_call_count=1, api_request_id="request", api_start_time=100.0, effective_task_id="large", turn_id="turn")
    message = SimpleNamespace(role="assistant", content="y" * 2000, tool_calls=[])
    response = SimpleNamespace(model="fixture", usage=None, service_tier="default")
    _fire_post_api_request_hook(agent, response, message, "stop", api_messages=history,
        api_call_count=1, api_duration=5.0, api_start_time=100.0, api_request_id="request",
        effective_task_id="large", turn_id="turn")
    gen = tracing.children[-1]
    assert gen.data["model_parameters"]["service_tier"] == "priority"
    assert gen.data["metadata"]["response_service_tier"] == "default"
    assert gen.data["metadata"]["fast_confirmed"] is False


def test_retry_uses_own_parameters(tracing):
    plugin.on_pre_llm_request(task_id="retry", api_call_count=1, request={"body": {"speed": "fast"}})
    plugin.on_api_request_error(task_id="retry", api_call_count=1, retryable=True)
    plugin.on_pre_llm_request(task_id="retry", api_call_count=1, request={"body": {}})
    first, second = tracing.children
    assert first.data["metadata"]["fast_requested"] is True
    assert first.ended is not None
    assert second.data["metadata"]["fast_requested"] is False
    assert second.data["metadata"]["fast_confirmed"] is None


def test_anthropic_usage_speed_is_response_evidence(tracing):
    plugin.on_pre_llm_request(task_id="anthropic", request={"body": {"extra_body": {"speed": "fast"}}})
    plugin.on_post_llm_call(task_id="anthropic", response={"usage": {"speed": "standard"}}, assistant_content_chars=4)
    assert tracing.children[-1].data["metadata"]["response_speed"] == "standard"
    assert tracing.children[-1].data["metadata"]["fast_confirmed"] is False


@pytest.mark.parametrize("first_token", [102.0, None, 99.0, 111.0, float("nan")])
def test_native_completion_timing_uses_content_not_first_event(tracing, first_token):
    from datetime import datetime, timezone

    plugin.on_pre_llm_request(task_id="timed", request={"body": {}}, started_at=100.0)
    plugin.on_post_llm_call(task_id="timed", assistant_content_chars=5,
        started_at=100.0, ended_at=110.0, first_chunk_at=101.0, first_token_at=first_token,
        usage={"input_tokens": 100, "output_tokens": 80})
    gen = tracing.children[-1]
    assert gen.ended == {"end_time": 110_000_000_000}
    if first_token == 102.0:
        assert gen.data["completion_start_time"] == datetime.fromtimestamp(first_token, timezone.utc)
    else:
        assert "completion_start_time" not in gen.data
    assert gen.data["usage_details"]["output"] == 80
    assert "input_tokens_per_second" not in gen.data["metadata"]


def test_error_hook_records_last_physical_request_after_rewrite(tracing, monkeypatch):
    from hermes_cli import lifecycle

    class Agent(ApiRequestHooksMixin):
        session_id = ""
        platform = "test"
        model = "fixture"
        provider = "openai"
        base_url = "http://localhost"
        api_mode = "chat_completions"
        _last_api_request_parameters = {"service_tier": "default"}

    monkeypatch.setattr(lifecycle, "has_hook", lambda _: True)
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda name, **kw: plugin.on_api_request_error(**kw))
    plugin.on_pre_llm_request(task_id="error", request={"body": {"service_tier": "priority"}})
    Agent()._invoke_api_request_error_hook(task_id="error", turn_id="", api_request_id="", api_call_count=0,
        api_start_time=100.0, api_kwargs={"service_tier": "priority"}, error_type="TestError", error_message="fixture")
    gen = tracing.children[-1]
    assert gen.data["model_parameters"]["service_tier"] == "default"
    assert gen.data["metadata"]["fast_requested"] is False
    assert gen.data["metadata"]["fast_confirmed"] is None
    assert gen.ended is not None


def test_anthropic_priority_tier_does_not_confirm_fast_speed(tracing):
    plugin.on_pre_llm_request(task_id="anthropic-tier", api_mode="anthropic_messages",
        request={"body": {"service_tier": "priority"}})
    plugin.on_post_llm_call(task_id="anthropic-tier", api_mode="anthropic_messages",
        response={"service_tier": "priority"}, assistant_content_chars=4)
    metadata = tracing.children[-1].data["metadata"]
    assert metadata["fast_requested"] is False
    assert metadata["response_service_tier"] == "priority"
    assert metadata["fast_confirmed"] is None  # Anthropic Fast is usage.speed, not priority capacity.
