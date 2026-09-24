"""Account-aware Gateway lifecycle notices using real handlers and transport doubles."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

import gateway.run as gr
from gateway.config import Platform
from gateway.platforms.base import SendResult
from tests.gateway.restart_test_helpers import make_restart_runner, RestartTestAdapter


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(gr, "_hermes_home", tmp_path)
    runner, primary = make_restart_runner()
    named = RestartTestAdapter()
    named.config.extra["account_id"] = "work"
    runner._telegram_accounts.live["work"] = named
    runner._profile_adapters = {}
    marker = tmp_path / ".restart_notify.json"
    marker.write_text(json.dumps({"platform": "telegram", "chat_id": "42", "chat_type": "dm", "account_id": "work"}))
    return runner, primary, named, marker


@pytest.mark.asyncio
async def test_restart_offline_waits_for_own_account(rig):
    runner, primary, named, marker = rig
    runner._telegram_accounts.live.clear()
    assert await runner._send_restart_notification() is None
    assert not primary.sent_calls
    assert marker.exists()
    runner._telegram_accounts.live["work"] = named
    assert await runner._send_restart_notification() == ("telegram", "42", None)
    assert len(named.sent_calls) == 1
    assert not marker.exists()


@pytest.mark.asyncio
async def test_restart_named_failure_is_not_reported_as_delivered(rig):
    runner, primary, named, marker = rig
    named.send = AsyncMock(return_value=SendResult(success=False, error="Not connected", retryable=True))
    assert await runner._send_restart_notification() is None
    assert marker.exists()
    assert not primary.sent_calls


@pytest.mark.asyncio
async def test_restart_named_respects_notification_switch(rig):
    runner, primary, named, marker = rig
    runner.config.platforms[Platform.TELEGRAM].gateway_restart_notification = False
    assert await runner._send_restart_notification() is None
    assert not primary.sent_calls and not named.sent_calls
    assert not marker.exists()


@pytest.mark.asyncio
async def test_restart_notice_concurrent_boot_and_reconnect_only_sends_once(rig):
    runner, primary, named, marker = rig
    entered = asyncio.Event()
    release = asyncio.Event()
    async def send(*a, **kw):
        entered.set()
        await release.wait()
        return SendResult(success=True, message_id="m1")
    named.send = AsyncMock(side_effect=send)
    first = asyncio.create_task(runner._send_restart_notification())
    await entered.wait()
    second = asyncio.create_task(runner._send_restart_notification())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert named.send.await_count == 1
    assert not primary.sent_calls
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("accounts", [(None, "work"), ("work", None)])
async def test_shutdown_notifies_both_bots_for_same_user(rig, monkeypatch, accounts):
    from gateway.session import build_session_key
    from tests.gateway.restart_test_helpers import make_restart_source
    runner, primary, named, marker = rig
    runner.session_store = None
    runner._restart_requested = True
    runner._notice_allowed = lambda *a: True
    runner._resolve_profile_home_for_source = lambda s: marker.parent
    for account in accounts:
        src = make_restart_source(chat_id="42")
        src.account_id = account
        key = build_session_key(src)
        runner._session_sources[key] = src
        runner._running_agents[key] = object()
    runner._restart_command_source = src
    runner._snapshot_running_agents = lambda: list(runner._running_agents)
    async def present(fn, **kw):
        await fn()
        return True
    monkeypatch.setattr("gateway.warning_notifications.present_notification", present)
    await runner._notify_active_sessions_of_shutdown()
    assert len(primary.sent_calls) == len(named.sent_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["cached", "persisted", "key_only"])
@pytest.mark.parametrize("online", [True, False])
async def test_shutdown_restores_original_account_without_fallback(rig, monkeypatch, origin, online):
    from types import SimpleNamespace
    from gateway.session import SessionSource, build_session_key
    from tests.gateway.restart_test_helpers import make_restart_source
    runner, primary, named, marker = rig
    src = make_restart_source(chat_id="42")
    src.account_id = "work"
    key = build_session_key(src)
    if not online:
        runner._telegram_accounts.live.clear()
    runner._restart_requested = True
    runner._restart_command_source = src
    runner._notice_allowed = lambda *a: True
    runner._resolve_profile_home_for_source = lambda s: marker.parent
    runner._snapshot_running_agents = lambda: [key]
    persisted = SessionSource.from_dict(src.to_dict())
    runner.session_store = None
    if origin == "cached":
        runner._session_sources[key] = persisted
    elif origin == "persisted":
        runner.session_store = SimpleNamespace(_entries={key: SimpleNamespace(
            origin=persisted, session_key=key, transport_profile=None)})
        runner._async_session_store = SimpleNamespace(_store=runner.session_store, _ensure_loaded=AsyncMock())
    async def present(fn, **kw):
        await fn()
        return True
    monkeypatch.setattr("gateway.warning_notifications.present_notification", present)
    await runner._notify_active_sessions_of_shutdown()
    assert not primary.sent_calls
    assert len(named.sent_calls) == int(online)
