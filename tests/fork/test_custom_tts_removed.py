"""Regression contract: the removed custom_api TTS is no longer core-owned."""

from __future__ import annotations

import inspect

from agent import tts_registry
from agent.tts_provider import TTSProvider
import tools.tts_tool as tts_tool
from tools.tts_tool import BUILTIN_TTS_PROVIDERS


class _LegacyNameProvider(TTSProvider):
    @property
    def name(self) -> str:
        return "custom_api"

    def synthesize(self, text: str, output_path: str, **kwargs) -> str:
        return output_path


def test_custom_api_tts_name_is_not_reserved_by_core():
    assert "custom_api" not in BUILTIN_TTS_PROVIDERS
    assert "custom_api" not in inspect.getsource(tts_tool)

    tts_registry._reset_for_tests()
    provider = _LegacyNameProvider()
    tts_registry.register_provider(provider)
    try:
        assert tts_registry.get_provider("custom_api") is provider
    finally:
        tts_registry._reset_for_tests()
