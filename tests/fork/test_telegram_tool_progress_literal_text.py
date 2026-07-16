"""Fork protection: Telegram tool-progress keeps literal delivery semantics."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.run import _tool_progress_delivery_metadata
from plugins.platforms.telegram.adapter import TelegramAdapter


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


def test_telegram_progress_metadata_preserves_topic_and_marks_literal_text():
    topic_metadata = {"thread_id": "17585"}

    literal = _tool_progress_delivery_metadata(
        topic_metadata, platform=Platform.TELEGRAM
    )

    assert literal == {"thread_id": "17585", "plain_text": True}
    assert topic_metadata == {"thread_id": "17585"}
    assert _tool_progress_delivery_metadata(topic_metadata, platform=Platform.DISCORD) is topic_metadata


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
