"""Tests for transport classification and reasoning-model timeout guidance.

Two layers:

1. **Transport classification (``agent/error_classifier.py``)**:
   A connection drop stays ``FailoverReason.timeout`` regardless of model
   family or session size. It does not enter the compression branch without
   an explicit context-overflow signal.

2. **Detection + guidance (``agent/thinking_timeout_guidance.py``)**:
   When the classifier says timeout AND the model is in the reasoning
   allowlist AND the error message has a transport-kill signature,
   the user gets actionable guidance (raise stale_timeout, lower
   reasoning_budget, or switch models) instead of the misleading
   "use execute_code with Python's open() for large files" advice
   that fires for the unrelated large-file-write stream-drop case.

The guidance remains reasoning-model-specific even though the underlying
transport classification is now uniform across model families.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


# ── helpers ──────────────────────────────────────────────────────────────


class _TimeoutReason:
    """Minimal FailoverReason stand-in for unit tests."""

    def __init__(self, value: str = "timeout") -> None:
        self.value = value


def _classified(reason: str = "timeout", **kwargs) -> SimpleNamespace:
    """Construct a ClassifiedError stand-in with the given reason."""
    defaults = dict(
        reason=_TimeoutReason(reason),
        status_code=None,
        retryable=True,
        should_compress=False,
        should_rotate_credential=False,
        should_fallback=False,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ── Part 1: transport-disconnect classification ──


def _make_session(disconnect_message: str, model: str, *, num_messages: int = 250):
    """Construct inputs to classify_api_error for a disconnect+large-session case."""
    e = Exception(disconnect_message)
    # Keep pressure high to prove neither token nor message count changes the
    # transport classification.
    return e, {
        "provider": "nvidia",
        "model": model,
        "approx_tokens": 130_000,
        "context_length": 200_000,
        "num_messages": num_messages,
    }


class TestClassifierOverride:
    """Transport disconnect classification for reasoning and chat models.

    A transport disconnect stays a timeout regardless of model family or
    session size. Reasoning-model detection remains relevant to the separate
    user guidance path, but no longer changes the base error classification.
    """

    def test_reasoning_model_disconnect_on_large_session_is_timeout(self):
        from agent.error_classifier import classify_api_error, FailoverReason
        e, kwargs = _make_session(
            "server disconnected without sending complete message",
            model="nvidia/nemotron-3-ultra-550b-a55b",
        )
        result = classify_api_error(e, **kwargs)
        assert result.reason == FailoverReason.timeout, (
            "A transport disconnect should remain FailoverReason.timeout "
            "regardless of session size or model family."
        )
        assert result.should_compress is False, (
            "A disconnect without explicit overflow evidence must not compress."
        )
    @pytest.mark.parametrize("model", [
        "nvidia/nemotron-3-ultra-550b-a55b",
        "openai/o3-mini",
        "anthropic/claude-opus-4-6",
        "deepseek/deepseek-r1",
        "qwen/qwq-32b-preview",
        "x-ai/grok-4-fast-reasoning",
    ])
    def test_all_known_reasoning_models_route_to_timeout(self, model):
        from agent.error_classifier import classify_api_error, FailoverReason
        e, kwargs = _make_session(
            "server disconnected without sending complete message",
            model=model,
        )
        result = classify_api_error(e, **kwargs)
        assert result.reason == FailoverReason.timeout
        assert result.should_compress is False

    def test_non_reasoning_model_large_session_routes_to_timeout(self):
        """Chat-model disconnects also remain transport errors."""
        from agent.error_classifier import classify_api_error, FailoverReason
        e, kwargs = _make_session(
            "server disconnected without sending complete message",
            model="gpt-4o",
        )
        result = classify_api_error(e, **kwargs)
        assert result.reason == FailoverReason.timeout
        assert result.should_compress is False

    @pytest.mark.parametrize("model", [
        "olmo-1",
        "gpt-4o",
        "claude-3-5-sonnet-20240620",
        "llama-3.3-70b-instruct",
        "qwen2-72b-instruct",
        "x-ai/grok-3",
    ])
    def test_non_reasoning_models_all_route_to_timeout(self, model):
        from agent.error_classifier import classify_api_error, FailoverReason
        e, kwargs = _make_session(
            "server disconnected without sending complete message",
            model=model,
        )
        result = classify_api_error(e, **kwargs)
        assert result.reason == FailoverReason.timeout
        assert result.should_compress is False

    def test_reasoning_model_small_session_still_routes_to_timeout(self):
        """A reasoning-model disconnect on a small session is also timeout."""
        from agent.error_classifier import classify_api_error, FailoverReason
        e = Exception("server disconnected")
        result = classify_api_error(
            e,
            model="nvidia/nemotron-3-ultra-550b-a55b",
            approx_tokens=5_000,
            context_length=200_000,
            num_messages=10,
        )
        assert result.reason == FailoverReason.timeout

    def test_reasoning_model_with_status_code_does_not_match_disconnect_pattern(self):
        """Status-code errors take the HTTP-status path in the
        classifier, not the status-less disconnect path."""
        from agent.error_classifier import classify_api_error, FailoverReason
        e = Exception("server disconnected")
        # Inject a status_code attribute to simulate an HTTP error
        # whose message happens to contain "server disconnected".
        e.status_code = 503
        result = classify_api_error(
            e,
            model="nvidia/nemotron-3-ultra-550b-a55b",
            approx_tokens=130_000,
            context_length=200_000,
            num_messages=250,
        )
        # 503 specifically routes to overloaded (per the 5xx → 503/529
        # handling in error_classifier.py). The key assertion is that
        # the status-less disconnect branch is not reached.
        assert result.reason != FailoverReason.timeout
        assert result.reason != FailoverReason.context_overflow
        assert result.should_compress is False

# ── Part 2: detection (agent/thinking_timeout_guidance.py:is_thinking_timeout) ──


class TestIsThinkingTimeout:

    @pytest.mark.parametrize("model,msg", [
        ("nvidia/nemotron-3-ultra-550b-a55b", "connection reset by peer"),
        ("openai/o3-mini", "remote protocol error"),
        ("anthropic/claude-opus-4-6", "peer closed connection"),
        ("deepseek/deepseek-r1", "connection lost"),
        ("x-ai/grok-4-fast-reasoning", "server disconnected"),
    ])
    def test_known_reasoning_models_match(self, model, msg):
        from agent.thinking_timeout_guidance import is_thinking_timeout
        classified = _classified(reason="timeout")
        assert is_thinking_timeout(classified, model, msg) is True




    def test_empty_error_msg_returns_false(self):
        from agent.thinking_timeout_guidance import is_thinking_timeout
        classified = _classified(reason="timeout")
        assert is_thinking_timeout(
            classified, "nvidia/nemotron-3-ultra-550b-a55b", "",
        ) is False

    def test_none_error_msg_returns_false(self):
        from agent.thinking_timeout_guidance import is_thinking_timeout
        classified = _classified(reason="timeout")
        assert is_thinking_timeout(
            classified, "nvidia/nemotron-3-ultra-550b-a55b", None,
        ) is False


# ── Part 2: guidance text (agent/thinking_timeout_guidance.py:build_thinking_timeout_guidance) ──


class TestBuildThinkingTimeoutGuidance:
    def test_guidance_mentions_config_path(self):
        from agent.thinking_timeout_guidance import build_thinking_timeout_guidance
        text = build_thinking_timeout_guidance(
            provider="nvidia", model="nvidia/nemotron-3-ultra-550b-a55b",
        )
        assert "providers.nvidia.models.nvidia/nemotron-3-ultra-550b-a55b.stale_timeout_seconds" in text


    def test_guidance_mentions_known_providers(self):
        from agent.thinking_timeout_guidance import build_thinking_timeout_guidance
        text = build_thinking_timeout_guidance(provider="nvidia", model="x")
        # At least one of the known cloud providers should be mentioned
        # to give the user context.
        assert any(p in text for p in (
            "NVIDIA NIM", "OpenAI", "Anthropic", "DeepSeek",
        ))
