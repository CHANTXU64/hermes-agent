"""Fork contract for the Qwen/custom STT plugin boundary."""

from __future__ import annotations

import os
from pathlib import Path

import yaml


class _Response:
    status_code = 200
    text = ""

    @staticmethod
    def json():
        return {"output": {"text": "  项目名称识别成功  "}}


def test_bundled_qwen_stt_auto_registers_and_owns_public_dispatch(
    monkeypatch, tmp_path
):
    """The bundled backend must auto-register without plugins.enabled config."""
    from agent import transcription_registry
    import hermes_cli.plugins as plugins_module
    from hermes_cli.plugins import PluginManager
    from tools import transcription_tools

    hermes_home = Path(os.environ["HERMES_HOME"])
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "stt": {
                    "provider": "custom_api",
                    "custom_api": {
                        "base_url": "https://dashscope.aliyuncs.com/api/v1",
                        "endpoint": "/services/aigc/multimodal-generation/generation",
                        "api_key_env": "QWEN_API_KEY",
                        "model": "qwen-audio-3.0-asr-flash",
                        "mode": "dashscope_multimodal",
                        "language": "zh",
                        "prompt": "施工术语提示",
                        "keywords": ["项目甲", "项目甲", "合同乙"],
                        "timeout": 45,
                    },
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("QWEN_API_KEY", "test-qwen-key")

    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _Response()

    import requests

    monkeypatch.setattr(requests, "post", fake_post)
    transcription_registry._reset_for_tests()
    plugins_module._reset_plugin_managers_for_tests()

    manager = PluginManager()
    manager.discover_and_load()
    assert manager._plugins["qwen-stt"].enabled is True, manager._plugins[
        "qwen-stt"
    ].error
    assert transcription_registry.get_provider("custom_api") is not None

    # Keep public dispatch on the provider registered by the real manager above.
    monkeypatch.setattr(plugins_module, "_ensure_plugins_discovered", lambda *a, **k: None)
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"RIFF" + b"\0" * 64)

    result = transcription_tools.transcribe_audio(str(audio), source="gateway")

    assert result == {
        "success": True,
        "transcript": "项目名称识别成功",
        "provider": "custom_api",
    }
    assert len(calls) == 1
    assert calls[0]["url"] == (
        "https://dashscope.aliyuncs.com/api/v1"
        "/services/aigc/multimodal-generation/generation"
    )
    assert calls[0]["headers"] == {
        "Authorization": "Bearer test-qwen-key",
        "Content-Type": "application/json",
        "X-DashScope-SSE": "disable",
    }
    assert calls[0]["timeout"] == 45.0
    assert calls[0]["json"]["model"] == "qwen-audio-3.0-asr-flash"
    messages = calls[0]["json"]["input"]["messages"]
    assert messages[0] == {
        "role": "user",
        "content": [
            {
                "type": "input_text",
                "text": "施工术语提示\n关键词：项目甲、合同乙",
            }
        ],
    }
    assert messages[1]["content"][0]["type"] == "input_audio"
    assert calls[0]["json"]["parameters"] == {"format": "wav"}

    transcription_registry._reset_for_tests()
