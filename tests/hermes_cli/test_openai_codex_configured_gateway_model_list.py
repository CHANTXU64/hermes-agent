"""Tests for configured OpenAI Codex gateway entries in /model."""

from hermes_cli.model_switch import list_authenticated_providers
from hermes_cli.models import provider_model_ids


class _EmptyPool:
    def has_credentials(self):
        return False


def _configured_codex_manager_config():
    return {
        "model": {
            "provider": "openai-codex",
            "default": "gpt-5.5",
            "base_url": "http://127.0.0.1:48761/v1",
            "api_key": "${CODEXMANAGER_API_KEY}",
            "api_mode": "codex_responses",
        }
    }


def test_model_picker_shows_configured_openai_codex_gateway(monkeypatch):
    monkeypatch.setenv("CODEXMANAGER_API_KEY", "cm-key")
    monkeypatch.setattr("hermes_cli.config.load_config", _configured_codex_manager_config)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    pool_providers = []

    def _record_pool_probe(provider):
        pool_providers.append(provider)
        return _EmptyPool()

    monkeypatch.setattr("agent.credential_pool.load_pool", _record_pool_probe)
    monkeypatch.setattr(
        "hermes_cli.models.fetch_api_models",
        lambda api_key, base_url: ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"],
    )

    providers = list_authenticated_providers(
        current_provider="openai-codex",
        current_base_url="http://127.0.0.1:48761/v1",
        current_model="gpt-5.5",
        max_models=5,
    )

    codex = next((p for p in providers if p["slug"] == "openai-codex"), None)
    assert codex is not None
    assert codex["is_current"] is True
    assert codex["models"] == ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"]
    assert codex["total_models"] == 3
    assert "openai-codex" not in pool_providers


def test_provider_model_ids_uses_configured_openai_codex_gateway_models(monkeypatch):
    monkeypatch.setenv("CODEXMANAGER_API_KEY", "cm-key")
    monkeypatch.setattr("hermes_cli.config.load_config", _configured_codex_manager_config)
    monkeypatch.setattr(
        "hermes_cli.models.fetch_api_models",
        lambda api_key, base_url: ["gpt-5.5", "gpt-5.4"],
    )

    assert provider_model_ids("openai-codex") == ["gpt-5.5", "gpt-5.4"]
