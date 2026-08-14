"""Fork regressions for OpenAI API providers backed by Codex-compatible gateways."""

from __future__ import annotations


class _ModelsResponse:
    status_code = 200
    ok = True

    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body

    def close(self):
        return None


def test_openai_api_auxiliary_inherits_declared_transport(monkeypatch):
    """Auxiliary calls must resolve the same wire transport as the main runtime."""
    from agent.auxiliary_client import CodexAuxiliaryClient, resolve_provider_client

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://codex.example.test/v1")

    client, model = resolve_provider_client(
        "openai-api",
        model="gpt-5.6-luna",
    )

    assert model == "gpt-5.6-luna"
    assert isinstance(client, CodexAuxiliaryClient)
    assert str(client.base_url).rstrip("/") == "https://codex.example.test/v1"


def test_openai_api_auxiliary_explicit_non_codex_mode_wins(monkeypatch):
    """A task-level wire mode must override the provider-declared transport."""
    from agent.auxiliary_client import CodexAuxiliaryClient, resolve_provider_client

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://codex.example.test/v1")

    client, model = resolve_provider_client(
        "openai-api",
        model="gpt-5.6-luna",
        api_mode="chat_completions",
    )

    assert model == "gpt-5.6-luna"
    assert client is not None
    assert not isinstance(client, CodexAuxiliaryClient)


def test_codexmanager_endpoint_metadata_uses_api_context_window(monkeypatch):
    """CodexManager's rich model catalog is authoritative over static tables."""
    import agent.model_metadata as metadata

    metadata._endpoint_model_metadata_cache.clear()
    metadata._endpoint_model_metadata_cache_time.clear()
    calls = []

    def fake_get(url, *args, **kwargs):
        calls.append({"url": url, "params": kwargs.get("params")})
        return _ModelsResponse({
            "object": "list",
            "data": [
                {
                    "id": "gpt-5.6-sol",
                    "object": "model",
                    "owned_by": "codexmanager",
                }
            ],
            "models": [
                {
                    "slug": "gpt-5.6-sol",
                    "context_window": 333_000,
                    "max_context_window": 333_000,
                }
            ],
        })

    monkeypatch.setattr(metadata.requests, "get", fake_get)

    context_length = metadata._resolve_endpoint_context_length(
        "gpt-5.6-sol",
        "https://codex.example.test/v1",
        api_key="test-key",
    )

    assert context_length == 333_000
    assert calls == [{"url": "https://codex.example.test/v1/models", "params": None}]


def test_non_codexmanager_endpoint_ignores_rich_models_extension(monkeypatch):
    """A non-CodexManager endpoint must not opt into the private extension."""
    import agent.model_metadata as metadata

    metadata._endpoint_model_metadata_cache.clear()
    metadata._endpoint_model_metadata_cache_time.clear()

    def fake_get(url, *args, **kwargs):
        return _ModelsResponse({
            "object": "list",
            "data": [
                {
                    "id": "gpt-5.6-sol",
                    "object": "model",
                    "owned_by": "openai",
                }
            ],
            "models": [
                {"slug": "gpt-5.6-sol", "context_window": 999_000},
            ],
        })

    monkeypatch.setattr(metadata.requests, "get", fake_get)

    context_length = metadata._resolve_endpoint_context_length(
        "gpt-5.6-sol",
        "https://openai.example.test/v1",
        api_key="test-key",
    )

    assert context_length is None


def test_codexmanager_catalog_does_not_guess_for_unlisted_model(monkeypatch):
    """An unlisted model must not inherit another model's advertised window."""
    import agent.model_metadata as metadata

    metadata._endpoint_model_metadata_cache.clear()
    metadata._endpoint_model_metadata_cache_time.clear()

    def fake_get(url, *args, **kwargs):
        return _ModelsResponse({
            "object": "list",
            "data": [
                {
                    "id": "gpt-5.7-terra",
                    "object": "model",
                    "owned_by": "codexmanager",
                }
            ],
            "models": [
                {"slug": "gpt-5.7-terra", "context_window": 444_000},
                {"slug": "gpt-5.7-luna", "context_window": 444_000},
            ],
        })

    monkeypatch.setattr(metadata.requests, "get", fake_get)

    context_length = metadata._resolve_endpoint_context_length(
        "gpt-5.7",
        "https://codex.example.test/v1",
        api_key="test-key",
    )

    assert context_length is None
