"""Tests for the non-stream stale-call detector context estimator.

Covers:
- ``estimate_request_context_tokens`` for Chat Completions, Responses API,
  bare lists, and mixed-shape dicts.
- ``AIAgent._compute_non_stream_stale_timeout`` with both legacy ``messages``
  list and full ``api_kwargs`` dicts.
- The May 2026 default-base change (300s -> 90s) and the lowered
  context-tier ceilings (450/600 -> 150/240).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace



def _write_config(tmp_path: Path, body: str) -> None:
    hermes_home = tmp_path
    (hermes_home / "config.yaml").write_text(body or "{}\n", encoding="utf-8")


def _make_agent(tmp_path: Path, **overrides):
    from run_agent import AIAgent
    kwargs = dict(
        model="gpt-5.5",
        provider="openai-codex",
        api_key="sk-dummy",
        base_url="https://chatgpt.com/backend-api/codex",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        platform="cli",
    )
    kwargs.update(overrides)
    return AIAgent(**kwargs)


# ── estimator ──────────────────────────────────────────────────────────────




def test_estimator_responses_api_input():
    from agent.chat_completion_helpers import estimate_request_context_tokens
    payload = {
        "model": "gpt-5.5",
        "instructions": "i" * 1000,
        "input": "x" * 4000,
        "tools": [{"name": "t", "description": "d" * 200}],
    }
    # input(4000) + instructions(1000) + tools (~stringified) -> well over 1000 tokens
    tokens = estimate_request_context_tokens(payload)
    assert tokens >= 1200, f"Responses API estimator returned {tokens}"






def test_estimator_empty_inputs():
    from agent.chat_completion_helpers import estimate_request_context_tokens
    assert estimate_request_context_tokens({}) == 0
    assert estimate_request_context_tokens([]) == 0
    assert estimate_request_context_tokens(None) == 0




# ── default base + tier scaling ────────────────────────────────────────────


def test_default_base_is_90s(monkeypatch, tmp_path):
    """Default base stale timeout dropped from 300s to 90s (May 2026)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.delenv("HERMES_API_CALL_STALE_TIMEOUT", raising=False)
    _write_config(tmp_path, "")

    agent = _make_agent(tmp_path)
    base, implicit = agent._resolved_api_call_stale_timeout_base()
    assert base == 90.0
    assert implicit is True










def test_explicit_user_config_overrides_default(monkeypatch, tmp_path):
    """If the user explicitly sets a stale_timeout, the new defaults don't apply."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("", encoding="utf-8")
    _write_config(tmp_path, """\
providers:
  openai-codex:
    stale_timeout_seconds: 1800
""")
    monkeypatch.delenv("HERMES_API_CALL_STALE_TIMEOUT", raising=False)

    import importlib
    from hermes_cli import timeouts as to_mod
    importlib.reload(to_mod)

    agent = _make_agent(tmp_path)
    assert agent._compute_non_stream_stale_timeout({"input": "hi"}) == 1800.0


# ── openai-codex gateway-scale stale floor ────────────────────────────────




def test_openai_codex_stale_floor_tiers():
    from agent.chat_completion_helpers import openai_codex_stale_timeout_floor

    assert openai_codex_stale_timeout_floor(55_000) == 900.0
    assert openai_codex_stale_timeout_floor(120_000) == 1200.0


def test_openai_api_uses_shared_codex_backend_watchdog_classification():
    from agent.chat_completion_helpers import _is_openai_codex_backend

    assert _is_openai_codex_backend(
        SimpleNamespace(
            provider="openai-api",
            _base_url_hostname="codex.chantx.top",
            _base_url_lower="https://codex.chantx.top/v1",
        )
    )
    assert not _is_openai_codex_backend(
        SimpleNamespace(
            provider="openrouter",
            _base_url_hostname="openrouter.ai",
            _base_url_lower="https://openrouter.ai/api/v1",
        )
    )


def test_openai_api_codex_responses_large_context_uses_900s_floor(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.delenv("HERMES_API_CALL_STALE_TIMEOUT", raising=False)
    _write_config(tmp_path, "")

    agent = _make_agent(
        tmp_path,
        model="gpt-5.6-sol",
        provider="openai-api",
        base_url="https://codex.chantx.top/v1",
    )
    setattr(agent, "api_mode", "codex_responses")

    payload = {"model": "gpt-5.6-sol", "input": "x" * 220_000}
    assert agent._compute_non_stream_stale_timeout(payload) == 900.0


def test_openai_api_chat_completions_keeps_generic_large_context_timeout(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.delenv("HERMES_API_CALL_STALE_TIMEOUT", raising=False)
    _write_config(tmp_path, "")

    agent = _make_agent(
        tmp_path,
        model="plain-model",
        provider="openai-api",
        base_url="https://api.example.test/v1",
    )
    setattr(agent, "api_mode", "chat_completions")

    payload = {"model": "plain-model", "messages": [{"content": "x" * 220_000}]}
    assert agent._compute_non_stream_stale_timeout(payload) == 150.0
