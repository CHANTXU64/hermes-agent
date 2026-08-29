"""Diagnostics for Codex Responses transport failures."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError

from agent.codex_runtime import _codex_request_failure_details, run_codex_stream


def test_transport_failure_without_attached_request_reports_unknown_size():
    error = httpx.RemoteProtocolError("connection closed")

    request_body_bytes, exception_chain = _codex_request_failure_details(error)

    assert request_body_bytes is None
    assert exception_chain == "RemoteProtocolError"


def test_transport_failure_logs_exact_request_bytes_and_class_chain(caplog):
    request_content = b'{"input":"payload"}'
    request = httpx.Request(
        "POST",
        "https://example.invalid/responses",
        content=request_content,
    )
    transport_error = httpx.RemoteProtocolError(
        "server disconnected without sending a response",
        request=request,
    )
    connection_error = APIConnectionError(request=request)
    connection_error.__cause__ = transport_error

    class FailingResponses:
        def create(self, **_kwargs):
            raise connection_error

    client = SimpleNamespace(responses=FailingResponses())
    agent = SimpleNamespace(
        _interrupt_requested=False,
        _current_api_request_id="request-id",
        _fallback_index=0,
        is_subagent=False,
        model="gpt-5.6-sol",
        provider="openai-codex",
        session_id="",
    )

    with caplog.at_level(logging.WARNING, logger="agent.codex_runtime"):
        with pytest.raises(APIConnectionError):
            run_codex_stream(agent, {"model": "gpt-5.6-sol"}, client=client)

    message = caplog.messages[-1]
    assert f"serialized_request_body_bytes={len(request_content)}" in message
    assert "stream_opened=false" in message
    assert "exception_chain=APIConnectionError <- RemoteProtocolError" in message
    assert "payload" not in message
    assert request_content.decode() not in message
    assert "example.invalid" not in message


def test_physical_request_callback_observes_relay_output_and_stream_flag(monkeypatch):
    observed = []

    def _relay_stream(request, callback, **_kwargs):
        physical = dict(request)
        physical["relay_sentinel"] = "managed"
        return callback(physical)

    class FailingResponses:
        def create(self, **kwargs):
            observed.append(("create", dict(kwargs)))
            raise RuntimeError("stop after physical capture")

    monkeypatch.setattr("agent.relay_llm.stream", _relay_stream)
    client = SimpleNamespace(responses=FailingResponses())
    agent = SimpleNamespace(
        _interrupt_requested=False,
        _current_api_request_id="request-id",
        _fallback_index=0,
        is_subagent=False,
        model="gpt-5.6-sol",
        provider="openai-codex",
        session_id="",
    )

    with pytest.raises(RuntimeError, match="stop after physical capture"):
        run_codex_stream(
            agent,
            {"model": "gpt-5.6-sol", "input": []},
            client=client,
            on_physical_request=lambda body: observed.append(
                ("capture", dict(body))
            ),
        )

    assert observed[0][0] == "capture"
    assert observed[0][1]["relay_sentinel"] == "managed"
    assert observed[0][1]["stream"] is True
    assert observed[1] == ("create", observed[0][1])


def test_physical_request_observer_failure_cannot_block_provider_call(monkeypatch):
    called = []

    class FailingResponses:
        def create(self, **kwargs):
            called.append(dict(kwargs))
            raise RuntimeError("provider failure after capture")

    monkeypatch.setattr(
        "agent.relay_llm.stream",
        lambda request, callback, **_kwargs: callback(dict(request)),
    )
    client = SimpleNamespace(responses=FailingResponses())
    agent = SimpleNamespace(
        _interrupt_requested=False,
        _current_api_request_id="request-id",
        _fallback_index=0,
        is_subagent=False,
        model="gpt-5.6-sol",
        provider="openai-codex",
        session_id="",
    )

    with pytest.raises(RuntimeError, match="provider failure after capture"):
        run_codex_stream(
            agent,
            {"model": "gpt-5.6-sol", "input": []},
            client=client,
            on_physical_request=lambda _body: (_ for _ in ()).throw(
                RuntimeError("observer failure")
            ),
        )

    assert len(called) == 1
