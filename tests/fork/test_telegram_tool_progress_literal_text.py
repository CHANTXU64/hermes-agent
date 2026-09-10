"""Fork protection: Telegram tool-progress keeps literal delivery semantics."""

import queue
from typing import Any, cast

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from fork_features import telegram_tool_progress
import gateway.run as gateway_run
from gateway.config import Platform, PlatformConfig
from gateway.run import _tool_progress_delivery_metadata
from gateway.run_turn_runner import TurnRunner
from plugins.platforms.telegram.adapter import TelegramAdapter
import tools.terminal_tool  # noqa: F401 - register the terminal progress emoji


RAW_PROGRESS = "```|code_block ||hidden||"


def _make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="fake-token", extra={"rich_messages": True})
    )
    bot = MagicMock()
    bot.do_api_request = AsyncMock(return_value=SimpleNamespace(message_id=1))
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    bot.send_chat_action = AsyncMock()
    bot.send_message_draft = AsyncMock(return_value=True)
    bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=1))
    adapter._bot = bot
    return adapter


def _build_terminal_progress(command: str, *, mode: str = "all"):
    adapter = _make_adapter()
    context = SimpleNamespace(
        source=SimpleNamespace(platform=Platform.TELEGRAM),
        last_was_terminal_block=[False],
        progress_mode=mode,
        progress_queue=queue.Queue(),
    )
    runner = SimpleNamespace(_adapter_for_source=lambda _source: adapter)
    turn_runner = TurnRunner(cast(Any, runner), cast(Any, context))
    message = turn_runner._progress_build_message(
        "terminal", command, {"command": command}
    )
    return message, context


def test_telegram_terminal_progress_is_one_compact_literal_line():
    message, context = _build_terminal_progress("printf one\nprintf two")

    assert message == "💻 terminal: printf one printf two"
    assert "```" not in message
    assert "\n" not in message
    assert context.last_was_terminal_block == [False]


def test_telegram_terminal_progress_verbose_does_not_generate_a_fenced_block():
    message, context = _build_terminal_progress(
        "printf one\nprintf two", mode="verbose"
    )

    assert message is None
    queued = context.progress_queue.get_nowait()
    assert "```" not in queued


def test_telegram_progress_metadata_preserves_topic_and_marks_literal_text():
    topic_metadata = {"thread_id": "17585"}

    literal = _tool_progress_delivery_metadata(
        topic_metadata, platform=Platform.TELEGRAM
    )

    assert literal == {"thread_id": "17585", "plain_text": True}
    assert topic_metadata == {"thread_id": "17585"}
    assert _tool_progress_delivery_metadata(topic_metadata, platform=Platform.DISCORD) is topic_metadata


def test_gateway_uses_fork_owned_progress_metadata_policy():
    assert (
        gateway_run._tool_progress_delivery_metadata
        is telegram_tool_progress.tool_progress_delivery_metadata
    )
    assert telegram_tool_progress.tool_progress_delivery_metadata(
        platform="telegram"
    ) == {"plain_text": True}


@pytest.mark.asyncio
async def test_literal_progress_text_bypasses_telegram_rich_and_markdown_on_send_and_edit():
    """The exact control characters must remain literal across a progress lifecycle."""
    adapter = _make_adapter()
    metadata = _tool_progress_delivery_metadata(platform=Platform.TELEGRAM)

    sent = await adapter.send("12345", RAW_PROGRESS, metadata=metadata)

    assert sent.success is True
    adapter._bot.do_api_request.assert_not_called()
    send_kwargs = adapter._bot.send_message.call_args.kwargs
    assert send_kwargs["text"] == RAW_PROGRESS
    assert send_kwargs["parse_mode"] is None

    edited = await adapter.edit_message(
        "12345", "1", RAW_PROGRESS, finalize=True, metadata=metadata
    )

    assert edited.success is True
    edit_kwargs = adapter._bot.edit_message_text.call_args.kwargs
    assert edit_kwargs["text"] == RAW_PROGRESS
    assert edit_kwargs["parse_mode"] is None
