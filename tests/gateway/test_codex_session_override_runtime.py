from types import SimpleNamespace

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    runner._session_model_overrides = {}
    return runner


def test_codex_session_override_resolves_fresh_runtime(monkeypatch):
    from gateway import run as gateway_run

    source = _make_source()
    session_key = build_session_key(source)
    runner = _make_runner()
    runner._session_model_overrides[session_key] = {
        "model": "gpt-5.5",
        "provider": "openai-codex",
        "api_key": "stale-token-from-old-entry",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_mode": "codex_responses",
    }

    pool = SimpleNamespace(provider="openai-codex", marker="codex-pool")
    calls = []

    def fake_resolve_runtime_provider(*, requested=None, explicit_api_key=None, explicit_base_url=None, target_model=None):
        calls.append(
            {
                "requested": requested,
                "explicit_api_key": explicit_api_key,
                "explicit_base_url": explicit_base_url,
                "target_model": target_model,
            }
        )
        return {
            "provider": "openai-codex",
            "api_key": "fresh-token-from-pool-front",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_mode": "codex_responses",
            "credential_pool": pool,
        }

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", fake_resolve_runtime_provider)

    model, runtime = runner._resolve_session_agent_runtime(
        source=source,
        session_key=session_key,
        user_config={"model": {"default": "qwen3.6-plus"}},
    )

    assert model == "gpt-5.5"
    assert runtime["provider"] == "openai-codex"
    assert runtime["api_key"] == "fresh-token-from-pool-front"
    assert runtime["credential_pool"] is pool
    assert calls == [
        {
            "requested": "openai-codex",
            "explicit_api_key": None,
            "explicit_base_url": None,
            "target_model": "gpt-5.5",
        }
    ]


def test_non_codex_session_override_keeps_fast_path(monkeypatch):
    source = _make_source()
    session_key = build_session_key(source)
    runner = _make_runner()
    runner._session_model_overrides[session_key] = {
        "model": "qwen3.6-plus",
        "provider": "custom",
        "api_key": "custom-token",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_mode": "chat_completions",
    }

    def fail_if_called(**kwargs):  # pragma: no cover - must not be called
        raise AssertionError("non-Codex fast path should not resolve fresh runtime")

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", fail_if_called)

    model, runtime = runner._resolve_session_agent_runtime(
        source=source,
        session_key=session_key,
        user_config={"model": {"default": "gpt-5.5"}},
    )

    assert model == "qwen3.6-plus"
    assert runtime == {
        "provider": "custom",
        "api_key": "custom-token",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_mode": "chat_completions",
    }
