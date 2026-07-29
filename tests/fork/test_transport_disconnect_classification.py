"""Fork contract: transport disconnects must not masquerade as context overflow."""

import httpx
import pytest

from agent.error_classifier import FailoverReason, classify_api_error


class _APIError(Exception):
    """Minimal HTTP-style provider error for classifier contract tests."""

    def __init__(self, message, *, status_code, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body or {}


def _assert_transport_timeout(result):
    assert result.reason == FailoverReason.timeout
    assert result.retryable is True
    assert result.should_compress is False


def test_real_incomplete_chunked_read_stays_transport_at_high_context_pressure():
    error = httpx.RemoteProtocolError(
        "peer closed connection without sending complete message body "
        "(incomplete chunked read)"
    )

    result = classify_api_error(
        error,
        provider="openai-codex",
        model="gpt-5.6-sol",
        approx_tokens=184_491,
        context_length=272_000,
        num_messages=34,
    )

    _assert_transport_timeout(result)


@pytest.mark.parametrize(
    ("message", "approx_tokens", "num_messages"),
    [
        ("server disconnected without sending complete message", 150_000, 10),
        ("peer closed connection without sending complete message", 5_000, 250),
    ],
)
def test_message_only_disconnect_ignores_token_and_message_pressure(
    message, approx_tokens, num_messages
):
    result = classify_api_error(
        Exception(message),
        approx_tokens=approx_tokens,
        context_length=200_000,
        num_messages=num_messages,
    )

    _assert_transport_timeout(result)


def test_explicit_provider_context_overflow_still_compresses():
    error = _APIError(
        "context length exceeded: 300000 > 272000",
        status_code=400,
    )

    result = classify_api_error(
        error,
        provider="openai-codex",
        model="gpt-5.6-sol",
        approx_tokens=1,
        context_length=272_000,
    )

    assert result.reason == FailoverReason.context_overflow
    assert result.should_compress is True


def test_generic_http_400_large_request_heuristic_remains_intact():
    error = _APIError(
        "Error",
        status_code=400,
        body={"error": {"message": "Error"}},
    )

    result = classify_api_error(
        error,
        approx_tokens=100_000,
        context_length=200_000,
    )

    assert result.reason == FailoverReason.context_overflow
    assert result.should_compress is True
