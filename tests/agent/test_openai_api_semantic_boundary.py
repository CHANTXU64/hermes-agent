"""Regression boundaries after retiring the fork-only openai-api Codex gateway."""

from __future__ import annotations


def test_openai_api_auxiliary_does_not_inherit_provider_codex_mode(monkeypatch):
    """Provider metadata must not silently turn ordinary openai-api aux calls into Codex."""
    from agent.auxiliary_client import CodexAuxiliaryClient, resolve_provider_client

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://codex.example.test/v1")
    monkeypatch.setattr(
        "hermes_cli.providers.determine_api_mode",
        lambda *_args, **_kwargs: "codex_responses",
    )

    client, model = resolve_provider_client(
        "openai-api",
        model="gpt-5.6-luna",
    )

    assert model == "gpt-5.6-luna"
    assert client is not None
    assert not isinstance(client, CodexAuxiliaryClient)


def test_other_auxiliary_providers_do_not_use_fork_transport_inheritance(monkeypatch):
    """Provider-declared Responses routing is left to upstream, not this fork."""
    from agent.auxiliary_client import CodexAuxiliaryClient, resolve_provider_client

    monkeypatch.setenv("XAI_API_KEY", "test-key")
    monkeypatch.setattr(
        "hermes_cli.providers.determine_api_mode",
        lambda *_args, **_kwargs: "codex_responses",
    )

    client, model = resolve_provider_client(
        "xai",
        model="grok-4.6",
    )

    assert model == "grok-4.6"
    assert not isinstance(client, CodexAuxiliaryClient)


def test_generic_endpoint_ignores_codexmanager_private_models_extension():
    """Generic endpoint metadata must not depend on a retired gateway schema."""
    from unittest.mock import MagicMock, patch

    import agent.model_metadata as mm

    mm._endpoint_model_metadata_cache.clear()
    mm._endpoint_model_metadata_cache_time.clear()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "data": [{"id": "gpt-5.6-luna", "owned_by": "codexmanager"}],
        "models": [{"slug": "gpt-5.6-luna", "context_window": 272_000}],
    }

    with patch("agent.model_metadata.requests.get", return_value=response):
        metadata = mm.fetch_endpoint_model_metadata(
            "https://generic.example.test/v1",
            api_key="test-key",
        )

    assert metadata["gpt-5.6-luna"].get("context_length") is None
