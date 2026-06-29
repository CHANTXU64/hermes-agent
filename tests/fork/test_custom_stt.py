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
