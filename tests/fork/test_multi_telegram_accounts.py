"""Fork: multi Telegram bot tokens in one profile (account_id session slots)."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageType, SendResult
from gateway.run import GatewayRunner, _parse_session_key
from gateway.session import (
    SessionSource,
    SessionStore,
    append_account_session_key,
    build_session_key,
    normalize_account_id,
    split_account_session_key,
)
from hermes_state import SessionDB
from plugins.platforms.telegram.adapter import TelegramAdapter


class StubTelegramAdapter(BasePlatformAdapter):
    """Small real adapter shell for multi-account runtime wiring tests."""

    def __init__(self, token="222:BBB", account_id="work", succeed=True):
        super().__init__(
            PlatformConfig(
                enabled=True,
                token=token,
                extra={"account_id": account_id},
            ),
            Platform.TELEGRAM,
        )
        self.succeed = succeed
        self.connect_calls = []

    async def connect(self, *, is_reconnect: bool = False):
        self.connect_calls.append(is_reconnect)
        return self.succeed

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="1")

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


def _make_runtime_runner():
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(enabled=True, token="111:AAA")
        }
    )
    runner.adapters = {}
    runner._telegram_account_adapters = {}
    runner._failed_platforms = {}
    runner._failed_telegram_accounts = {}
    runner._running = True
    runner._exit_with_failure = False
    runner._exit_cleanly = False
    runner._exit_reason = None
    runner.delivery_router = MagicMock()
    runner.session_store = MagicMock()
    runner.stop = AsyncMock()
    runner._update_platform_runtime_status = MagicMock()
    runner._sync_voice_mode_state_to_adapter = MagicMock()
    runner._ensure_reconnect_watcher_running = MagicMock()
    runner._handle_message = AsyncMock()
    runner._is_user_authorized = MagicMock(return_value=True)
    return runner


def _clear_telegram_token_env(monkeypatch):
    """Keep env discovery tests independent from the operator's real .env."""
    for key in list(os.environ):
        if key == "TELEGRAM_BOT_TOKEN" or key.startswith("TELEGRAM_BOT_TOKEN_"):
            monkeypatch.delenv(key, raising=False)


def test_normalize_account_id():
    assert normalize_account_id("Work") == "work"
    assert normalize_account_id("bot-2") == "bot-2"
    assert normalize_account_id("") is None
    assert normalize_account_id("bad id") is None
    assert normalize_account_id("-leading") is None


def test_primary_session_key_byte_compatible():
    src = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="5612546357",
        chat_type="dm",
        user_id="5612546357",
    )
    assert build_session_key(src) == "agent:main:telegram:dm:5612546357"


def test_named_account_session_key_isolated():
    primary = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="5612546357",
        chat_type="dm",
        user_id="5612546357",
    )
    work = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="5612546357",
        chat_type="dm",
        user_id="5612546357",
        account_id="work",
    )
    k1 = build_session_key(primary)
    k2 = build_session_key(work)
    assert k1 == "agent:main:telegram:dm:5612546357"
    assert k2 == "agent:main:telegram:dm:5612546357:account:work"
    assert k1 != k2


def test_named_account_does_not_recover_primary_peer_history(tmp_path, caplog):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = SessionStore(tmp_path / "sessions", GatewayConfig())
        store._db = db
        primary = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="5612546357",
            chat_type="dm",
            user_id="5612546357",
        )
        monika = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="5612546357",
            chat_type="dm",
            user_id="5612546357",
            account_id="monika",
        )

        primary_entry = store.get_or_create_session(primary)
        db.append_message(primary_entry.session_id, "user", "PRIMARY_CONTEXT")
        monika_entry = store.get_or_create_session(monika)

        assert monika_entry.session_id != primary_entry.session_id
        assert any(
            "recovered routing identity is incompatible" in record.getMessage()
            for record in caplog.records
        )
    finally:
        db.close()


def test_transfer_session_detaches_previous_bot_route(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = SessionStore(tmp_path / "sessions", GatewayConfig())
        store._db = db
        primary = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="5612546357",
            chat_type="dm",
            user_id="5612546357",
        )
        monika = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="5612546357",
            chat_type="dm",
            user_id="5612546357",
            account_id="monika",
        )
        primary_entry = store.get_or_create_session(primary)
        monika_entry = store.get_or_create_session(monika)

        transferred, detached = store.transfer_session(
            monika_entry.session_key, primary_entry.session_id
        )

        assert transferred is not None
        assert transferred.session_id == primary_entry.session_id
        assert detached == [primary_entry.session_key]
        assert primary_entry.session_key not in store._entries
        assert store._entries[monika_entry.session_key].session_id == primary_entry.session_id
    finally:
        db.close()


def test_account_suffix_not_parsed_as_thread_id():
    key = "agent:main:telegram:dm:5612546357:account:work"
    parsed = _parse_session_key(key)
    assert parsed is not None
    assert parsed["platform"] == "telegram"
    assert parsed["chat_id"] == "5612546357"
    assert parsed.get("thread_id") is None
    assert parsed.get("account_id") == "work"


def test_account_suffix_with_real_thread():
    key = "agent:main:telegram:dm:5612546357:42:account:work"
    parsed = _parse_session_key(key)
    assert parsed is not None
    assert parsed["thread_id"] == "42"
    assert parsed["account_id"] == "work"


def test_split_append_roundtrip():
    base = "agent:main:telegram:dm:1"
    full = append_account_session_key(base, "alerts")
    b2, acc = split_account_session_key(full)
    assert b2 == base
    assert acc == "alerts"
    assert append_account_session_key(full, "alerts") == full


def test_env_discovers_extra_tokens(monkeypatch):
    _clear_telegram_token_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111:AAA")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_WORK", "222:BBB")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_ALERTS", "333:CCC")

    from gateway.config import GatewayConfig, _apply_env_overrides

    cfg = GatewayConfig()
    _apply_env_overrides(cfg)

    tg = cfg.platforms.get(Platform.TELEGRAM)
    assert tg is not None
    assert tg.token == "111:AAA"
    accounts = (tg.extra or {}).get("accounts") or {}
    assert set(accounts.keys()) == {"work", "alerts"}
    assert accounts["work"]["token"] == "222:BBB"
    assert accounts["alerts"]["token"] == "333:CCC"


def test_named_tokens_require_explicit_primary(monkeypatch):
    _clear_telegram_token_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_MONIKA", "222:BBB")

    from gateway.config import GatewayConfig, _apply_env_overrides

    cfg = GatewayConfig()
    _apply_env_overrides(cfg)

    assert Platform.TELEGRAM not in cfg.platforms


def test_env_skips_duplicate_of_primary(monkeypatch):
    _clear_telegram_token_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111:AAA")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_CLONE", "111:AAA")

    from gateway.config import GatewayConfig, _apply_env_overrides

    cfg = GatewayConfig()
    _apply_env_overrides(cfg)
    tg = cfg.platforms.get(Platform.TELEGRAM)
    assert tg is not None
    accounts = (tg.extra or {}).get("accounts") or {}
    assert "clone" not in accounts


def test_extra_token_discovery_uses_active_profile_secret_scope(monkeypatch):
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from gateway.config import GatewayConfig, _apply_env_overrides

    _clear_telegram_token_env(monkeypatch)
    # Simulate default-profile credentials still present process-wide.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111:AAA")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_MONIKA", "222:BBB")
    token = set_secret_scope(
        {
            "TELEGRAM_BOT_TOKEN": "333:CCC",
            "TELEGRAM_BOT_TOKEN_WORK": "444:DDD",
        }
    )
    try:
        cfg = GatewayConfig()
        _apply_env_overrides(cfg)
    finally:
        reset_secret_scope(token)

    tg = cfg.platforms.get(Platform.TELEGRAM)
    assert tg is not None
    assert tg.token == "333:CCC"
    accounts = (tg.extra or {}).get("accounts") or {}
    assert set(accounts) == {"work"}
    assert accounts["work"]["token"] == "444:DDD"


def test_append_replaces_existing_account_suffix():
    original = "agent:main:telegram:dm:1:account:work"
    assert append_account_session_key(original, "alerts") == (
        "agent:main:telegram:dm:1:account:alerts"
    )


def test_adapter_for_source_routes_named_account_and_fails_closed():
    runner = _make_runtime_runner()
    primary = object()
    work = object()
    runner.adapters = {Platform.TELEGRAM: primary}
    runner._telegram_account_adapters = {"work": work}

    base = dict(
        platform=Platform.TELEGRAM,
        chat_id="5612546357",
        chat_type="dm",
        user_id="5612546357",
    )
    assert runner._adapter_for_source(SessionSource(**base)) is primary
    assert runner._adapter_for_source(
        SessionSource(**base, account_id="work")
    ) is work
    # Never leak a named-account turn to the primary bot when it is offline.
    assert runner._adapter_for_source(
        SessionSource(**base, account_id="missing")
    ) is None


def test_telegram_predispatch_event_and_auth_carry_account_id():
    adapter = TelegramAdapter(
        PlatformConfig(
            enabled=True,
            token="222:BBB",
            extra={"account_id": "work"},
        )
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(
            id=5612546357,
            type="private",
            title=None,
            full_name="Wright",
            is_forum=False,
        ),
        from_user=SimpleNamespace(
            id=5612546357,
            username="wright",
            full_name="Wright",
            is_bot=False,
        ),
        sender_chat=None,
        message_thread_id=None,
        is_topic_message=False,
        message_id=99,
        reply_to_message=None,
        quote=None,
        text="hello",
        caption=None,
        date=datetime.now(timezone.utc),
    )

    auth_source = adapter._source_from_message_for_auth(message)
    event = adapter._build_message_event(message, MessageType.TEXT)

    assert auth_source.account_id == "work"
    assert event.source.account_id == "work"
    assert build_session_key(event.source).endswith(":account:work")


@pytest.mark.asyncio
async def test_named_account_fatal_does_not_remove_primary_and_queues_own_config():
    runner = _make_runtime_runner()
    primary = object()
    adapter = StubTelegramAdapter(token="222:BBB", account_id="work")
    adapter.disconnect = AsyncMock()
    adapter._set_fatal_error("network_error", "DNS failure", retryable=True)
    runner.adapters = {Platform.TELEGRAM: primary}
    runner._telegram_account_adapters = {"work": adapter}

    await runner._handle_adapter_fatal_error(adapter)

    assert runner.adapters[Platform.TELEGRAM] is primary
    assert "work" not in runner._telegram_account_adapters
    assert runner._failed_telegram_accounts["work"]["config"] is adapter.config
    assert runner._failed_telegram_accounts["work"]["config"].token == "222:BBB"
    adapter.disconnect.assert_awaited_once()
    runner.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_named_account_fatal_without_primary_queues_without_stopping_gateway():
    """A named retry queue is not a stranded primary Telegram platform."""
    runner = _make_runtime_runner()
    adapter = StubTelegramAdapter(token="222:BBB", account_id="work")
    adapter.disconnect = AsyncMock()
    adapter._set_fatal_error("network_error", "DNS failure", retryable=True)
    runner._telegram_account_adapters = {"work": adapter}
    runner._safe_adapter_disconnect = AsyncMock()

    await runner._handle_adapter_fatal_error(adapter)

    assert "work" not in runner._telegram_account_adapters
    assert runner._failed_telegram_accounts["work"]["config"] is adapter.config
    runner._safe_adapter_disconnect.assert_awaited_once_with(
        adapter, Platform.TELEGRAM
    )
    runner._ensure_reconnect_watcher_running.assert_called_once_with()
    runner.stop.assert_not_awaited()
    assert runner._exit_with_failure is False
    assert runner._exit_reason is None


@pytest.mark.asyncio
async def test_nontelegram_account_id_is_not_misrouted_to_telegram_queue():
    runner = _make_runtime_runner()
    adapter = StubTelegramAdapter(token="wx-token", account_id="wx-account")
    adapter.platform = Platform.WEIXIN
    adapter.disconnect = AsyncMock()
    adapter._set_fatal_error("network_error", "temporary", retryable=True)
    runner.config.platforms[Platform.WEIXIN] = adapter.config
    runner.adapters = {Platform.WEIXIN: adapter}

    await runner._handle_adapter_fatal_error(adapter)

    assert runner._failed_telegram_accounts == {}
    assert Platform.WEIXIN in runner._failed_platforms


@pytest.mark.asyncio
async def test_named_account_reconnect_restores_map_and_direct_handler(monkeypatch):
    runner = _make_runtime_runner()
    cfg = PlatformConfig(
        enabled=True,
        token="222:BBB",
        extra={"account_id": "work"},
    )
    runner._failed_telegram_accounts = {
        "work": {"config": cfg, "attempts": 0, "next_retry": time.monotonic() - 1}
    }
    adapter = StubTelegramAdapter(token="222:BBB", account_id="work")
    monkeypatch.setattr(runner, "_create_adapter", MagicMock(return_value=adapter))
    monkeypatch.setattr(
        runner,
        "_connect_adapter_with_timeout",
        AsyncMock(return_value=True),
    )

    await runner._reconnect_failed_telegram_accounts()

    assert runner._telegram_account_adapters["work"] is adapter
    assert "work" not in runner._failed_telegram_accounts
    runner._connect_adapter_with_timeout.assert_awaited_once_with(
        adapter, Platform.TELEGRAM, is_reconnect=True
    )

    assert adapter._message_handler is runner._handle_message
    event = SimpleNamespace(
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="5612546357",
            chat_type="dm",
            user_id="5612546357",
            account_id="work",
        )
    )
    await adapter._message_handler(event)
    assert event.source.account_id == "work"
    runner._handle_message.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_start_named_account_failure_enters_named_reconnect_queue(monkeypatch):
    runner = _make_runtime_runner()
    runner.config.platforms[Platform.TELEGRAM].extra["accounts"] = {
        "work": {"token": "222:BBB"}
    }
    adapter = StubTelegramAdapter(token="222:BBB", account_id="work", succeed=False)
    monkeypatch.setattr(runner, "_create_adapter", MagicMock(return_value=adapter))
    monkeypatch.setattr(
        runner,
        "_connect_adapter_with_timeout",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(runner, "_safe_adapter_disconnect", AsyncMock())

    connected = await runner._start_telegram_account_adapters()

    assert connected == 0
    assert "work" in runner._failed_telegram_accounts
    queued = runner._failed_telegram_accounts["work"]
    assert queued["config"].token == "222:BBB"
    assert queued["attempts"] == 1
    assert Platform.TELEGRAM not in runner._failed_platforms


@pytest.mark.asyncio
async def test_cross_account_resume_uses_real_telegram_origin_identity():
    runner = _make_runtime_runner()
    caller = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="5612546357",
        chat_type="dm",
        user_id="5612546357",
        account_id="monika",
    )
    original = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="5612546357",
        chat_type="dm",
        user_id="5612546357",
        account_id="work",
    )
    runner._gateway_session_origin_for_id = MagicMock(return_value=original)

    assert await runner._resume_target_allowed(caller, "session-from-work") is True


@pytest.mark.asyncio
async def test_named_account_restart_marker_and_comeback_use_same_adapter(
    tmp_path, monkeypatch
):
    import json
    import gateway.run as gateway_run
    from gateway.platforms.base import MessageEvent, MessageType
    from tests.gateway.restart_test_helpers import make_restart_runner

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, primary = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)
    work = StubTelegramAdapter(token="222:BBB", account_id="work")
    primary.send = AsyncMock(return_value=SendResult(success=True, message_id="p"))
    work.send = AsyncMock(return_value=SendResult(success=True, message_id="w"))
    runner._telegram_account_adapters = {"work": work}

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="5612546357",
        chat_type="dm",
        user_id="5612546357",
        account_id="work",
    )
    event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m1",
    )

    await runner._handle_restart_command(event)
    marker = json.loads((tmp_path / ".restart_notify.json").read_text())
    assert marker["account_id"] == "work"

    await runner._send_restart_notification()

    work.send.assert_awaited_once()
    primary.send.assert_not_awaited()
