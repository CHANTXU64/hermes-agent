"""In-flight sends keep their Bot identity when the named adapter is rebuilt.

Exercise real Telegram send methods and the Fork runtime; only Telegram's wire
endpoint is mocked. A healthy primary is deliberately present in every case.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fork_features.multi_telegram_accounts.runtime import TelegramAccountRuntime
from gateway.config import Platform, PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def _adapter(account: str | None = None) -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(
        enabled=True, token="test-token", extra={"account_id": account} if account else {},
    ))
    adapter._rich_send_disabled = True
    adapter.send_typing = AsyncMock()
    adapter._RECONNECT_WAIT_SECONDS = 0.03
    adapter._RECONNECT_POLL_INTERVAL = 0.005
    adapter._bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)),
        send_document=AsyncMock(return_value=SimpleNamespace(message_id=43)),
    )
    return adapter


def _topology(account: str = "work"):
    primary, other, old, live = (_adapter(), _adapter("other"), _adapter(account), _adapter("work"))
    runner = SimpleNamespace(adapters={Platform.TELEGRAM: primary})
    runtime = TelegramAccountRuntime(runner)
    runner._telegram_accounts = runtime
    runtime.live = {"other": other}
    for adapter in (primary, other, old, live):
        adapter.gateway_runner = runner
    old._bot = None
    return runtime, primary, other, old, live


def _assert_no_other_bot_send(primary, other):
    for adapter in (primary, other):
        adapter._bot.send_message.assert_not_awaited()
        adapter._bot.send_document.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["work", " WORK "])
@pytest.mark.parametrize("reconnected", [False, True])
async def test_old_named_text_never_borrows_primary(account, reconnected):
    runtime, primary, other, old, live = _topology(account)
    if reconnected:
        runtime.live["work"] = live
    metadata = {"plain_text": True, "disable_notification": True}

    result = await old.send("123", "tool progress", reply_to="7", metadata=metadata)

    _assert_no_other_bot_send(primary, other)
    assert result.success is reconnected
    if reconnected:
        call = live._bot.send_message.await_args.kwargs
        assert call["text"] == "tool progress"
        assert call["chat_id"] == 123
        assert call["reply_to_message_id"] == 7
        assert call["parse_mode"] is None
        assert call["disable_notification"] is True
    else:
        assert result.error == "Not connected"
        assert result.retryable is True
        live._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_named_replacement_installed_during_wait_keeps_account():
    runtime, primary, other, old, live = _topology()
    old._RECONNECT_WAIT_SECONDS = 0.3

    async def reconnect():
        await asyncio.sleep(0.01)
        runtime.live["work"] = live

    task = asyncio.create_task(reconnect())
    try:
        result = await old.send("123", "background notice", metadata={"plain_text": True})
    finally:
        await task

    _assert_no_other_bot_send(primary, other)
    assert result.success is True
    assert live._bot.send_message.await_args.kwargs["text"] == "background notice"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["missing", "bad:account"])
async def test_unknown_or_invalid_named_account_does_not_fall_back(account):
    runtime, primary, other, old, live = _topology(account)
    runtime.live["work"] = live

    result = await old.send("123", "private notice")

    _assert_no_other_bot_send(primary, other)
    live._bot.send_message.assert_not_awaited()
    assert result.success is False
    assert result.retryable is True


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [".json", ".zip"])
@pytest.mark.parametrize("state", ["ready", "during-wait", "offline"])
async def test_old_named_attachment_uses_same_account_replacement(tmp_path, suffix, state):
    runtime, primary, other, old, live = _topology()
    old._RECONNECT_WAIT_SECONDS = 0.3 if state == "during-wait" else 0.03
    artifact = tmp_path / ("report" + suffix)
    payload = b"attachment payload"
    artifact.write_bytes(payload)
    uploads = []

    async def upload(**kwargs):
        uploads.append({**kwargs, "payload": kwargs["document"].read()})
        return SimpleNamespace(message_id=43)

    live._bot.send_document.side_effect = upload
    if state == "ready":
        runtime.live["work"] = live

    async def reconnect():
        await asyncio.sleep(0.01)
        runtime.live["work"] = live

    task = asyncio.create_task(reconnect()) if state == "during-wait" else None
    try:
        result = await old.send_document(
            "123", str(artifact), caption="report", file_name="download" + suffix,
            reply_to="7", metadata={"disable_notification": True},
        )
    finally:
        if task is not None:
            await task

    _assert_no_other_bot_send(primary, other)
    assert result.success is (state != "offline")
    if state != "offline":
        assert len(uploads) == 1
        sent = uploads[0]
        assert sent["payload"] == payload
        assert sent["filename"] == "download" + suffix
        assert sent["caption"] == "report"
        assert sent["chat_id"] == 123
        assert sent["reply_to_message_id"] == 7
        assert sent["disable_notification"] is True
    else:
        assert result.error == "Not connected"
        assert result.retryable is True
        assert uploads == []


@pytest.mark.asyncio
async def test_attachment_failure_notice_keeps_original_account():
    runtime, primary, other, old, live = _topology()
    runtime.live["work"] = live

    await old._notify_media_delivery_failure("123", "/not-present/report.json")

    _assert_no_other_bot_send(primary, other)
    live._bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_named_account_without_runtime_fails_closed():
    runtime, primary, other, old, live = _topology()
    del old.gateway_runner._telegram_accounts

    result = await old.send("123", "notice")

    _assert_no_other_bot_send(primary, other)
    assert result.success is False
    assert result.retryable is True


@pytest.mark.asyncio
async def test_named_slot_still_pointing_at_old_transport_does_not_recurse():
    runtime, primary, other, old, live = _topology()
    runtime.live["work"] = old

    result = await old.send("123", "notice")

    _assert_no_other_bot_send(primary, other)
    assert result.success is False
    assert result.retryable is True


@pytest.mark.asyncio
async def test_connected_named_transport_does_not_switch_to_replacement():
    runtime, primary, other, old, live = _topology()
    original_bot = _adapter("work")._bot
    old._bot = original_bot
    runtime.live["work"] = live

    result = await old.send("123", "notice", metadata={"plain_text": True})

    _assert_no_other_bot_send(primary, other)
    assert result.success is True
    original_bot.send_message.assert_awaited_once()
    live._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["send", "send_document"])
async def test_permanent_named_failure_does_not_wait_or_borrow_primary(method):
    runtime, primary, other, old, live = _topology()
    old._set_fatal_error("telegram_auth_error", "invalid token", retryable=False)
    old._wait_for_reconnection = AsyncMock(side_effect=AssertionError("must not wait"))

    result = await getattr(old, method)("123", "notice-or-file")

    _assert_no_other_bot_send(primary, other)
    assert result.success is False
    assert result.retryable is False


@pytest.mark.asyncio
async def test_real_fatal_reconnect_lifecycle_keeps_old_turn_on_named_bot(tmp_path):
    runtime, primary, other, old, live = _topology()
    host = old.gateway_runner
    host._running = True
    host._busy_text_mode = "interrupt"
    host.session_store = None
    host._handle_message = AsyncMock()
    host._handle_adapter_fatal_error = AsyncMock()
    host._handle_active_session_busy_message = AsyncMock()
    host._make_adapter_auth_check = lambda platform: None
    host._sync_voice_mode_state_to_adapter = lambda adapter: None
    host._update_platform_runtime_status = lambda *args, **kwargs: None
    host._ensure_reconnect_watcher_running = lambda: None
    host._connect_adapter_with_timeout = AsyncMock(return_value=True)
    old._bot = _adapter("work")._bot
    runtime.live["work"] = old
    old._set_fatal_error("telegram_network_error", "network recovery stalled", retryable=True)

    async def disconnect(adapter, platform):
        assert runtime.failed["work"]["config"] is old.config
        adapter._bot = None

    def create(platform, config):
        assert platform is Platform.TELEGRAM
        assert config is old.config
        return live

    host._safe_adapter_disconnect = disconnect
    host._create_adapter = create

    assert await runtime.handle_fatal(old) is True
    assert runtime.adapter_for("work") is None
    await runtime.reconnect_failed(now=float("inf"))
    assert runtime.adapter_for("work") is live
    assert runtime.failed == {}

    text = await old.send("123", "old turn progress", metadata={"plain_text": True})
    artifact = tmp_path / "report.json"
    artifact.write_text("{}")
    media = await old.send_document("123", str(artifact))
    await old._notify_media_delivery_failure("123", "another-file.zip")

    _assert_no_other_bot_send(primary, other)
    assert text.success is True
    assert media.success is True
    assert live._bot.send_message.await_count == 2
    live._bot.send_document.assert_awaited_once()


@pytest.mark.asyncio
async def test_primary_document_reconnect_stays_primary(tmp_path):
    runtime, primary, other, old, live = _topology()
    retired_primary = _adapter()
    retired_primary._bot = None
    retired_primary.gateway_runner = primary.gateway_runner
    runtime.live["work"] = live
    artifact = tmp_path / "report.json"
    artifact.write_text("{}")

    result = await retired_primary.send_document("123", str(artifact))

    assert result.success is True
    primary._bot.send_document.assert_awaited_once()
    other._bot.send_document.assert_not_awaited()
    live._bot.send_document.assert_not_awaited()
