"""Generic exact-current-request Fork service contracts."""

from __future__ import annotations

import copy
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from fork_features.request_fork import (
    FrozenCodexRequest,
    RequestForkService,
    build_out_of_turn_compression_request_snapshot,
    rematerialize_codex_request_after_adopt,
    compression_request_fork_enabled,
    current_request_fork_scope,
    freeze_codex_request_for_compression,
)


class _Normalized:
    content = '{"schema_version": 1}'
    tool_calls = []
    usage = {"input_tokens": 100, "cached_input_tokens": 80}


class _Transport:
    def normalize_response(self, response):
        assert response == "RAW RESPONSE"
        return _Normalized()


class _Client:
    def __init__(self, name):
        self.name = name
        self.closed = False


class _Agent:
    api_mode = "codex_responses"
    provider = "test-provider"
    session_id = "same-session"
    _api_max_retries = 3
    stream_delta_callback = object()
    reasoning_callback = object()
    interim_assistant_callback = object()
    event_callback = object()
    _stream_callback = object()
    _stream_writer_generation = 9

    def __init__(self):
        self.sent = []
        self.client = _Client("parent")
        self._client_kwargs = {"api_key": "test-key"}
        self.created_clients = []

    def _create_openai_client(self, _kwargs, *, reason, shared):
        assert reason == "request_fork"
        assert shared is False
        client = _Client("fork")
        self.created_clients.append(client)
        return client

    def _close_openai_client(self, client, *, reason, shared):
        assert reason == "request_fork"
        assert shared is False
        client.closed = True

    def _build_api_kwargs(self, messages, tools_for_api=None):
        self.sent.append((copy.deepcopy(messages), copy.deepcopy(tools_for_api)))
        return {
            "model": "gpt-test",
            "input": copy.deepcopy(messages),
            "tools": copy.deepcopy(tools_for_api),
            "prompt_cache_key": "session-cache-key",
        }

    def _run_codex_stream(self, kwargs, client=None):
        assert self.stream_delta_callback is None
        assert self.reasoning_callback is None
        assert self.interim_assistant_callback is None
        assert self.event_callback is None
        assert self._stream_callback is None
        self.sent.append(copy.deepcopy(kwargs))
        return "RAW RESPONSE"

    def _get_transport(self):
        return _Transport()


def _fake_scope(agent, frozen):
    return current_request_fork_scope(
        agent,
        frozen_request=frozen,
        client_factory=lambda: agent._create_openai_client(
            copy.deepcopy(agent._client_kwargs),
            reason="request_fork",
            shared=False,
        ),
        client_close=lambda client: agent._close_openai_client(
            client,
            reason="request_fork",
            shared=False,
        ),
        normalize_response=agent._get_transport().normalize_response,
    )


def test_freeze_accepts_sdk_transform_bypassed_physical_request(monkeypatch):
    from agent.sdk_transform_bypass import bypass_sdk_request_transform as _bypass_sdk_request_transform

    monkeypatch.setattr(
        "fork_features.request_fork.compression_request_fork_enabled",
        lambda _agent: True,
    )
    physical = _bypass_sdk_request_transform(
        {
            "model": "gpt-test",
            "input": [{"role": "user", "content": "FULL PREFIX"}],
            "tools": [{"name": "terminal"}],
            "extra_body": {"trace_marker": "keep"},
            "stream": True,
        }
    )
    assert "input" not in physical
    assert isinstance(physical["extra_body"]["input"], list)

    frozen = freeze_codex_request_for_compression(
        SimpleNamespace(session_id="physical-session"),
        physical,
        fidelity="failed_wire",
    )
    assert frozen is not None

    body = frozen.clone_body()
    assert body["input"] == [{"role": "user", "content": "FULL PREFIX"}]
    assert body["tools"] == [{"name": "terminal"}]
    assert body["extra_body"] == {"trace_marker": "keep"}

    body["input"].append({"role": "user", "content": "CHECKPOINT"})
    resend = _bypass_sdk_request_transform(body)
    assert resend["extra_body"]["input"][-1]["content"] == "CHECKPOINT"


def test_frozen_responses_request_reaches_transport_without_second_conversion(
    monkeypatch,
):
    import agent.codex_runtime as codex_runtime

    agent = _Agent()
    body = {
        "model": "gpt-test",
        "input": [
            {"role": "user", "content": "RUN THE TOOL"},
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "read_file",
                "arguments": '{"path":"/tmp/input"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "VERIFIED TOOL RESULT",
            },
        ],
        "tools": [
            {
                "type": "function",
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object"},
            }
        ],
        "prompt_cache_key": "stable-cache-key",
        "extra_headers": {
            "session_id": "physical-session-before-rotation",
            "thread-id": "stable-cache-key",
            "x-client-request-id": "stable-cache-key",
        },
    }
    original_body = copy.deepcopy(body)
    captured = {}

    def _capture_transport(runtime, api_kwargs, client=None, on_first_delta=None):
        captured["runtime"] = runtime
        captured["api_kwargs"] = copy.deepcopy(api_kwargs)
        captured["client"] = client
        captured["on_first_delta"] = on_first_delta
        return "RAW RESPONSE"

    monkeypatch.setattr(codex_runtime, "run_codex_stream", _capture_transport)
    frozen = FrozenCodexRequest(
        body=body,
        fidelity="failed_wire",
        captured_session_id="physical-session-before-rotation",
    )
    with _fake_scope(agent, frozen):
        request_fork = RequestForkService().capture_current()

    body["input"][0]["content"] = "MUTATED PARENT"
    body["tools"][0]["name"] = "mutated_tool"
    result = request_fork.call(
        append_message={"role": "user", "content": "RETURN JSON ONLY"},
        request_id="continuity:cp-1:attempt-1",
    )

    assert result.raw_output == '{"schema_version": 1}'
    assert result.usage["cached_input_tokens"] == 80
    assert captured["api_kwargs"] == {
        **original_body,
        "input": [
        *original_body["input"],
        {"role": "user", "content": "RETURN JSON ONLY"},
        ],
    }
    assert captured["client"] is agent.created_clients[0]
    assert captured["client"] is not agent.client
    assert captured["on_first_delta"] is None
    assert agent.stream_delta_callback is not None


def test_request_fork_logs_prompt_cache_usage(monkeypatch, caplog):
    import agent.codex_runtime as codex_runtime

    agent = _Agent()
    monkeypatch.setattr(
        _Normalized,
        "usage",
        {
            "input_tokens": 225_367,
            "output_tokens": 2_212,
            "input_tokens_details": {"cached_tokens": 224_768},
        },
    )
    monkeypatch.setattr(
        codex_runtime,
        "run_codex_stream",
        lambda runtime, api_kwargs, client=None: "RAW RESPONSE",
    )
    frozen = FrozenCodexRequest(
        body={
            "model": "gpt-test",
            "input": [{"role": "user", "content": "FULL PREFIX"}],
            "tools": [],
        },
        fidelity="prepared_parent",
        captured_session_id="same-session",
    )
    with _fake_scope(agent, frozen):
        request_fork = RequestForkService().capture_current()

    caplog.set_level(logging.WARNING, logger="fork_features.request_fork")
    request_fork.call(
        append_message={"role": "user", "content": "RETURN JSON"},
        request_id="continuity:cp-cache:attempt-1",
    )

    assert (
        "request Fork usage request_id=continuity:cp-cache:attempt-1 "
        "prompt_tokens=225367 uncached_input_tokens=599 "
        "cache_read_tokens=224768 cache_write_tokens=0 "
        "cache_hit_rate=99.73%"
    ) in caplog.text
    assert caplog.records[-1].levelno == logging.WARNING


def test_request_fork_uses_host_api_attempt_limit_for_transient_connection_errors(
    monkeypatch,
):
    import agent.codex_runtime as codex_runtime
    agent = _Agent()
    agent._api_max_retries = 4
    transport_attempts = []

    def _fail_then_succeed(runtime, api_kwargs, client=None, on_first_delta=None):
        del runtime, api_kwargs, on_first_delta
        transport_attempts.append(client)
        if len(transport_attempts) < 4:
            raise ConnectionError("temporary Codex connection failure")
        return "RAW RESPONSE"

    monkeypatch.setattr(codex_runtime, "run_codex_stream", _fail_then_succeed)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    frozen = FrozenCodexRequest(
        body={
            "model": "gpt-test",
            "input": [{"role": "user", "content": "FULL PREFIX"}],
            "tools": [],
        },
        fidelity="prepared_parent",
        captured_session_id="same-session",
    )

    with _fake_scope(agent, frozen):
        request_fork = RequestForkService().capture_current()

    result = request_fork.call(
        append_message={"role": "user", "content": "RETURN JSON"},
        request_id="continuity:cp-retry:attempt-1",
    )

    assert result.raw_output == '{"schema_version": 1}'
    assert len(transport_attempts) == agent._api_max_retries
    assert len({id(client) for client in transport_attempts}) == 4
    assert all(client.closed for client in transport_attempts)


def test_capture_current_is_available_only_inside_host_lifecycle_scope():
    with pytest.raises(RuntimeError, match="on_compression_start"):
        RequestForkService().capture_current()


def test_request_fork_snapshot_is_enabled_only_for_verified_codex_path(monkeypatch):
    import hermes_cli.lifecycle as lifecycle

    monkeypatch.setattr(
        lifecycle,
        "has_hook",
        lambda name: name == "on_compression_start",
    )
    agent = SimpleNamespace(
        is_subagent=False,
        platform="telegram",
        api_mode="codex_responses",
    )

    assert compression_request_fork_enabled(agent) is True
    agent.api_mode = "chat_completions"
    assert compression_request_fork_enabled(agent) is False
    agent.api_mode = "anthropic_messages"
    assert compression_request_fork_enabled(agent) is False


def test_in_flight_fork_owns_client_independent_of_parent_close(monkeypatch):
    import agent.codex_runtime as codex_runtime

    started = threading.Event()
    release = threading.Event()

    class _BlockingAgent(_Agent):
        def close(self):
            self.client.closed = True

    def _blocking_transport(runtime, api_kwargs, client=None, on_first_delta=None):
        del runtime, api_kwargs, on_first_delta
        assert client is not None
        started.set()
        assert release.wait(2)
        if client.closed:
            raise RuntimeError("Fork client was closed by parent teardown")
        return "RAW RESPONSE"

    monkeypatch.setattr(codex_runtime, "run_codex_stream", _blocking_transport)
    agent = _BlockingAgent()
    frozen = FrozenCodexRequest(
        body={
            "model": "gpt-test",
            "input": [{"role": "user", "content": "FULL PREFIX"}],
            "tools": [],
        },
        fidelity="prepared_parent",
        captured_session_id="same-session",
    )
    with _fake_scope(agent, frozen):
        request_fork = RequestForkService().capture_current()

    result_box = {}
    worker = threading.Thread(
        target=lambda: result_box.setdefault(
            "result",
            request_fork.call(
                append_message={"role": "user", "content": "RETURN JSON"},
                request_id="continuity:cp-close:attempt-1",
            ),
        )
    )
    worker.start()
    assert started.wait(1)
    agent.close()
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert result_box["result"].raw_output == '{"schema_version": 1}'
    assert agent.client.closed is True
    assert len(agent.created_clients) == 1
    assert agent.created_clients[0].closed is True


def test_request_fork_callbacks_do_not_retain_or_reread_parent_agent(monkeypatch):
    import agent.codex_runtime as codex_runtime

    agent = _Agent()
    agent.provider = "captured-provider"
    observed = {}

    def _create(owner, _kwargs, *, reason, shared):
        del reason, shared
        observed["provider"] = owner.provider
        return _Client("fork")

    monkeypatch.setattr(
        "agent.agent_runtime_helpers.create_openai_client",
        _create,
    )

    def _capture(runtime, api_kwargs, client=None, on_first_delta=None):
        del api_kwargs, client, on_first_delta
        observed["runtime_provider"] = runtime.provider
        return "RAW RESPONSE"

    monkeypatch.setattr(
        codex_runtime,
        "run_codex_stream",
        _capture,
    )
    frozen = FrozenCodexRequest(
        body={
            "model": "gpt-test",
            "input": [{"role": "user", "content": "FULL PREFIX"}],
            "tools": [],
        },
        fidelity="prepared_parent",
        captured_session_id="same-session",
    )
    with current_request_fork_scope(agent, frozen_request=frozen):
        request_fork = RequestForkService().capture_current()

    template = request_fork._template
    assert all(
        getattr(callback, "__self__", None) is not agent
        for callback in (
            template.client_factory,
            template.client_close,
            template.normalize_response,
            template.abort_client,
        )
        if callback is not None
    )
    agent.provider = "mutated-provider"
    request_fork.call(
        append_message={"role": "user", "content": "RETURN JSON"},
        request_id="continuity:immutable-owner:attempt-1",
    )

    assert observed["provider"] == "captured-provider"
    assert observed["runtime_provider"] == "captured-provider"


def test_plugin_context_exposes_request_fork_service():
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

    context = PluginContext(PluginManifest(name="request-fork-test"), PluginManager())

    assert isinstance(context.request_fork, RequestForkService)
    assert context.request_fork is context.request_fork


def test_out_of_turn_snapshot_uses_full_history_cached_system_and_final_tools(
    monkeypatch,
):
    import copy

    import fork_features.request_fork as request_fork
    import hermes_cli.lifecycle as lifecycle

    monkeypatch.setattr(
        lifecycle,
        "has_hook",
        lambda name: name == "on_compression_start",
    )
    history = [
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "recent tail"},
    ]
    tools = [{"type": "function", "function": {"name": "continuity_save"}}]
    build_calls = []

    def _build_api_kwargs(messages, *, tools_for_api):
        build_calls.append((copy.deepcopy(messages), copy.deepcopy(tools_for_api)))
        return {
            "model": "test-model",
            "instructions": messages[0]["content"],
            "input": copy.deepcopy(messages[1:]),
            "tools": [
                {
                    "type": "function",
                    "name": tools_for_api[0]["function"]["name"],
                    "parameters": {},
                }
            ],
            "prompt_cache_key": "stable-root",
        }

    class _Transport:
        def preflight_kwargs(self, body, **_kwargs):
            prepared = copy.deepcopy(body)
            prepared["tools"][0]["strict"] = False
            prepared["extra_headers"] = {
                "session-id": "physical-session",
                "thread-id": "stable-root",
                "x-client-request-id": "stable-root",
            }
            return prepared

    agent = SimpleNamespace(
        api_mode="codex_responses",
        platform="cli",
        is_subagent=False,
        session_id="physical-session",
        _cached_system_prompt="CACHED SYSTEM",
        _force_ascii_payload=False,
        tools=tools,
        _build_api_kwargs=_build_api_kwargs,
        _get_transport=lambda: _Transport(),
        _is_copilot_url=lambda: False,
        _is_codex_backend=lambda: True,
    )

    snapshot = request_fork.build_out_of_turn_compression_request_snapshot(
        agent,
        history,
    )
    history[-1]["content"] = "MUTATED LIVE TAIL"
    tools[0]["function"]["name"] = "mutated_tool"

    assert snapshot is not None
    assert snapshot.fidelity == "reconstructed_out_of_turn"
    assert len(build_calls) == 1
    body = snapshot.clone_body()
    assert body["instructions"] == "CACHED SYSTEM"
    assert body["input"] == [
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "recent tail"},
    ]
    assert body["tools"] == [
        {
            "type": "function",
            "name": "continuity_save",
            "parameters": {},
            "strict": False,
        }
    ]
    assert body["extra_headers"] == {
        "session-id": "physical-session",
        "thread-id": "stable-root",
        "x-client-request-id": "stable-root",
    }


def test_adopt_rematerialization_splices_only_new_rows_into_prepared_body():
    prepared = FrozenCodexRequest(
        body={
            "model": "gpt-test",
            "instructions": "STABLE SYSTEM",
            "input": [
                {"role": "user", "content": "persisted question"},
                {"role": "assistant", "content": "persisted answer"},
                {
                    "role": "user",
                    "content": "LIVE USER + REQUEST-ONLY SENTINEL",
                },
                {
                    "role": "developer",
                    "content": "PLUGIN + MEMORY REQUEST CONTEXT",
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "read_file",
                    "strict": False,
                }
            ],
            "prompt_cache_key": "stable-cache",
            "extra_headers": {
                "thread-id": "stable-cache",
                "x-initiator": "user",
            },
        },
        fidelity="prepared_parent",
        captured_session_id="session-1",
    )
    original = [
        {"role": "user", "content": "persisted question", "_row_id": 1},
        {"role": "assistant", "content": "persisted answer", "_row_id": 2},
        {"role": "user", "content": "LIVE USER"},
    ]
    adopted = [
        {"role": "user", "content": "persisted question", "_row_id": 1},
        {"role": "assistant", "content": "persisted answer", "_row_id": 2},
        {"role": "assistant", "content": "concurrent row 1", "_row_id": 3},
        {"role": "assistant", "content": "concurrent row 2", "_row_id": 4},
        {"role": "user", "content": "LIVE USER", "_row_id": 5},
    ]
    converter_calls = []

    def _convert(rows):
        converter_calls.append(copy.deepcopy(rows))
        return copy.deepcopy(rows)

    rematerialized = rematerialize_codex_request_after_adopt(
        prepared,
        original_messages=original,
        adopted_messages=adopted,
        current_user_input=[
            {
                "role": "user",
                "content": "LIVE USER + REQUEST-ONLY SENTINEL",
            }
        ],
        convert_added_messages=_convert,
    )

    assert rematerialized is not None
    assert rematerialized.fidelity == "rematerialized_after_adopt"
    body = rematerialized.clone_body()
    assert body["input"] == [
        {"role": "user", "content": "persisted question"},
        {"role": "assistant", "content": "persisted answer"},
        {"role": "assistant", "content": "concurrent row 1"},
        {"role": "assistant", "content": "concurrent row 2"},
        {
            "role": "user",
            "content": "LIVE USER + REQUEST-ONLY SENTINEL",
        },
        {
            "role": "developer",
            "content": "PLUGIN + MEMORY REQUEST CONTEXT",
        },
    ]
    assert body["tools"] == prepared.clone_body()["tools"]
    assert body["extra_headers"] == prepared.clone_body()["extra_headers"]
    assert converter_calls == [[
        {"role": "assistant", "content": "concurrent row 1"},
        {"role": "assistant", "content": "concurrent row 2"},
    ]]


def test_out_of_turn_multimodal_snapshot_fails_closed_without_building(monkeypatch):
    import hermes_cli.lifecycle as lifecycle

    monkeypatch.setattr(
        lifecycle,
        "has_hook",
        lambda name: name == "on_compression_start",
    )
    build_calls = []
    agent = SimpleNamespace(
        api_mode="codex_responses",
        platform="telegram",
        is_subagent=False,
        session_id="session-1",
        _cached_system_prompt="SYSTEM",
        tools=[],
        _build_api_kwargs=lambda *_args, **_kwargs: build_calls.append(True),
    )

    snapshot = build_out_of_turn_compression_request_snapshot(
        agent,
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "inspect"},
                    {"type": "image_url", "image_url": {"url": "file:///tmp/x.png"}},
                ],
            }
        ],
    )

    assert snapshot is None
    assert build_calls == []
