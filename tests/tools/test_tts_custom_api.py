import base64
import json
from types import SimpleNamespace

import tools.tts_tool as tts_tool


class _FakeResponse:
    def __init__(self, *, status_code=200, content=b"audio", headers=None, json_data=None, text=""):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"Content-Type": "audio/mpeg"}
        self._json_data = json_data
        self.text = text

    def json(self):
        if self._json_data is None:
            raise ValueError("not json")
        return self._json_data


def test_custom_api_tts_posts_openai_compatible_payload(monkeypatch, tmp_path):
    calls = []

    def fake_post(url, *, headers, json, timeout):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return _FakeResponse(content=b"mp3-bytes")

    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {
        "provider": "custom_api",
        "custom_api": {
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "endpoint": "/audio/speech",
            "model": "qwen-tts-latest",
            "voice": "Cherry",
            "api_key_env": "QWEN_API_KEY",
            "timeout": 12,
        },
    })
    monkeypatch.setattr(tts_tool, "get_env_value", lambda name, default=None: "secret" if name == "QWEN_API_KEY" else default)
    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))

    output_path = tmp_path / "speech.mp3"
    result = json.loads(tts_tool.text_to_speech_tool("你好", str(output_path)))

    assert result["success"] is True
    assert result["provider"] == "custom_api"
    assert output_path.read_bytes() == b"mp3-bytes"
    assert calls == [{
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/audio/speech",
        "headers": {
            "Authorization": "Bearer secret",
            "Content-Type": "application/json",
        },
        "json": {
            "model": "qwen-tts-latest",
            "input": "你好",
            "voice": "Cherry",
            "response_format": "mp3",
        },
        "timeout": 12.0,
    }]


def test_custom_api_tts_accepts_json_base64_audio(monkeypatch, tmp_path):
    encoded = base64.b64encode(b"json-audio").decode("ascii")

    def fake_post(url, *, headers, json, timeout):
        return _FakeResponse(
            headers={"Content-Type": "application/json"},
            content=b'{"data":{"audio":"..."}}',
            json_data={"data": {"audio": encoded}},
        )

    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {
        "provider": "custom_api",
        "custom_api": {
            "base_url": "https://example.test/v1",
            "api_key": "inline-secret",
            "model": "custom-tts",
            "voice_id": "voice-a",
            "response_format": "wav",
        },
    })
    monkeypatch.setattr(tts_tool, "get_env_value", lambda name, default=None: default)
    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))

    output_path = tmp_path / "speech.wav"
    result = json.loads(tts_tool.text_to_speech_tool("hello", str(output_path)))

    assert result["success"] is True
    assert output_path.read_bytes() == b"json-audio"


def test_custom_api_tts_dashscope_multimodal_downloads_audio_url(monkeypatch, tmp_path):
    calls = []

    def fake_post(url, *, headers, json, timeout):
        calls.append(("post", url, json))
        return _FakeResponse(
            headers={"Content-Type": "application/json"},
            content=b"{}",
            json_data={"output": {"audio": {"url": "https://audio.example/out.wav"}}},
        )

    def fake_get(url, *, timeout):
        calls.append(("get", url, timeout))
        return _FakeResponse(content=b"wav-bytes", headers={"Content-Type": "audio/wav"})

    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {
        "provider": "custom_api",
        "custom_api": {
            "base_url": "https://dashscope.aliyuncs.com/api/v1",
            "endpoint": "/services/aigc/multimodal-generation/generation",
            "api_key_env": "QWEN_API_KEY",
            "model": "qwen3-tts-flash",
            "voice": "Cherry",
            "language_type": "Chinese",
        },
    })
    monkeypatch.setattr(tts_tool, "get_env_value", lambda name, default=None: "secret" if name == "QWEN_API_KEY" else default)
    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post, get=fake_get))

    output_path = tmp_path / "speech.mp3"
    result = json.loads(tts_tool.text_to_speech_tool("你好", str(output_path)))

    assert result["success"] is True
    assert output_path.read_bytes() == b"wav-bytes"
    assert calls[0] == (
        "post",
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
        {
            "model": "qwen3-tts-flash",
            "input": {"text": "你好", "voice": "Cherry", "language_type": "Chinese"},
        },
    )
    assert calls[1] == ("get", "https://audio.example/out.wav", 120.0)


def test_custom_api_tts_missing_key_is_configuration_error(monkeypatch, tmp_path):
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {
        "provider": "custom_api",
        "custom_api": {
            "base_url": "https://example.test/v1",
            "api_key_env": "QWEN_API_KEY",
            "model": "custom-tts",
            "voice": "Cherry",
        },
    })
    monkeypatch.setattr(tts_tool, "get_env_value", lambda name, default=None: default)

    output_path = tmp_path / "speech.mp3"
    result = json.loads(tts_tool.text_to_speech_tool("hello", str(output_path)))

    assert result["success"] is False
    assert "QWEN_API_KEY" in result["error"]
