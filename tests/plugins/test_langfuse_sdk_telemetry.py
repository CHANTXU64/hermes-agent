"""Local integration: real OpenAI SDK -> Hermes stream/hooks -> real Langfuse SDK.

HTTP uses deterministic test fixtures; OTEL exports only to memory, never a live
model or Langfuse project. Run in an environment with the optional langfuse SDK.
"""
import json
import uuid

import httpx
import pytest
from openai import OpenAI

from tests.agent.test_first_chunk_at_hook import agent


@pytest.mark.parametrize("streaming", [True, False])
@pytest.mark.parametrize("capture", ["metadata", "sanitized", "full"])
@pytest.mark.parametrize("wire_tier", ["priority", "default"])
def test_sdk_export_contains_fast_and_native_timing(agent, monkeypatch, capture, streaming, wire_tier):
    from hermes_cli import plugins
    from hermes_cli.config import atomic_config_write
    from hermes_constants import get_hermes_home

    sdk = pytest.importorskip("langfuse")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    home = get_hermes_home()
    atomic_config_write(home / "config.yaml", {"plugins": {"enabled": ["observability/langfuse"]}})
    monkeypatch.setenv("HERMES_LANGFUSE_CAPTURE", capture)
    manager = plugins.PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins["observability/langfuse"]
    assert loaded.enabled and loaded.module is not None, loaded.error
    monkeypatch.setattr(plugins, "_plugin_manager", manager)
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    client = sdk.Langfuse(public_key="pk-lf-test-" + uuid.uuid4().hex,
        secret_key="sk-lf-test", base_url="http://127.0.0.1:1", environment="test",
        tracer_provider=tracer_provider, span_exporter=exporter)
    monkeypatch.setattr(loaded.module, "_get_langfuse", lambda: client)
    captured_requests = []
    response_usage = {"prompt_tokens": 100, "completion_tokens": 8, "total_tokens": 108}

    def handle(request):
        body = json.loads(request.content)
        captured_requests.append(body)
        if not body.get("stream"):
            return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 1,
                "model": "fixture", "service_tier": "default", "usage": response_usage,
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}]})
        chunks = [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "service_tier": "default"},
            {"choices": [], "usage": response_usage},
        ]
        data = "".join("data: " + json.dumps({"id": "fixture", "object": "chat.completion.chunk",
            "created": 1, "model": "fixture", **chunk}) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)

    http_client = httpx.Client(transport=httpx.MockTransport(handle))
    openai = OpenAI(api_key="test-only", base_url="http://127.0.0.1:1/v1", http_client=http_client, max_retries=0)
    agent.client = openai
    agent.model, agent.provider, agent.api_mode = "fixture", "openai", "chat_completions"
    agent._disable_streaming = not streaming
    # Exercise the actual request rather than interpreting the agent's static mode.
    agent.request_overrides = {"service_tier": "priority"}
    # Execution middleware runs after pre_api_request; observe what it actually sends.
    monkeypatch.setattr("hermes_cli.middleware.run_llm_execution_middleware",
        lambda request, next_call, **kw: next_call({**request, "service_tier": wire_tier}))
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **_: openai)
    monkeypatch.setattr(agent, "_close_request_openai_client", lambda *a, **kw: None)
    monkeypatch.setattr(agent, "_persist_session", lambda *a, **kw: None)
    monkeypatch.setattr(agent, "_save_trajectory", lambda *a, **kw: None)
    monkeypatch.setattr(agent, "_cleanup_task_resources", lambda *a, **kw: None)
    try:
        result = agent.run_conversation("local telemetry fixture")
        client.flush()
        assert result["final_response"] == "done"
        assert captured_requests and captured_requests[0]["service_tier"] == wire_tier
        generations = [s for s in exporter.get_finished_spans() if s.attributes.get("langfuse.observation.type") == "generation"]
        assert len(generations) == 1
        generation = generations[0]
        attrs = dict(generation.attributes)
        assert json.loads(attrs["langfuse.observation.model.parameters"])["service_tier"] == wire_tier
        assert attrs["langfuse.observation.metadata.fast_requested"] is (wire_tier == "priority")
        assert attrs["langfuse.observation.metadata.response_service_tier"] == "default"
        assert attrs["langfuse.observation.metadata.fast_confirmed"] is False
        usage = json.loads(attrs["langfuse.observation.usage_details"])
        assert usage["output"] == 8
        assert generation.end_time > generation.start_time
        if streaming:
            assert attrs["langfuse.observation.completion_start_time"]
        else:
            assert "langfuse.observation.completion_start_time" not in attrs
        assert not any("input_tokens_per_second" in k for k in attrs)
    finally:
        client.shutdown()
        tracer_provider.shutdown()
        openai.close()
