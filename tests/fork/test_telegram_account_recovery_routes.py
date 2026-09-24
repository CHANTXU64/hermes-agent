"""Same-profile named bots keep their identity through durable delivery paths."""
from unittest.mock import AsyncMock

import pytest

from gateway import delivery_ledger as dl
from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import build_session_key
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source, RestartTestAdapter


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(dl, "ledger_enabled", lambda: True)
    monkeypatch.setattr(dl, "_owner_stamp", lambda: (999999999, 100))
    runner, primary = make_restart_runner()
    runner._profile_adapters = {}
    runner._primary_profile_name = "default"
    runner._clear_resume_pending_for_claimed_obligations = AsyncMock(side_effect=lambda rows, **kw: rows)
    named = RestartTestAdapter()
    named.config.extra["account_id"] = "work"
    named.gateway_runner = primary.gateway_runner = runner
    runner._telegram_accounts.live["work"] = named
    return runner, primary, named


def source(account="work"):
    src = make_restart_source(chat_id="42")
    src.account_id = account
    return src


async def record(named, account: str | None = "work", error="send_path_degraded"):
    src = source(account)
    event = MessageEvent(text="request", message_type=MessageType.TEXT, source=src, message_id="m1")
    oid = await named._record_delivery_obligation(event, build_session_key(src), "owed reply", named, False)
    assert oid
    dl.mark_failed(oid, error)
    return oid


def row(oid):
    conn = dl._connect()
    try:
        return conn.execute("SELECT state, attempts FROM delivery_obligations WHERE obligation_id=?", (oid,)).fetchone()
    finally:
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", [False, True])
@pytest.mark.parametrize("primary_online", [False, True])
async def test_ledger_redelivery_uses_original_named_bot(rig, monkeypatch, runtime, primary_online):
    runner, primary, named = rig
    if not primary_online:
        runner.adapters.clear()
    oid = await record(named)
    monkeypatch.setattr(dl, "_owner_alive", lambda *a: False)
    if runtime:
        delivered = await runner._redeliver_failed_obligations_for_platform(Platform.TELEGRAM)
    else:
        delivered = await runner._redeliver_pending_obligations()
    assert delivered == 1
    assert len(named.sent_calls) == 1
    assert not primary.sent_calls
    assert row(oid)[0] == "delivered"


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", [False, True])
async def test_offline_named_ledger_waits_without_spending_attempts(rig, monkeypatch, runtime):
    runner, primary, named = rig
    oid = await record(named)
    runner._telegram_accounts.live.clear()
    monkeypatch.setattr(dl, "_owner_alive", lambda *a: False)
    for _ in range(dl.MAX_ATTEMPTS + 1):
        if runtime:
            assert await runner._redeliver_failed_obligations_for_platform(Platform.TELEGRAM) == 0
        else:
            assert await runner._redeliver_pending_obligations() == 0
    assert row(oid) == ("failed", 0)
    assert not primary.sent_calls
    runner._telegram_accounts.live["work"] = named
    assert await runner._redeliver_pending_obligations() == 1
    assert len(named.sent_calls) == 1


@pytest.mark.asyncio
async def test_named_reconnect_replays_owed_reply(rig):
    runner, primary, named = rig
    oid = await record(named)
    runner._telegram_accounts.live.clear()
    runner._telegram_accounts.failed["work"] = {"config": named.config, "attempts": 0, "next_retry": 0}
    runner._create_adapter = lambda *a: named
    runner._connect_adapter_with_timeout = AsyncMock(return_value=True)
    runner._sync_voice_mode_state_to_adapter = lambda *a: None
    runner._update_platform_runtime_status = lambda *a, **kw: None
    runner._busy_text_mode = "interrupt"
    runner._handle_message = AsyncMock()
    runner._make_adapter_auth_check = lambda *a: (lambda *a: True)
    await runner._telegram_accounts.reconnect_failed(now=100)
    assert row(oid)[0] == "delivered"
    assert len(named.sent_calls) == 1
    assert not primary.sent_calls


@pytest.mark.asyncio
async def test_primary_ledger_route_is_unchanged(rig, monkeypatch):
    runner, primary, named = rig
    oid = await record(primary, account=None)
    monkeypatch.setattr(dl, "_owner_alive", lambda *a: False)
    assert await runner._redeliver_pending_obligations() == 1
    assert len(primary.sent_calls) == 1
    assert not named.sent_calls
    assert row(oid)[0] == "delivered"


@pytest.mark.asyncio
async def test_named_online_does_not_spend_offline_primary_budget(rig, monkeypatch):
    runner, primary, named = rig
    oid = await record(primary, account=None)
    runner.adapters.clear()
    monkeypatch.setattr(dl, "_owner_alive", lambda *a: False)
    assert await runner._redeliver_pending_obligations() == 0
    assert row(oid) == ("failed", 0)
    assert not primary.sent_calls and not named.sent_calls


@pytest.mark.asyncio
async def test_flood_timer_does_not_spin_on_offline_named_account(rig):
    from unittest.mock import MagicMock
    runner, primary, named = rig
    await record(named, error="flood_control:1")
    runner._schedule_flood_redelivery = MagicMock()
    runner._telegram_accounts.live.clear()
    await runner._arm_flood_timers_for_waiting_rows()
    runner._schedule_flood_redelivery.assert_not_called()
    runner._telegram_accounts.live["work"] = named
    await runner._arm_flood_timers_for_waiting_rows()
    runner._schedule_flood_redelivery.assert_called_once_with("telegram", profile="default")
