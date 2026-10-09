"""Request-local Fast opt-in; never changes the captured parent or its route."""
import copy
from types import SimpleNamespace

import pytest

from fork_features.request_fork import FrozenRequest, RequestForkService, current_request_fork_scope


def capture_fork(monkeypatch, *, mode="codex_responses", provider="openai-codex",
                 url="https://chatgpt.com/backend-api/codex", model="gpt-5.4", extra=None):
    key = "input" if mode == "codex_responses" else "messages"
    body = {"model": model, key: [{"role": "user", "content": "original prefix"}],
            "tools": [], "extra_body": {"unrelated": "keep"},
            "extra_headers": {"x-test": "keep"}, "reasoning": {"effort": "high"}}
    if extra:
        body.update(copy.deepcopy(extra))
    frozen = FrozenRequest(body=body, fidelity="prepared_parent", api_mode=mode)
    agent = SimpleNamespace(provider=provider, _api_max_retries=2, base_url=url)
    client = SimpleNamespace(base_url=url, default_headers={"anthropic-beta": "client-beta"})
    sent = []

    def send(*args, **kwargs):
        sent.append(copy.deepcopy(args[1]))
        if kwargs.get("on_physical_request"):
            kwargs["on_physical_request"](args[1])
        return SimpleNamespace(content="ok", tool_calls=[], usage={})

    monkeypatch.setattr("agent.codex_runtime.run_codex_stream", send)
    monkeypatch.setattr("fork_features.request_fork.message_protocols.send_messages", send)
    with current_request_fork_scope(agent, frozen_request=frozen, client_factory=lambda: client,
                                    client_close=lambda _: None, normalize_response=lambda x: x):
        fork = RequestForkService().capture_current()
    return fork, sent, body, frozen, agent, client


@pytest.mark.parametrize("mode,provider,url,model", [
    ("codex_responses", "openai-codex", "https://chatgpt.com/backend-api/codex", "gpt-5.4"),
    ("chat_completions", "openai", "https://api.openai.com/v1", "gpt-5.4"),
    ("chat_completions", "xai", "https://api.x.ai/v1", "grok-4.6"),
])
def test_supported_fast_is_local_to_one_call(monkeypatch, mode, provider, url, model):
    fork, sent, body, frozen, agent, _ = capture_fork(
        monkeypatch, mode=mode, provider=provider, url=url, model=model)
    original = copy.deepcopy(body)
    # Captured route, not a later parent model/provider switch, owns the request.
    agent.base_url = "https://proxy.invalid/v1"
    agent.provider = "custom"
    append = {"role": "user", "content": "checkpoint"}
    fork.call(append_message=append, request_id="fast", prefer_fast=True)
    fork.call(append_message=append, request_id="ordinary")
    assert sent[0]["service_tier"] == "priority"
    assert {k: v for k, v in sent[0].items() if k != "service_tier"} == sent[1]
    assert frozen.clone_body() == original == body
    assert "service_tier" not in sent[1]


@pytest.mark.parametrize("provider,url,model", [
    ("custom", "https://proxy.invalid/v1", "gpt-5.4"),
    ("openai-codex", "https://proxy.invalid/v1", "gpt-5.4"),
    ("openrouter", "https://openrouter.ai/api/v1", "openai/gpt-5.4"),
    ("openai", "https://api.openai.com.attacker.invalid/v1", "gpt-5.4"),
    ("openai", "", "gpt-5.4"),
    ("openai", "https://api.openai.com/v1", "unknown-model"),
    ("openai-codex", "https://chatgpt.com/backend-api/codex", "gpt-5-codex"),
])
def test_unsupported_route_or_model_keeps_original_request(monkeypatch, caplog, provider, url, model):
    fork, sent, _, _, _, _ = capture_fork(monkeypatch, provider=provider, url=url, model=model,
        extra={"extra_body": {"service_tier": "provider-custom-tier", "keep": True}})
    append = {"role": "user", "content": "checkpoint"}
    fork.call(append_message=append, request_id="fast", prefer_fast=True)
    fork.call(append_message=append, request_id="ordinary")
    assert sent[0] == sent[1]
    import json
    records = [json.loads(r.getMessage().split("request Fork service ", 1)[1])
               for r in caplog.records if r.getMessage().startswith("request Fork service ")]
    assert len(records) == 1  # ordinary calls do not add opt-in telemetry
    assert records[0]["fast_decision"] == "unsupported_or_unknown"
    assert records[0]["requested_service_tier"] == "provider-custom-tier"
    assert records[0]["response_service_tier"] is None


def test_fast_overrides_existing_sdk_extra_body_tier_only_in_fork(monkeypatch):
    fork, sent, body, frozen, _, _ = capture_fork(monkeypatch,
        extra={"extra_body": {"service_tier": "default", "keep": True}})
    fork.call(append_message={"role": "user", "content": "checkpoint"},
              request_id="fast", prefer_fast=True)
    assert sent[0]["extra_body"] == {"service_tier": "priority", "keep": True}
    assert frozen.clone_body() == body


@pytest.mark.parametrize("request_beta", [None, "request-beta"])
def test_anthropic_fast_preserves_effective_beta_headers(monkeypatch, request_beta):
    headers = {"x-test": "keep"}
    if request_beta:
        headers["Anthropic-Beta"] = request_beta
    fork, sent, body, frozen, _, _ = capture_fork(monkeypatch,
        mode="anthropic_messages", provider="anthropic", url="https://api.anthropic.com",
        model="claude-opus-4-8", extra={"extra_headers": headers})
    fork.call(append_message={"role": "user", "content": "checkpoint"},
              request_id="fast", prefer_fast=True)
    assert "speed" not in sent[0]  # native SDK accepts it in extra_body
    assert sent[0]["extra_body"] == {"unrelated": "keep", "speed": "fast"}
    import httpx
    result_headers = httpx.Headers(sent[0]["extra_headers"])
    from agent.anthropic_adapter import _FAST_MODE_BETA
    assert result_headers["anthropic-beta"].split(",") == [request_beta or "client-beta", _FAST_MODE_BETA]
    assert result_headers["x-test"] == "keep"
    assert frozen.clone_body() == body


def test_network_retry_keeps_fast_and_does_not_accumulate_checkpoint_messages(monkeypatch, caplog):
    fork, _, body, frozen, _, _ = capture_fork(monkeypatch)
    sent = []

    def send(_runtime, kwargs, **options):
        sent.append(copy.deepcopy(kwargs))
        options["on_physical_request"](kwargs)
        if len(sent) == 1:
            raise TimeoutError("offline retry test")
        return SimpleNamespace(content="ok", tool_calls=[], usage={})

    monkeypatch.setattr("agent.codex_runtime.run_codex_stream", send)
    monkeypatch.setattr("fork_features.request_fork.time.sleep", lambda _: None)
    fork.call(append_message={"role": "user", "content": "checkpoint"},
              request_id="retry", prefer_fast=True)
    assert len(sent) == 2
    assert sent[0] == sent[1]
    assert sent[1]["service_tier"] == "priority"
    assert len(sent[1]["input"]) == len(body["input"]) + 1
    assert frozen.clone_body() == body
    import json
    records = [json.loads(r.getMessage().split("request Fork service ", 1)[1])
               for r in caplog.records if r.getMessage().startswith("request Fork service ")]
    assert [r["network_attempt"] for r in records] == [1, 2]
    assert [r["result"] for r in records] == ["failed", "returned"]
    assert all(r["requested_service_tier"] == "priority" for r in records)
    assert all(r["response_service_tier"] is None for r in records)


@pytest.mark.parametrize("served", ["priority", "default", None])
def test_fast_log_separates_sent_tier_from_provider_evidence(monkeypatch, caplog, served):
    import json
    fork, _, _, _, _, _ = capture_fork(monkeypatch, mode="chat_completions",
        provider="openai", url="https://api.openai.com/v1")
    monkeypatch.setattr("fork_features.request_fork.message_protocols.send_messages",
        lambda *a, **kw: SimpleNamespace(content="ok", tool_calls=[], usage={}, service_tier=served))
    fork.call(append_message={"role": "user", "content": "PRIVATE MESSAGE"},
              request_id="continuity:log:attempt-1", prefer_fast=True)
    records = [json.loads(r.getMessage().split("request Fork service ", 1)[1])
               for r in caplog.records if r.getMessage().startswith("request Fork service ")]
    assert len(records) == 1
    assert records[0] == {
        "request_id": "continuity:log:attempt-1", "network_attempt": 1,
        "result": "returned", "fast_decision": "enabled", "request_observed": True,
        "requested_service_tier": "priority", "requested_speed": None,
        "response_service_tier": served, "response_speed": None,
    }
    assert "PRIVATE MESSAGE" not in caplog.text
