"""Qwen/DashScope and compatible custom HTTP transcription plugin.

The provider intentionally keeps the historical ``custom_api`` registration
name and ``stt.custom_api`` configuration namespace so existing installations
continue to work while the vendor-specific HTTP behavior lives outside the
core transcription dispatcher.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from agent.transcription_provider import TranscriptionProvider

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com"
DEFAULT_MODEL = "qwen-audio-3.0-asr-flash"
DEFAULT_ENDPOINT = "/api/v1/services/aigc/multimodal-generation/generation"
DEFAULT_MODE = "dashscope_multimodal"
DEFAULT_TIMEOUT = 120.0
_VALID_MODES = {"chat_completions", "dashscope_multimodal", "multipart"}


def _get_env_value(name: str, default: Any = None) -> Any:
    try:
        from hermes_cli.config import get_env_value
    except ImportError:
        return os.getenv(name, default)
    value = get_env_value(name)
    return default if value is None else value


def _load_stt_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        section = (load_config() or {}).get("stt") or {}
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def _resolve_language(stt_config: Dict[str, Any], custom_cfg: Dict[str, Any]) -> str:
    candidates = [
        custom_cfg.get("language"),
        stt_config.get("language"),
        os.getenv("HERMES_LOCAL_STT_LANGUAGE"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _resolve_custom_api_config(
    stt_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve the historical ``stt.custom_api`` configuration contract."""
    if stt_config is None:
        stt_config = _load_stt_config()
    custom_cfg = stt_config.get("custom_api", {}) if isinstance(stt_config, dict) else {}
    if not isinstance(custom_cfg, dict):
        custom_cfg = {}

    base_url = str(
        custom_cfg.get("base_url")
        or os.getenv("STT_CUSTOM_API_BASE_URL")
        or DEFAULT_BASE_URL
    ).strip().rstrip("/")
    endpoint = str(
        custom_cfg.get("endpoint")
        or os.getenv("STT_CUSTOM_API_ENDPOINT")
        or DEFAULT_ENDPOINT
    ).strip()
    if endpoint and not endpoint.startswith("/"):
        endpoint = f"/{endpoint}"

    api_key = str(custom_cfg.get("api_key") or "").strip()
    api_key_env = str(custom_cfg.get("api_key_env") or "QWEN_API_KEY").strip()
    if not api_key and api_key_env:
        api_key = str(_get_env_value(api_key_env) or "").strip()

    model = str(
        custom_cfg.get("model")
        or os.getenv("STT_CUSTOM_API_MODEL")
        or DEFAULT_MODEL
    ).strip()
    mode = str(
        custom_cfg.get("mode") or os.getenv("STT_CUSTOM_API_MODE") or ""
    ).strip()
    if not mode:
        if endpoint.rstrip("/") == DEFAULT_ENDPOINT:
            mode = DEFAULT_MODE
        elif endpoint.rstrip("/") == "/chat/completions":
            mode = "chat_completions"
        else:
            mode = "multipart"

    response_format = str(
        custom_cfg.get("response_format")
        or os.getenv("STT_CUSTOM_API_RESPONSE_FORMAT")
        or "json"
    ).strip()
    prompt_value = custom_cfg.get("prompt")
    if prompt_value is None:
        prompt_value = os.getenv("STT_CUSTOM_API_PROMPT")
    if prompt_value is None:
        prompt_value = "" if mode == "dashscope_multimodal" else "请将这段音频转写为文本。"
    prompt = str(prompt_value).strip()

    keywords_value = custom_cfg.get("keywords") or []
    if isinstance(keywords_value, str):
        keywords_value = [keywords_value]
    elif not isinstance(keywords_value, (list, tuple, set)):
        keywords_value = []
    keywords = list(
        dict.fromkeys(
            keyword
            for item in keywords_value
            if (keyword := str(item).strip())
        )
    )

    timeout_value = custom_cfg.get("timeout")
    if timeout_value is None or timeout_value == "":
        timeout_value = os.getenv("STT_CUSTOM_API_TIMEOUT")
    if timeout_value is None or timeout_value == "":
        timeout_value = DEFAULT_TIMEOUT
    try:
        timeout = float(timeout_value)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    return {
        "base_url": base_url,
        "endpoint": endpoint,
        "api_key": api_key,
        "api_key_env": api_key_env,
        "model": model,
        "mode": mode,
        "response_format": response_format,
        "language": _resolve_language(stt_config, custom_cfg),
        "prompt": prompt,
        "keywords": keywords,
        "timeout": timeout,
    }


def _mime_type(path: Path) -> tuple[str, str]:
    audio_format = path.suffix.lower().lstrip(".") or "wav"
    mime_type = {
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "mpeg": "audio/mpeg",
        "ogg": "audio/ogg",
        "oga": "audio/ogg",
        "m4a": "audio/mp4",
        "mp4": "audio/mp4",
        "aac": "audio/aac",
        "flac": "audio/flac",
        "webm": "audio/webm",
    }.get(audio_format, f"audio/{audio_format}")
    return audio_format, mime_type


def _extract_transcript_text(transcription: Any) -> str:
    text: Optional[str] = None
    if isinstance(transcription, str):
        text = transcription.strip()
    if text is None and hasattr(transcription, "text"):
        value = getattr(transcription, "text")
        if isinstance(value, str):
            text = value.strip()
    if text is None and isinstance(transcription, dict):
        value = transcription.get("text")
        if isinstance(value, str):
            text = value.strip()
    if text is None:
        text = str(transcription).strip()

    match = re.match(
        r"\s*language\s+[\w.-]+(?:\s*<audio_language>[^<]*</audio_language>)?\s*<asr_text>\s*(?P<text>.*)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return match.group("text").strip() if match else text


def _transcribe_custom_api(
    file_path: str,
    model_name: str,
    *,
    resolved_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Transcribe with the compatible/DashScope HTTP behavior kept by the Fork."""
    cfg = resolved_config or _resolve_custom_api_config()
    if not cfg["base_url"]:
        return {"success": False, "transcript": "", "error": "stt.custom_api.base_url not set"}
    if not cfg["api_key"]:
        return {
            "success": False,
            "transcript": "",
            "error": f"stt.custom_api.api_key not set and {cfg['api_key_env']} is unavailable",
        }
    if cfg["mode"] not in _VALID_MODES:
        return {
            "success": False,
            "transcript": "",
            "error": (
                f"Unsupported stt.custom_api.mode {cfg['mode']!r}; expected one of: "
                + ", ".join(sorted(_VALID_MODES))
            ),
        }

    try:
        import requests

        audio_path = Path(file_path)
        audio_format, mime_type = _mime_type(audio_path)
        if cfg["mode"] == "chat_completions":
            audio_b64 = base64.b64encode(audio_path.read_bytes()).decode("ascii")
            audio_item: Dict[str, Any] = {
                "type": "input_audio",
                "input_audio": {"data": f"data:{mime_type};base64,{audio_b64}"},
            }
            if cfg["prompt"]:
                audio_item["text"] = cfg["prompt"]
            payload: Dict[str, Any] = {
                "model": model_name,
                "messages": [{"role": "user", "content": [audio_item]}],
                "stream": False,
            }
            asr_options: Dict[str, Any] = {"enable_itn": False}
            if cfg["language"]:
                asr_options["language"] = cfg["language"]
            payload["asr_options"] = asr_options
            response = requests.post(
                f"{cfg['base_url']}{cfg['endpoint']}",
                headers={
                    "Authorization": f"Bearer {cfg['api_key']}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=cfg["timeout"],
            )
        elif cfg["mode"] == "dashscope_multimodal":
            audio_b64 = base64.b64encode(audio_path.read_bytes()).decode("ascii")
            messages: list[Dict[str, Any]] = []
            context_parts = []
            if cfg["prompt"]:
                context_parts.append(cfg["prompt"])
            if cfg["keywords"]:
                context_parts.append(f"关键词：{'、'.join(cfg['keywords'])}")
            if context_parts:
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "\n".join(context_parts)}
                        ],
                    }
                )
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": f"data:{mime_type};base64,{audio_b64}"
                            },
                        }
                    ],
                }
            )
            payload = {
                "model": model_name,
                "input": {"messages": messages},
                "parameters": {"format": audio_format},
            }
            response = requests.post(
                f"{cfg['base_url']}{cfg['endpoint']}",
                headers={
                    "Authorization": f"Bearer {cfg['api_key']}",
                    "Content-Type": "application/json",
                    "X-DashScope-SSE": "disable",
                },
                json=payload,
                timeout=cfg["timeout"],
            )
        else:
            data: Dict[str, str] = {"model": model_name}
            if cfg["response_format"]:
                data["response_format"] = cfg["response_format"]
            if cfg["language"]:
                data["language"] = cfg["language"]
            with open(file_path, "rb") as audio_file:
                response = requests.post(
                    f"{cfg['base_url']}{cfg['endpoint']}",
                    headers={"Authorization": f"Bearer {cfg['api_key']}"},
                    files={"file": (audio_path.name, audio_file)},
                    data=data,
                    timeout=cfg["timeout"],
                )

        if response.status_code != 200:
            try:
                err_body = response.json()
                detail = err_body.get("error", {}).get("message", "") or response.text[:300]
            except Exception:
                detail = response.text[:300]
            return {
                "success": False,
                "transcript": "",
                "error": f"Custom STT API error (HTTP {response.status_code}): {detail}",
            }

        try:
            result: Any = response.json()
        except Exception:
            result = response.text
        transcript_text = ""
        if isinstance(result, dict):
            output = result.get("output")
            if isinstance(output, dict) and isinstance(output.get("text"), str):
                transcript_text = output["text"].strip()
            choices = result.get("choices", [])
            if not transcript_text and choices:
                message = choices[0].get("message", {})
                content = message.get("content", "")
                if isinstance(content, list):
                    parts = []
                    for item in content:
                        if isinstance(item, dict):
                            parts.append(str(item.get("text") or item.get("transcript") or ""))
                        else:
                            parts.append(str(item))
                    transcript_text = "".join(parts).strip()
                else:
                    transcript_text = str(content).strip()
        if not transcript_text:
            transcript_text = _extract_transcript_text(result)
        if not transcript_text:
            return {
                "success": False,
                "transcript": "",
                "error": "Custom STT API returned empty transcript",
            }

        logger.info(
            "Transcribed %s via custom STT plugin (%s, %d chars)",
            audio_path.name,
            model_name,
            len(transcript_text),
        )
        return {
            "success": True,
            "transcript": transcript_text,
            "provider": "custom_api",
        }
    except PermissionError:
        return {
            "success": False,
            "transcript": "",
            "error": f"Permission denied: {file_path}",
        }
    except Exception as exc:
        logger.error("Custom STT plugin transcription failed: %s", exc, exc_info=True)
        return {
            "success": False,
            "transcript": "",
            "error": f"Custom STT API transcription failed: {exc}",
        }


class QwenSTTProvider(TranscriptionProvider):
    """Plugin provider retaining the historical ``custom_api`` identifier."""

    @property
    def name(self) -> str:
        return "custom_api"

    @property
    def display_name(self) -> str:
        return "Qwen / Custom STT API"

    def is_available(self) -> bool:
        cfg = _resolve_custom_api_config()
        return bool(cfg["base_url"] and cfg["api_key"])

    def list_models(self):
        model = _resolve_custom_api_config().get("model") or DEFAULT_MODEL
        return [{"id": model, "name": model}]

    def transcribe(
        self,
        file_path: str,
        *,
        model: Optional[str] = None,
        language: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        cfg = _resolve_custom_api_config()
        if model:
            cfg["model"] = model
        if language:
            cfg["language"] = language
        # The legacy built-in intentionally used stt.custom_api.prompt rather
        # than the generic top-level hook prompt. Preserve that behavior.
        return _transcribe_custom_api(
            file_path,
            str(cfg["model"] or DEFAULT_MODEL),
            resolved_config=cfg,
        )


def register(ctx) -> None:
    """Register the historical provider name through Hermes' official hook."""
    ctx.register_transcription_provider(QwenSTTProvider())
