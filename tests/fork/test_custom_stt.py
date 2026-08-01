"""Fork-owned custom STT provider regressions."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.usefixtures("disable_lazy_stt_install")


@pytest.fixture
def disable_lazy_stt_install():
    with patch("tools.transcription_tools._try_lazy_install_stt", return_value=False):
        yield


def test_custom_api_when_configured():
    from tools.transcription_tools import _get_provider
    stt_config = {
        "provider": "custom_api",
        "custom_api": {
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key": "sk-test",
            "model": "qwen3-asr",
        },
    }
    assert _get_provider(stt_config) == "custom_api"

def test_custom_api_requires_key_and_base_url():
    from tools.transcription_tools import _get_provider
    assert _get_provider({"provider": "custom_api", "custom_api": {}}) == "none"

def test_successful_text_response(tmp_path):
    from tools.transcription_tools import _transcribe_custom_api

    audio_file = tmp_path / "test.wav"
    audio_file.write_bytes(b"fake audio")
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"text": "  你好 Hermes  "}
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        captured["data"] = kwargs["data"]
        return response

    cfg = {
        "provider": "custom_api",
        "custom_api": {
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/",
            "api_key": "dashscope-key",
            "model": "qwen3-asr",
            "endpoint": "audio/transcriptions",
            "response_format": "json",
            "language": "zh",
        },
    }
    with patch("tools.transcription_tools._load_stt_config", return_value=cfg), \
         patch("requests.post", side_effect=fake_post):
        result = _transcribe_custom_api(str(audio_file), "qwen3-asr")

    assert result == {"success": True, "transcript": "你好 Hermes", "provider": "custom_api"}
    assert captured["url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/audio/transcriptions"
    assert captured["headers"]["Authorization"] == "Bearer dashscope-key"
    assert captured["data"] == {"model": "qwen3-asr", "response_format": "json", "language": "zh"}

def test_successful_choices_response(tmp_path):
    from tools.transcription_tools import _transcribe_custom_api

    audio_file = tmp_path / "test.wav"
    audio_file.write_bytes(b"fake audio")
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"choices": [{"message": {"content": "选择格式文本"}}]}
    cfg = {
        "custom_api": {
            "base_url": "https://example.com/v1",
            "api_key": "key",
            "response_format": "",
        },
    }
    with patch("tools.transcription_tools._load_stt_config", return_value=cfg), \
         patch("requests.post", return_value=response):
        result = _transcribe_custom_api(str(audio_file), "qwen3-asr")

    assert result["success"] is True
    assert result["transcript"] == "选择格式文本"

def test_successful_chat_completions_response(tmp_path):
    from tools.transcription_tools import _transcribe_custom_api

    audio_file = tmp_path / "test.ogg"
    audio_file.write_bytes(b"fake audio")
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"choices": [{"message": {"content": "聊天接口文本"}}]}
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        captured["url"] = url
        return response

    cfg = {
        "custom_api": {
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key": "key",
            "model": "qwen3-asr-flash-2026-02-10",
            "endpoint": "/chat/completions",
            "mode": "chat_completions",
            "language": "zh",
            "prompt": "术语提示",
        },
    }
    with patch("tools.transcription_tools._load_stt_config", return_value=cfg), \
         patch("requests.post", side_effect=fake_post):
        result = _transcribe_custom_api(str(audio_file), "qwen3-asr-flash-2026-02-10")

    assert result["success"] is True
    assert result["transcript"] == "聊天接口文本"
    assert captured["url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    assert captured["json"]["model"] == "qwen3-asr-flash-2026-02-10"
    content = captured["json"]["messages"][0]["content"]
    assert captured["json"] == {
        "model": "qwen3-asr-flash-2026-02-10",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": content[0]["input_audio"]["data"]},
                        "text": "术语提示",
                    }
                ],
            }
        ],
        "stream": False,
        "asr_options": {"enable_itn": False, "language": "zh"},
    }
    assert len(content) == 1
    assert content[0]["type"] == "input_audio"
    assert content[0]["input_audio"]["data"].startswith("data:audio/ogg;base64,")
    assert captured["json"]["asr_options"] == {"enable_itn": False, "language": "zh"}

def test_default_config_recognizes_and_resolves_latest_qwen_custom_stt(monkeypatch):
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    from tools.transcription_tools import _resolve_custom_api_config

    for name in (
        "STT_CUSTOM_API_BASE_URL",
        "STT_CUSTOM_API_MODEL",
        "STT_CUSTOM_API_ENDPOINT",
        "STT_CUSTOM_API_MODE",
        "STT_CUSTOM_API_RESPONSE_FORMAT",
        "STT_CUSTOM_API_PROMPT",
        "STT_CUSTOM_API_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)

    custom_schema = DEFAULT_CONFIG["stt"]["custom_api"]
    assert {
        "base_url",
        "api_key",
        "api_key_env",
        "model",
        "endpoint",
        "mode",
        "response_format",
        "language",
        "prompt",
        "timeout",
    } <= custom_schema.keys()

    cfg = _resolve_custom_api_config(DEFAULT_CONFIG["stt"])
    assert cfg["base_url"] == "https://dashscope.aliyuncs.com"
    assert cfg["api_key_env"] == "QWEN_API_KEY"
    assert cfg["model"] == "qwen-audio-3.0-asr-flash"
    assert cfg["endpoint"] == "/api/v1/services/aigc/multimodal-generation/generation"
    assert cfg["mode"] == "dashscope_multimodal"
    assert cfg["response_format"] == "json"
    assert cfg["prompt"] == ""
    assert cfg["timeout"] == 120.0


def test_loaded_legacy_endpoint_without_mode_still_infers_multipart(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "stt:\n"
        "  provider: custom_api\n"
        "  custom_api:\n"
        "    base_url: https://legacy.example/v1\n"
        "    api_key: legacy-key\n"
        "    model: legacy-asr\n"
        "    endpoint: /audio/transcriptions\n",
        encoding="utf-8",
    )

    from hermes_cli.config import load_config
    from tools.transcription_tools import _load_stt_config, _resolve_custom_api_config

    loaded = load_config()
    stt_config = _load_stt_config()
    assert stt_config == loaded["stt"]

    cfg = _resolve_custom_api_config(stt_config)
    assert cfg["endpoint"] == "/audio/transcriptions"
    assert cfg["mode"] == "multipart"
    assert cfg["model"] == "legacy-asr"


def test_loaded_custom_stt_uses_environment_before_qwen_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "stt:\n  provider: custom_api\n",
        encoding="utf-8",
    )
    env = {
        "STT_CUSTOM_API_BASE_URL": "https://env.example/v1/",
        "STT_CUSTOM_API_MODEL": "env-asr",
        "STT_CUSTOM_API_ENDPOINT": "chat/completions",
        "STT_CUSTOM_API_MODE": "chat_completions",
        "STT_CUSTOM_API_RESPONSE_FORMAT": "verbose_json",
        "STT_CUSTOM_API_PROMPT": "environment prompt",
        "STT_CUSTOM_API_TIMEOUT": "37.5",
        "QWEN_API_KEY": "environment-key",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    from hermes_cli.config import load_config
    from tools.transcription_tools import _load_stt_config, _resolve_custom_api_config

    loaded = load_config()
    stt_config = _load_stt_config()
    assert stt_config == loaded["stt"]

    cfg = _resolve_custom_api_config(stt_config)
    assert cfg == {
        "base_url": "https://env.example/v1",
        "endpoint": "/chat/completions",
        "api_key": "environment-key",
        "api_key_env": "QWEN_API_KEY",
        "model": "env-asr",
        "mode": "chat_completions",
        "response_format": "verbose_json",
        "language": "en",
        "prompt": "environment prompt",
        "timeout": 37.5,
    }


def test_custom_stt_config_overrides_environment_including_timeout(monkeypatch):
    from tools.transcription_tools import _resolve_custom_api_config

    env = {
        "STT_CUSTOM_API_BASE_URL": "https://env.example",
        "STT_CUSTOM_API_MODEL": "env-model",
        "STT_CUSTOM_API_ENDPOINT": "/env",
        "STT_CUSTOM_API_MODE": "multipart",
        "STT_CUSTOM_API_RESPONSE_FORMAT": "text",
        "STT_CUSTOM_API_PROMPT": "env prompt",
        "STT_CUSTOM_API_TIMEOUT": "99",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    cfg = _resolve_custom_api_config(
        {
            "custom_api": {
                "base_url": "https://config.example",
                "model": "config-model",
                "endpoint": "/chat/completions",
                "mode": "chat_completions",
                "response_format": "json",
                "prompt": "config prompt",
                "timeout": 12,
            }
        }
    )

    assert cfg["base_url"] == "https://config.example"
    assert cfg["model"] == "config-model"
    assert cfg["endpoint"] == "/chat/completions"
    assert cfg["mode"] == "chat_completions"
    assert cfg["response_format"] == "json"
    assert cfg["prompt"] == "config prompt"
    assert cfg["timeout"] == 12.0


def test_default_custom_api_preserves_empty_prompt_and_global_language():
    from tools.transcription_tools import _resolve_custom_api_config

    cfg = _resolve_custom_api_config(
        {
            "language": "fr",
            "custom_api": {
                "base_url": "https://dashscope.aliyuncs.com",
                "api_key": "key",
                "prompt": "",
            },
        }
    )

    assert cfg["language"] == "fr"
    assert cfg["prompt"] == ""


def test_default_custom_api_targets_latest_qwen_audio_model():
    from tools.transcription_tools import _resolve_custom_api_config

    stt_config = {
        "custom_api": {
            "base_url": "https://dashscope.aliyuncs.com",
            "api_key": "key",
        }
    }
    with patch.dict(
        "os.environ",
        {
            "STT_CUSTOM_API_MODEL": "",
            "STT_CUSTOM_API_ENDPOINT": "",
            "STT_CUSTOM_API_MODE": "",
        },
    ):
        cfg = _resolve_custom_api_config(stt_config)

    assert cfg["model"] == "qwen-audio-3.0-asr-flash"
    assert cfg["endpoint"] == "/api/v1/services/aigc/multimodal-generation/generation"
    assert cfg["mode"] == "dashscope_multimodal"


def test_successful_dashscope_multimodal_response(tmp_path):
    from tools.transcription_tools import _transcribe_custom_api

    audio_file = tmp_path / "test.ogg"
    audio_file.write_bytes(b"fake audio")
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "output": {
            "sentence": {"text": "新模型文本"},
            "text": "新模型文本",
        },
        "usage": {"duration": 2},
        "request_id": "request-1",
    }
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        captured["url"] = url
        return response

    cfg = {
        "custom_api": {
            "base_url": "https://dashscope.aliyuncs.com",
            "api_key": "key",
            "model": "qwen-audio-3.0-asr-flash",
            "endpoint": "/api/v1/services/aigc/multimodal-generation/generation",
            "mode": "dashscope_multimodal",
            "prompt": "Convert this audio to text.",
        },
    }
    with patch("tools.transcription_tools._load_stt_config", return_value=cfg), \
         patch("requests.post", side_effect=fake_post):
        result = _transcribe_custom_api(str(audio_file), "qwen-audio-3.0-asr-flash")

    assert result == {"success": True, "transcript": "新模型文本", "provider": "custom_api"}
    assert captured["url"] == (
        "https://dashscope.aliyuncs.com"
        "/api/v1/services/aigc/multimodal-generation/generation"
    )
    assert captured["headers"] == {
        "Authorization": "Bearer key",
        "Content-Type": "application/json",
        "X-DashScope-SSE": "disable",
    }
    content = captured["json"]["input"]["messages"][0]["content"]
    assert captured["json"] == {
        "model": "qwen-audio-3.0-asr-flash",
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": content[0]["input_audio"]["data"]},
                        }
                    ],
                }
            ]
        },
        "parameters": {"format": "ogg"},
    }
    assert content[0]["input_audio"]["data"].startswith("data:audio/ogg;base64,")


def test_invalid_custom_api_mode_fails_before_request(tmp_path):
    from tools.transcription_tools import _transcribe_custom_api

    audio_file = tmp_path / "test.ogg"
    audio_file.write_bytes(b"fake audio")
    cfg = {
        "custom_api": {
            "base_url": "https://dashscope.aliyuncs.com",
            "api_key": "key",
            "mode": "dashscope_multimoda",
        }
    }

    with patch("tools.transcription_tools._load_stt_config", return_value=cfg), \
         patch("requests.post") as post:
        result = _transcribe_custom_api(str(audio_file), "qwen-audio-3.0-asr-flash")

    assert result == {
        "success": False,
        "transcript": "",
        "error": (
            "Unsupported stt.custom_api.mode 'dashscope_multimoda'; expected one of: "
            "chat_completions, dashscope_multimodal, multipart"
        ),
    }
    post.assert_not_called()


def test_dispatches_to_custom_api(tmp_path):
    audio_file = tmp_path / "test.ogg"
    audio_file.write_bytes(b"fake audio")
    stt_config = {
        "provider": "custom_api",
        "custom_api": {"model": "qwen3-asr", "base_url": "https://example.com/v1", "api_key": "key"},
    }

    with patch("tools.transcription_tools._load_stt_config", return_value=stt_config), \
         patch("tools.transcription_tools._get_provider", return_value="custom_api"), \
         patch("tools.transcription_tools._transcribe_custom_api", return_value={"success": True, "transcript": "hi"}) as mock_custom:
        from tools.transcription_tools import transcribe_audio
        result = transcribe_audio(str(audio_file))

    assert result["success"] is True
    mock_custom.assert_called_once_with(str(audio_file), "qwen3-asr")

def test_explicit_custom_api_sees_dotenv_env_key():
    from tools import transcription_tools as tt

    stt_config = {
        "enabled": True,
        "provider": "custom_api",
        "custom_api": {
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key_env": "QWEN_API_KEY",
        },
    }
    with patch("hermes_cli.config.load_env", return_value={"QWEN_API_KEY": "dotenv-secret"}):
        assert tt._get_provider(stt_config) == "custom_api"

def test_default_custom_api_uses_qwen_dotenv_key():
    from tools import transcription_tools as tt

    stt_config = {
        "enabled": True,
        "provider": "custom_api",
        "custom_api": {
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        },
    }
    with patch("hermes_cli.config.load_env", return_value={"QWEN_API_KEY": "dotenv-secret"}):
        assert tt._get_provider(stt_config) == "custom_api"

def test_transcribe_custom_api_forwards_dotenv_env_key():
    from tools import transcription_tools as tt

    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["headers"] = kwargs.get("headers", {})
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"text": "hello"}
        return response

    cfg = {
        "custom_api": {
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key_env": "QWEN_API_KEY",
            "model": "qwen3-asr-flash-2026-02-10",
            "endpoint": "/audio/transcriptions",
            "mode": "multipart",
        }
    }
    with patch.object(tt, "_load_stt_config", return_value=cfg),              patch("hermes_cli.config.load_env", return_value={"QWEN_API_KEY": "qwen-dotenv-key"}),              patch("requests.post", side_effect=fake_post),              patch("builtins.open", MagicMock()):
        result = tt._transcribe_custom_api("/tmp/fake.mp3", "qwen3-asr")

    assert result["success"] is True
    assert captured["headers"]["Authorization"] == "Bearer qwen-dotenv-key"
