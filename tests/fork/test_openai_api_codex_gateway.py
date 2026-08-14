"""Fork regressions for OpenAI API providers backed by Codex-compatible gateways."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_endpoint_metadata_memory_cache():
    """Prevent one regression case's five-minute catalog from leaking to another."""
    import agent.model_metadata as metadata

    metadata._endpoint_model_metadata_cache.clear()
    metadata._endpoint_model_metadata_cache_time.clear()
    yield
    metadata._endpoint_model_metadata_cache.clear()
    metadata._endpoint_model_metadata_cache_time.clear()


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


def test_codexmanager_context_uses_persisted_last_valid_value_when_live_catalog_omits_model(
    monkeypatch, tmp_path
):
    """A partial live catalog must not erase the last exact value for this credential."""
    import agent.model_metadata as metadata

    cache_file = tmp_path / "endpoint_context_cache.json"
    monkeypatch.setattr(
        metadata,
        "_get_endpoint_context_cache_path",
        lambda: cache_file,
        raising=False,
    )
    metadata._endpoint_model_metadata_cache.clear()
    metadata._endpoint_model_metadata_cache_time.clear()

    responses = [
        {
            "object": "list",
            "data": [
                {
                    "id": "gpt-5.6-sol",
                    "object": "model",
                    "owned_by": "codexmanager",
                }
            ],
            "models": [
                {"slug": "gpt-5.6-sol", "context_window": 272_000},
            ],
        },
        {
            "object": "list",
            "data": [
                {
                    "id": "gpt-5.6-luna",
                    "object": "model",
                    "owned_by": "codexmanager",
                }
            ],
            "models": [
                {"slug": "gpt-5.6-luna", "context_window": 272_000},
            ],
        },
    ]

    def fake_get(url, *args, **kwargs):
        return _ModelsResponse(responses.pop(0))

    monkeypatch.setattr(metadata.requests, "get", fake_get)
    base_url = "https://codex.example.test/v1"
    api_key = "credential-a"

    assert metadata._resolve_endpoint_context_length(
        "gpt-5.6-sol", base_url, api_key=api_key
    ) == 272_000
    assert cache_file.exists()

    metadata._endpoint_model_metadata_cache.clear()
    metadata._endpoint_model_metadata_cache_time.clear()

    assert metadata._resolve_endpoint_context_length(
        "gpt-5.6-sol", base_url, api_key=api_key
    ) == 272_000
    assert "gpt-5.6-sol" not in metadata.fetch_endpoint_model_metadata(
        base_url, api_key=api_key
    )


def test_codexmanager_context_cache_isolated_by_credential_and_updates_live_value(
    monkeypatch, tmp_path
):
    """Each credential keeps its own last value and a newer exact value replaces it."""
    import agent.model_metadata as metadata

    cache_file = tmp_path / "endpoint_context_cache.json"
    monkeypatch.setattr(
        metadata,
        "_get_endpoint_context_cache_path",
        lambda: cache_file,
        raising=False,
    )

    def catalog(context_length=None):
        if context_length is None:
            return {
                "object": "list",
                "data": [
                    {
                        "id": "gpt-5.6-luna",
                        "object": "model",
                        "owned_by": "codexmanager",
                    }
                ],
                "models": [
                    {"slug": "gpt-5.6-luna", "context_window": 272_000},
                ],
            }
        return {
            "object": "list",
            "data": [
                {
                    "id": "gpt-5.6-sol",
                    "object": "model",
                    "owned_by": "codexmanager",
                }
            ],
            "models": [
                {"slug": "gpt-5.6-sol", "context_window": context_length},
            ],
        }

    responses = [
        catalog(272_000),
        catalog(372_000),
        catalog(300_000),
        catalog(),
        catalog(),
    ]

    def fake_get(url, *args, **kwargs):
        return _ModelsResponse(responses.pop(0))

    monkeypatch.setattr(metadata.requests, "get", fake_get)
    base_url = "https://codex.example.test/v1"

    def resolve(api_key, *, clear_memory=True):
        if clear_memory:
            metadata._endpoint_model_metadata_cache.clear()
            metadata._endpoint_model_metadata_cache_time.clear()
        return metadata._resolve_endpoint_context_length(
            "gpt-5.6-sol", base_url, api_key=api_key
        )

    assert resolve("credential-a") == 272_000
    # Do not clear the five-minute memory cache: credential B must still fetch
    # and retain its own live catalog rather than reusing credential A's 272K.
    assert resolve("credential-b", clear_memory=False) == 372_000
    assert resolve("credential-a") == 300_000
    assert resolve("credential-a") == 300_000
    assert resolve("credential-b") == 372_000

    cache_text = cache_file.read_text(encoding="utf-8")
    assert "credential-a" not in cache_text
    assert "credential-b" not in cache_text


def test_public_context_resolution_bypasses_legacy_unscoped_cache_per_credential(
    monkeypatch, tmp_path
):
    """The public resolver must not let model@URL cache mask live Key metadata."""
    import agent.model_metadata as metadata

    cache_file = tmp_path / "endpoint_context_cache.json"
    monkeypatch.setattr(
        metadata,
        "_get_endpoint_context_cache_path",
        lambda: cache_file,
        raising=False,
    )
    monkeypatch.setattr(
        metadata,
        "get_cached_context_length",
        lambda model, base_url: 111_000,
    )
    contexts = {"credential-a": 272_000, "credential-b": 372_000}
    calls = []

    def fake_get(url, *args, **kwargs):
        api_key = kwargs["headers"]["Authorization"].removeprefix("Bearer ")
        calls.append(api_key)
        context = contexts[api_key]
        return _ModelsResponse(
            {
                "object": "list",
                "data": [
                    {"id": "gpt-5.6-sol", "owned_by": "codexmanager"},
                ],
                "models": [
                    {"slug": "gpt-5.6-sol", "context_window": context},
                ],
            }
        )

    monkeypatch.setattr(metadata.requests, "get", fake_get)
    base_url = "https://codex.example.test/v1"

    assert metadata.get_model_context_length(
        "gpt-5.6-sol",
        base_url=base_url,
        api_key="credential-a",
        provider="openai-api",
    ) == 272_000
    assert metadata.get_model_context_length(
        "gpt-5.6-sol",
        base_url=base_url,
        api_key="credential-b",
        provider="openai-api",
    ) == 372_000
    assert calls == ["credential-a", "credential-b"]


def test_public_context_resolution_uses_scoped_stale_value_before_legacy_cache(
    monkeypatch, tmp_path
):
    """A failed live refresh must still prefer this credential's saved value."""
    import requests

    import agent.model_metadata as metadata

    cache_file = tmp_path / "endpoint_context_cache.json"
    monkeypatch.setattr(
        metadata,
        "_get_endpoint_context_cache_path",
        lambda: cache_file,
        raising=False,
    )
    base_url = "https://codex.example.test/v1"
    api_key = "credential-a"
    metadata._save_endpoint_context_length(
        "gpt-5.6-sol", base_url, api_key, 272_000
    )
    monkeypatch.setattr(
        metadata,
        "get_cached_context_length",
        lambda model, url: 111_000,
    )
    monkeypatch.setattr(
        metadata.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            requests.ConnectionError("temporary catalog failure")
        ),
    )

    assert metadata.get_model_context_length(
        "gpt-5.6-sol",
        base_url=base_url,
        api_key=api_key,
        provider="openai-api",
    ) == 272_000


@pytest.mark.parametrize(
    ("provider", "base_url"),
    [
        ("openai-api", "https://api.openai.com/v1"),
        ("custom", "https://generic.example.test/v1"),
    ],
)
def test_unrelated_routes_keep_legacy_context_cache_order(
    monkeypatch, provider, base_url
):
    """The credential-first rule is limited to custom openai-api gateways."""
    import agent.model_metadata as metadata

    monkeypatch.setattr(
        metadata,
        "get_cached_context_length",
        lambda model, url: 111_000,
    )
    monkeypatch.setattr(
        metadata.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unrelated route must return its legacy cache first")
        ),
    )

    assert metadata.get_model_context_length(
        "gpt-5.6-sol",
        base_url=base_url,
        api_key="credential-a",
        provider=provider,
    ) == 111_000


def test_codexmanager_context_cache_survives_later_models_request_failure(
    monkeypatch, tmp_path
):
    """A transient request failure must not discard a previously confirmed value."""
    import requests

    import agent.model_metadata as metadata

    cache_file = tmp_path / "endpoint_context_cache.json"
    monkeypatch.setattr(
        metadata,
        "_get_endpoint_context_cache_path",
        lambda: cache_file,
        raising=False,
    )
    live = {
        "object": "list",
        "data": [
            {
                "id": "gpt-5.6-sol",
                "object": "model",
                "owned_by": "codexmanager",
            }
        ],
        "models": [
            {"slug": "gpt-5.6-sol", "context_window": 272_000},
        ],
    }
    calls = 0

    def fake_get(url, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _ModelsResponse(live)
        raise requests.ConnectionError("temporary catalog failure")

    monkeypatch.setattr(metadata.requests, "get", fake_get)
    base_url = "https://codex.example.test/v1"
    api_key = "credential-a"

    assert metadata._resolve_endpoint_context_length(
        "gpt-5.6-sol", base_url, api_key=api_key
    ) == 272_000

    metadata._endpoint_model_metadata_cache.clear()
    metadata._endpoint_model_metadata_cache_time.clear()

    assert metadata._resolve_endpoint_context_length(
        "gpt-5.6-sol", base_url, api_key=api_key
    ) == 272_000


def test_codexmanager_context_cache_survives_missing_context_field(
    monkeypatch, tmp_path
):
    """A present model with no live window must not erase its last valid value."""
    import agent.model_metadata as metadata

    cache_file = tmp_path / "endpoint_context_cache.json"
    monkeypatch.setattr(
        metadata,
        "_get_endpoint_context_cache_path",
        lambda: cache_file,
        raising=False,
    )
    full = {
        "object": "list",
        "data": [{"id": "gpt-5.6-sol", "owned_by": "codexmanager"}],
        "models": [
            {"slug": "gpt-5.6-sol", "context_window": 272_000},
        ],
    }
    without_window = {
        "object": "list",
        "data": [{"id": "gpt-5.6-sol", "owned_by": "codexmanager"}],
        "models": [],
    }
    responses = iter([full, without_window])
    monkeypatch.setattr(
        metadata.requests,
        "get",
        lambda *args, **kwargs: _ModelsResponse(next(responses)),
    )
    base_url = "https://codex.example.test/v1"
    api_key = "credential-a"

    assert metadata._resolve_endpoint_context_length(
        "gpt-5.6-sol", base_url, api_key=api_key
    ) == 272_000
    metadata._endpoint_model_metadata_cache.clear()
    metadata._endpoint_model_metadata_cache_time.clear()
    assert metadata._resolve_endpoint_context_length(
        "gpt-5.6-sol", base_url, api_key=api_key
    ) == 272_000


def test_codexmanager_context_cache_isolated_by_endpoint(monkeypatch, tmp_path):
    """One URL's last valid value must never populate another endpoint."""
    import agent.model_metadata as metadata

    cache_file = tmp_path / "endpoint_context_cache.json"
    monkeypatch.setattr(
        metadata,
        "_get_endpoint_context_cache_path",
        lambda: cache_file,
        raising=False,
    )
    full = {
        "object": "list",
        "data": [{"id": "gpt-5.6-sol", "owned_by": "codexmanager"}],
        "models": [
            {"slug": "gpt-5.6-sol", "context_window": 272_000},
        ],
    }
    partial = {
        "object": "list",
        "data": [{"id": "gpt-5.6-luna", "owned_by": "codexmanager"}],
        "models": [
            {"slug": "gpt-5.6-luna", "context_window": 272_000},
        ],
    }
    endpoint_a_calls = 0

    def fake_get(url, *args, **kwargs):
        nonlocal endpoint_a_calls
        if "codex-a.example.test" in url:
            endpoint_a_calls += 1
            return _ModelsResponse(full if endpoint_a_calls == 1 else partial)
        return _ModelsResponse(partial)

    monkeypatch.setattr(metadata.requests, "get", fake_get)
    api_key = "credential-a"
    endpoint_a = "https://codex-a.example.test/v1"
    endpoint_b = "https://codex-b.example.test/v1"

    assert metadata._resolve_endpoint_context_length(
        "gpt-5.6-sol", endpoint_a, api_key=api_key
    ) == 272_000
    metadata._endpoint_model_metadata_cache.clear()
    metadata._endpoint_model_metadata_cache_time.clear()
    assert metadata._resolve_endpoint_context_length(
        "gpt-5.6-sol", endpoint_b, api_key=api_key
    ) is None
    metadata._endpoint_model_metadata_cache.clear()
    metadata._endpoint_model_metadata_cache_time.clear()
    assert metadata._resolve_endpoint_context_length(
        "gpt-5.6-sol", endpoint_a, api_key=api_key
    ) == 272_000


def test_codexmanager_context_cache_corruption_is_a_safe_miss(
    monkeypatch, tmp_path
):
    """A damaged local cache must not crash context resolution."""
    import agent.model_metadata as metadata

    cache_file = tmp_path / "endpoint_context_cache.json"
    cache_file.write_text("{broken-json", encoding="utf-8")
    monkeypatch.setattr(
        metadata,
        "_get_endpoint_context_cache_path",
        lambda: cache_file,
        raising=False,
    )
    partial = {
        "object": "list",
        "data": [{"id": "gpt-5.6-sol", "owned_by": "codexmanager"}],
        "models": [],
    }
    monkeypatch.setattr(
        metadata.requests,
        "get",
        lambda *args, **kwargs: _ModelsResponse(partial),
    )

    assert metadata._resolve_endpoint_context_length(
        "gpt-5.6-sol",
        "https://codex.example.test/v1",
        api_key="credential-a",
    ) is None


def test_codexmanager_context_cache_repairs_malformed_scope(monkeypatch, tmp_path):
    """Structurally damaged JSON must be replaced by the next exact live value."""
    import json

    import agent.model_metadata as metadata

    cache_file = tmp_path / "endpoint_context_cache.json"
    base_url = "https://codex.example.test/v1"
    api_key = "credential-a"
    scope_id, _, _ = metadata._endpoint_context_scope(base_url, api_key)
    cache_file.write_text(
        json.dumps({"version": 1, "scopes": {scope_id: "broken-scope"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        metadata,
        "_get_endpoint_context_cache_path",
        lambda: cache_file,
        raising=False,
    )
    full = {
        "object": "list",
        "data": [{"id": "gpt-5.6-sol", "owned_by": "codexmanager"}],
        "models": [
            {"slug": "gpt-5.6-sol", "context_window": 272_000},
        ],
    }
    monkeypatch.setattr(
        metadata.requests,
        "get",
        lambda *args, **kwargs: _ModelsResponse(full),
    )

    assert metadata._resolve_endpoint_context_length(
        "gpt-5.6-sol", base_url, api_key=api_key
    ) == 272_000
    repaired = json.loads(cache_file.read_text(encoding="utf-8"))
    assert repaired["scopes"][scope_id]["models"]["gpt-5.6-sol"][
        "context_length"
    ] == 272_000


@pytest.mark.parametrize("bad_models", [None, [], "broken-models"])
def test_codexmanager_context_cache_malformed_models_is_a_safe_miss(
    monkeypatch, tmp_path, bad_models
):
    """Parseable cache JSON with a non-object models value must not raise."""
    import json

    import agent.model_metadata as metadata

    cache_file = tmp_path / "endpoint_context_cache.json"
    base_url = "https://codex.example.test/v1"
    api_key = "credential-a"
    scope_id, _, _ = metadata._endpoint_context_scope(base_url, api_key)
    cache_file.write_text(
        json.dumps(
            {
                "version": 1,
                "scopes": {scope_id: {"models": bad_models}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        metadata,
        "_get_endpoint_context_cache_path",
        lambda: cache_file,
        raising=False,
    )

    assert metadata._get_endpoint_cached_context_length(
        "gpt-5.6-sol", base_url, api_key
    ) is None
