"""Fork regression: image_gen.provider=openai-api reuses chat credentials."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx

PLUGIN_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "image_gen"
    / "openai-codex"
    / "__init__.py"
)


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "fork_image_gen_openai_codex", PLUGIN_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_openai_api_provider_is_registered_by_name():
    module = _load_plugin()
    registered = []

    class _Ctx:
        def register_image_gen_provider(self, provider):
            registered.append(provider)

    module.register(_Ctx())
    names = [provider.name for provider in registered]
    assert "openai-codex" in names
    assert "openai-api" in names


def test_each_image_provider_reads_its_own_model_section(monkeypatch):
    module = _load_plugin()
    monkeypatch.delenv("OPENAI_IMAGE_MODEL", raising=False)
    monkeypatch.setattr(
        module,
        "_load_image_gen_config",
        lambda: {
            "openai-codex": {"model": "gpt-image-2-low"},
            "openai-api": {"model": "gpt-image-2-high"},
        },
    )

    assert module._resolve_model()[0] == "gpt-image-2-low"
    assert module._resolve_model("openai-api")[0] == "gpt-image-2-high"


def test_openai_api_host_model_does_not_inherit_another_provider(monkeypatch):
    module = _load_plugin()
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(module, "_load_image_gen_config", lambda: {})
    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {
            "model": {
                "provider": "anthropic",
                "default": "claude-opus-4-1",
            }
        },
    )
    assert module._resolve_host_chat_model() == module._CODEX_CHAT_MODEL

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {
            "model": {
                "provider": "openai-api",
                "default": "gpt-5.6-luna",
            }
        },
    )
    assert module._resolve_host_chat_model() == "gpt-5.6-luna"

    monkeypatch.setattr(
        module,
        "_load_image_gen_config",
        lambda: {"openai-api": {"chat_model": "gpt-explicit"}},
    )
    assert module._resolve_host_chat_model() == "gpt-explicit"


def test_openai_api_generate_posts_to_runtime_base_url(monkeypatch, tmp_path):
    module = _load_plugin()
    captured = {}

    monkeypatch.setattr(
        module,
        "_resolve_openai_api_runtime",
        lambda: {
            "api_key": "test-key",
            "base_url": "https://codex.example.test/v1",
        },
    )
    monkeypatch.setattr(module, "_resolve_host_chat_model", lambda: "gpt-5.6-sol")
    monkeypatch.setattr(
        module,
        "_resolve_model",
        lambda _section_key="openai-codex": (
            "gpt-image-2-medium",
            module._MODELS["gpt-image-2-medium"],
        ),
    )

    fake_png = tmp_path / "openai_api_gpt-image-2-medium.png"
    fake_png.write_bytes(b"png")
    monkeypatch.setattr(module, "save_b64_image", lambda *_args, **_kwargs: fake_png)

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def iter_lines(self):
            payload = {
                "type": "image_generation_call",
                "result": "aW1hZ2U=",
            }
            yield f"data: {json.dumps(payload)}"
            yield ""

    class _Stream:
        def __enter__(self):
            return _Response()

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Client:
        def __init__(self, timeout=None, headers=None):
            captured["headers"] = dict(headers or {})

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, json=None):
            captured["method"] = method
            captured["url"] = url
            captured["payload"] = json
            return _Stream()

    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(httpx, "Timeout", lambda *args, **kwargs: "timeout")

    result = module.OpenAIApiImageGenProvider().generate("a red apple")

    assert result["success"] is True
    assert result["provider"] == "openai-api"
    assert result["image"] == str(fake_png)
    assert captured["method"] == "POST"
    assert captured["url"] == "https://codex.example.test/v1/responses"
    assert captured["payload"]["model"] == "gpt-5.6-sol"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert "originator" not in captured["headers"]
