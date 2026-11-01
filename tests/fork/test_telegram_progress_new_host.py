"""Relocated official Telegram progress entry; surrounding API boundaries are fakes.

These component tests do not replace full GatewayRunner topic/turn tests.
"""
import queue
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from typing import Any, cast

import pytest

from agent.display import set_friendly_tool_labels
from fork_features.telegram_tool_progress import tool_progress_delivery_metadata
from gateway.config import Platform, PlatformConfig
from gateway.run_turn_progress import GatewayTurnProgressMixin
from plugins.platforms.telegram.adapter import TelegramAdapter
import tools.terminal_tool  # register the terminal progress emoji


def renderer(platform, mode="all") -> Any:
    host = cast(Any, GatewayTurnProgressMixin())
    host._ctx = SimpleNamespace(source=SimpleNamespace(platform=platform),
                                last_was_terminal_block=[False], progress_mode=mode,
                                progress_queue=queue.Queue())
    adapter = SimpleNamespace(supports_code_blocks=True)
    host._runner = SimpleNamespace(_delivery_adapter_for=lambda _: adapter)
    return host


def test_turn_runner_does_not_shadow_the_relocated_official_progress():
    source = (Path(__file__).resolve().parents[2] / "gateway/run_turn_runner.py").read_text()
    definitions = set(re.findall(r"^    (?:async )?def (\w+)\(", source, re.MULTILINE))
    assert not definitions.intersection({"_progress_build_message", "_progress_terminal_blocks", "send_progress_messages"})


@pytest.mark.parametrize("mode", ["all", "new"])
def test_new_progress_entry_keeps_telegram_terminal_compact(monkeypatch, mode):
    monkeypatch.setenv("HERMES_LANGUAGE", "en")
    set_friendly_tool_labels(True)
    host = renderer(Platform.TELEGRAM, mode)
    result = host._progress_build_message("terminal", "printf one", {"command": "printf one\nprintf two"})
    assert result == "💻 Running printf one printf two"
    assert host._ctx.last_was_terminal_block == [False]


def test_new_verbose_entry_keeps_telegram_arguments_literal():
    host = renderer(Platform.TELEGRAM, "verbose")
    assert host._progress_build_message("terminal", "echo one", {"command": "echo one\necho two"}) is None
    result = host._ctx.progress_queue.get_nowait()
    assert "```" not in result
    assert '"command"' in result


@pytest.mark.parametrize("platform", [Platform.DISCORD, Platform.SLACK])
def test_other_platform_terminal_blocks_remain_official(platform):
    host = renderer(platform)
    result = host._progress_build_message("terminal", "echo one", {"command": "echo one\necho two"})
    assert "```" in result
    assert host._ctx.last_was_terminal_block == [True]


def adapter() -> Any:
    obj = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra={"rich_messages": True}))
    bot = MagicMock()
    bot.do_api_request = AsyncMock(return_value=SimpleNamespace(message_id=1))
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    bot.edit_message_text = AsyncMock(return_value=SimpleNamespace(message_id=1))
    bot.send_chat_action = AsyncMock()
    bot.send_message_draft = AsyncMock(return_value=True)
    obj._bot = bot
    return obj


@pytest.mark.asyncio
async def test_literal_adapter_send_and_final_edit_never_parse_markup():
    obj = adapter()
    metadata = tool_progress_delivery_metadata({"thread_id": "17"}, platform=Platform.TELEGRAM)
    raw = "```|command ||literal|| a_b 🧱"
    assert (await obj.send("12345", raw, metadata=metadata)).success
    assert obj._bot.send_message.call_args.kwargs["text"] == raw
    assert obj._bot.send_message.call_args.kwargs["parse_mode"] is None
    assert (await obj.edit_message("12345", "1", raw, finalize=True, metadata=metadata)).success
    assert obj._bot.edit_message_text.call_args.kwargs["text"] == raw
    assert obj._bot.edit_message_text.call_args.kwargs["parse_mode"] is None
    obj._bot.do_api_request.assert_not_called()


@pytest.mark.asyncio
async def test_literal_overflow_edit_keeps_control_characters_and_topic():
    obj = adapter()
    metadata = tool_progress_delivery_metadata({"thread_id": "17"}, platform=Platform.TELEGRAM)
    raw = "```|command ||literal|| a_b 🧱 " * 240
    result = await obj.edit_message("12345", "1", raw, finalize=True, metadata=metadata)
    assert result.success
    calls = obj._bot.edit_message_text.call_args_list + obj._bot.send_message.call_args_list
    assert len(calls) > 1
    assert all(call.kwargs.get("parse_mode") is None for call in calls)
    chunks = [call.kwargs["text"] for call in calls]
    assert "".join(re.sub(r" \(\d+/\d+\)$", "", chunk) for chunk in chunks) == raw
    assert all(len(chunk.encode("utf-16-le")) // 2 <= obj.MAX_MESSAGE_LENGTH for chunk in chunks)
    assert all(call.kwargs.get("message_thread_id") == 17 for call in obj._bot.send_message.call_args_list)
    obj._bot.do_api_request.assert_not_called()
