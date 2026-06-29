"""Fork-owned MLX Whisper provider selection regression."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("disable_lazy_stt_install")


@pytest.fixture
def disable_lazy_stt_install():
    with patch("tools.transcription_tools._try_lazy_install_stt", return_value=False):
        yield


def test_auto_detect_prefers_mlx_whisper_on_macos():
    """Fork: MLX Whisper is a local macOS fallback before cloud providers."""
    with patch("platform.system", return_value="Darwin"), \
         patch("tools.transcription_tools._HAS_FASTER_WHISPER", False), \
         patch("tools.transcription_tools._HAS_MLX_WHISPER", True), \
         patch("tools.transcription_tools._has_local_command", return_value=False):
        from tools.transcription_tools import _get_provider
        assert _get_provider({}) == "mlx_whisper"
