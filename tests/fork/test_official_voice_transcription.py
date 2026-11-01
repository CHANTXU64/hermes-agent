"""Chosen official STT behavior; component proof is not full GatewayRunner proof."""
import re
from pathlib import Path

import pytest

from gateway.run_inbound_transcription import GatewayInboundTranscriptionMixin


def test_inbound_does_not_shadow_the_official_transcription_flow():
    source = (Path(__file__).resolve().parents[2] / "gateway/run_inbound.py").read_text()
    definitions = set(re.findall(r"^    (?:async )?def (\w+)\(", source, re.MULTILINE))
    assert not definitions.intersection({
        "_transcribe_one_clip", "_enrich_message_with_transcription",
        "_pending_event_audio_paths", "_transcribe_pending_audio_event_once",
        "_echo_pending_stt_transcripts_once", "_transcribe_and_echo_pending_voice",
        "_prepend_media_prefix", "_untranscribed_audio_note",
    }), "Old overrides bypass the official transcription flow"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["帮我查一下今天天气", "first line\nsecond line"])
async def test_successful_transcription_uses_the_official_plain_quote(text):
    fallback_calls = []
    primary_calls = []

    def primary(*args):
        primary_calls.append(args)
        return {"success": True, "transcript": text}

    def fallback(*args):
        fallback_calls.append(args)
        raise AssertionError("A successful transcript must not use fallback")

    result = await GatewayInboundTranscriptionMixin()._transcribe_one_clip("voice.ogg", primary, fallback)
    assert result == (text, f'"{text}"')
    assert primary_calls == [("voice.ogg", None, "gateway")]
    assert fallback_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
async def test_empty_transcription_retains_the_official_inaudible_note(text):
    result = await GatewayInboundTranscriptionMixin()._transcribe_one_clip(
        "voice.ogg", lambda *_: {"success": True, "transcript": text},
        lambda *_: (_ for _ in ()).throw(AssertionError("Unexpected fallback")),
    )
    assert result[0] is None
    assert "empty or inaudible" in result[1]
    assert "Do not guess" in result[1]


@pytest.mark.asyncio
async def test_configured_failure_keeps_the_existing_local_fallback():
    calls = []

    def fallback(path):
        calls.append(path)
        return {"success": True, "transcript": "recovered"}

    result = await GatewayInboundTranscriptionMixin()._transcribe_one_clip(
        "voice.ogg", lambda *_: {"success": False, "error": "unavailable"}, fallback,
    )
    assert result == ("recovered", '"recovered"')
    assert calls == ["voice.ogg"]


@pytest.mark.asyncio
async def test_failed_transcription_keeps_the_neutral_official_note(tmp_path):
    path = str(tmp_path / "voice.ogg")
    result = await GatewayInboundTranscriptionMixin()._transcribe_one_clip(
        path, lambda *_: {"success": False, "error": "unavailable"},
        lambda *_: {"success": False, "error": "unavailable"},
    )
    assert result[0] is None
    assert "could not be transcribed automatically" in result[1]
    assert "setup" not in result[1].lower()
