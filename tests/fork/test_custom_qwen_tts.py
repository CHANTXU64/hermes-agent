import base64
import json
from types import SimpleNamespace

import pytest

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


class _StreamingResponse(_FakeResponse):
    def __init__(self, chunks, **kwargs):
        self._chunks = list(chunks)
        self.closed = False
        super().__init__(content=b"".join(self._chunks), **kwargs)

    def iter_content(self, chunk_size=65536):
        del chunk_size
        yield from self._chunks

    def close(self):
        self.closed = True


def test_custom_api_tts_posts_openai_compatible_payload(monkeypatch, tmp_path):
    calls = []

    def fake_post(url, *, headers, json, timeout, stream=False):
        calls.append({
            "url": url,
            "headers": headers,
            "json": json,
            "timeout": timeout,
            "stream": stream,
        })
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
        "stream": True,
    }]


def test_custom_api_tts_accepts_json_base64_audio(monkeypatch, tmp_path):
    encoded = base64.b64encode(b"json-audio").decode("ascii")
    response_data = {"data": {"audio": encoded}}
    response_content = json.dumps(response_data).encode("utf-8")

    def fake_post(url, *, headers, json, timeout, stream=False):
        assert stream is True
        return _FakeResponse(
            headers={"Content-Type": "application/json"},
            content=response_content,
            json_data=response_data,
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
    response_data = {
        "output": {"audio": {"url": "https://audio.example/out.wav"}},
    }
    response_content = json.dumps(response_data).encode("utf-8")

    def fake_post(url, *, headers, json, timeout, stream=False):
        assert stream is True
        calls.append(("post", url, json))
        return _FakeResponse(
            headers={"Content-Type": "application/json"},
            content=response_content,
            json_data=response_data,
        )

    def fake_get(url, *, timeout, stream=False):
        assert stream is True
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


def test_custom_api_tts_rejects_oversized_audio_response(monkeypatch, tmp_path):
    response = _StreamingResponse(
        [b"12345", b"6789"],
        headers={"Content-Type": "audio/mpeg"},
    )
    request_stream_values = []

    def fake_post(url, *, headers, json, timeout, stream=False):
        del url, headers, json, timeout
        request_stream_values.append(stream)
        return response

    monkeypatch.setattr(tts_tool, "TTS_RESPONSE_BODY_LIMIT_BYTES", 8)
    monkeypatch.setattr(tts_tool, "get_env_value", lambda name, default=None: default)
    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))

    with pytest.raises(RuntimeError, match="Custom TTS API response exceeds 8 bytes"):
        tts_tool._generate_custom_api_tts(
            "hello",
            str(tmp_path / "speech.mp3"),
            {
                "custom_api": {
                    "base_url": "https://example.test/v1",
                    "api_key": "test-key",
                    "model": "custom-tts",
                    "voice": "voice-a",
                },
            },
        )

    assert request_stream_values == [True]
    assert response.closed is True
    assert not (tmp_path / "speech.mp3").exists()


def test_custom_api_tts_rejects_oversized_json_response(monkeypatch, tmp_path):
    response = _StreamingResponse(
        [b'{"audio":"eA==",', b'"padding":"xxxxxxxx"}'],
        headers={"Content-Type": "application/json"},
        json_data={"audio": base64.b64encode(b"x").decode("ascii")},
    )

    def fake_post(url, *, headers, json, timeout, stream=False):
        del url, headers, json, timeout, stream
        return response

    monkeypatch.setattr(tts_tool, "TTS_RESPONSE_BODY_LIMIT_BYTES", 16)
    monkeypatch.setattr(tts_tool, "get_env_value", lambda name, default=None: default)
    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))

    with pytest.raises(RuntimeError, match="Custom TTS API response exceeds 16 bytes"):
        tts_tool._generate_custom_api_tts(
            "hello",
            str(tmp_path / "speech.mp3"),
            {
                "custom_api": {
                    "base_url": "https://example.test/v1",
                    "api_key": "test-key",
                    "model": "custom-tts",
                    "voice": "voice-a",
                },
            },
        )

    assert response.closed is True
    assert not (tmp_path / "speech.mp3").exists()


def test_custom_api_tts_http_error_preserves_bounded_text_detail(monkeypatch, tmp_path):
    response = _StreamingResponse(
        [b"rate limited"],
        status_code=429,
        headers={"Content-Type": "text/plain"},
    )

    def fake_post(url, *, headers, json, timeout, stream=False):
        del url, headers, json, timeout
        assert stream is True
        return response

    monkeypatch.setattr(tts_tool, "get_env_value", lambda name, default=None: default)
    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))

    with pytest.raises(RuntimeError) as exc_info:
        tts_tool._generate_custom_api_tts(
            "hello",
            str(tmp_path / "speech.mp3"),
            {
                "custom_api": {
                    "base_url": "https://example.test/v1",
                    "api_key": "test-key",
                    "model": "custom-tts",
                    "voice": "voice-a",
                },
            },
        )

    assert str(exc_info.value) == "Custom TTS API error (HTTP 429): rate limited"
    assert response.closed is True
    assert not (tmp_path / "speech.mp3").exists()


def test_custom_api_tts_http_error_extracts_top_level_json_message(monkeypatch, tmp_path):
    response_data = {"message": "request rejected"}
    response = _StreamingResponse(
        [json.dumps(response_data).encode("utf-8")],
        status_code=400,
        headers={"Content-Type": "application/json"},
        json_data=response_data,
    )

    def fake_post(url, *, headers, json, timeout, stream=False):
        del url, headers, json, timeout
        assert stream is True
        return response

    monkeypatch.setattr(tts_tool, "get_env_value", lambda name, default=None: default)
    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))

    with pytest.raises(RuntimeError) as exc_info:
        tts_tool._generate_custom_api_tts(
            "hello",
            str(tmp_path / "speech.mp3"),
            {
                "custom_api": {
                    "base_url": "https://example.test/v1",
                    "api_key": "test-key",
                    "model": "custom-tts",
                    "voice": "voice-a",
                },
            },
        )

    assert str(exc_info.value) == "Custom TTS API error (HTTP 400): request rejected"
    assert response.closed is True
    assert not (tmp_path / "speech.mp3").exists()


def test_custom_api_tts_dashscope_accepts_direct_audio_response(monkeypatch, tmp_path):
    response = _StreamingResponse(
        [b"wav-bytes"],
        headers={"Content-Type": "audio/wav"},
    )

    def fake_post(url, *, headers, json, timeout, stream=False):
        del url, headers, json, timeout
        assert stream is True
        return response

    monkeypatch.setattr(tts_tool, "get_env_value", lambda name, default=None: default)
    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))

    output_path = tmp_path / "speech.wav"
    result = tts_tool._generate_custom_api_tts(
        "hello",
        str(output_path),
        {
            "custom_api": {
                "base_url": "https://example.test/v1",
                "endpoint": "/generate",
                "mode": "dashscope_multimodal",
                "api_key": "test-key",
                "model": "qwen3-tts-flash",
                "voice": "Cherry",
            },
        },
    )

    assert result == str(output_path)
    assert output_path.read_bytes() == b"wav-bytes"
    assert response.closed is True


def test_custom_api_tts_rejects_oversized_downloaded_audio(monkeypatch, tmp_path):
    response = _StreamingResponse(
        [b'{"output":{"audio":{"url":"https://audio.example/out.wav"}}}'],
        headers={"Content-Type": "application/json"},
        json_data={"output": {"audio": {"url": "https://audio.example/out.wav"}}},
    )
    audio_response = _StreamingResponse(
        [b"a" * 64, b"b" * 65],
        headers={"Content-Type": "audio/wav"},
    )
    request_stream_values = []

    def fake_post(url, *, headers, json, timeout, stream=False):
        del url, headers, json, timeout
        request_stream_values.append(("post", stream))
        return response

    def fake_get(url, *, timeout, stream=False):
        del url, timeout
        request_stream_values.append(("get", stream))
        return audio_response

    monkeypatch.setattr(tts_tool, "TTS_RESPONSE_BODY_LIMIT_BYTES", 128)
    monkeypatch.setattr(tts_tool, "get_env_value", lambda name, default=None: default)
    monkeypatch.setitem(
        __import__("sys").modules,
        "requests",
        SimpleNamespace(post=fake_post, get=fake_get),
    )

    with pytest.raises(RuntimeError, match="Custom TTS API audio download response exceeds 128 bytes"):
        tts_tool._generate_custom_api_tts(
            "hello",
            str(tmp_path / "speech.mp3"),
            {
                "custom_api": {
                    "base_url": "https://example.test/v1",
                    "endpoint": "/generate",
                    "mode": "dashscope_multimodal",
                    "api_key": "test-key",
                    "model": "qwen3-tts-flash",
                    "voice": "Cherry",
                },
            },
        )

    assert request_stream_values == [("post", True), ("get", True)]
    assert response.closed is True
    assert audio_response.closed is True
    assert not (tmp_path / "speech.mp3").exists()
