"""Fixed wall-clock schedules must use the normal session heartbeat path."""
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from gateway.platforms.event import MessageEvent
from gateway.slash_commands_goals import GatewayGoalCommandsMixin
from hermes_cli import goals, heartbeat


def epoch(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()


@pytest.fixture
def clock(monkeypatch):
    goals._DB_CACHE.clear()
    value = SimpleNamespace(now=epoch("2026-09-12T08:00:00"))
    monkeypatch.setattr(heartbeat, "time", SimpleNamespace(time=lambda: value.now))
    yield value
    for db in goals._DB_CACHE.values():
        if hasattr(db, "close"):
            db.close()
    goals._DB_CACHE.clear()


@pytest.mark.asyncio
async def test_gateway_daily_command_fires_only_at_four_times_in_own_session(clock):
    manager = heartbeat.HeartbeatManager("daily-session")
    watches = {}

    async def get_manager(event):
        return manager, None

    runner = SimpleNamespace(
        _get_heartbeat_manager_for_event=get_manager,
        _session_key_for_source=lambda source: "work-session-key",
        _register_heartbeat_watch=lambda key, source, sid: watches.update({key: sid}),
    )
    event = MessageEvent(
        text="/heartbeat daily 08:10,11:00,14:30,17:30 Asia/Shanghai 检查工作",
        source=SimpleNamespace(),
    )
    result = await GatewayGoalCommandsMixin._handle_heartbeat_command(runner, event)
    assert "♥" in result, result
    assert watches == {"work-session-key": "daily-session"}
    assert manager.due_prompt() is None
    for value in ["08:10", "11:00", "14:30", "17:30"]:
        clock.now = epoch(f"2026-09-12T{value}:00")
        # A fresh manager models restart/reload, not an in-memory timer.
        manager = heartbeat.HeartbeatManager("daily-session")
        assert "检查工作" in manager.due_prompt()
        assert manager.due_prompt() is None
        assert manager.due_prompt(clock.now + 59) is None
    assert heartbeat.HeartbeatManager("daily-session").state.fire_count == 4
    assert "17:30" in manager.status_line()
    assert "Asia/Shanghai" in manager.status_line()
    assert manager.due_prompt(epoch("2026-09-13T08:09:59")) is None
    assert manager.due_prompt(epoch("2026-09-13T08:10:00")) is not None


@pytest.mark.parametrize("spec,label", [
    ("daily 08:10,11:00 Asia/Shanghai", "daily 08:10,11:00 (Asia/Shanghai)"),
    ("weekly 08:10,11:00 12:00 Asia/Shanghai", "Mon-Fri 08:10,11:00; Sat-Sun 12:00 (Asia/Shanghai)"),
])
def test_cli_uses_same_daily_parser_and_resume_label(clock, monkeypatch, spec, label):
    import cli
    from hermes_cli import cli_commands_mixin as commands
    mgr = heartbeat.HeartbeatManager("cli-daily")
    messages, starts = [], []
    monkeypatch.setattr(commands, "_cp", lambda *args: messages.extend(args))
    instance = object.__new__(cli.HermesCLI)
    instance._get_heartbeat_manager = lambda: mgr
    instance._start_heartbeat_watchdog = lambda: starts.append(True)
    instance._handle_heartbeat_command(f"/heartbeat {spec} 检查")
    assert mgr.is_active(), messages
    assert len(starts) == 1
    instance._handle_heartbeat_command("/heartbeat pause")
    instance._handle_heartbeat_command("/heartbeat resume")
    assert label in messages[-1]


def test_repeated_enable_pause_resume_rotation_clear_and_legacy(clock):
    from fork_features.daily_heartbeat import configure_daily
    mgr = heartbeat.HeartbeatManager("lifecycle-daily")
    args = "daily 11:00,08:10,08:10 Asia/Shanghai 检查工作"
    configure_daily(mgr, args)
    initial = mgr.state.to_json()
    clock.now = epoch("2026-09-12T08:10:00")
    configure_daily(mgr, args)
    assert mgr.state.to_json() == initial
    assert mgr.due_prompt() is not None
    mgr.pause()
    clock.now = epoch("2026-09-12T12:00:00")
    assert mgr.due_prompt() is None
    mgr.resume()
    assert mgr.due_prompt() is None  # No stale 11:00 fire on resume.
    assert heartbeat.migrate_heartbeat_to_session("lifecycle-daily", "compressed-child")
    assert heartbeat.load_heartbeat("lifecycle-daily") is None
    child = heartbeat.HeartbeatManager("compressed-child")
    assert child.state.daily_times == ("08:10", "11:00")
    clock.now = epoch("2026-09-13T11:05:00")
    assert child.due_prompt() is not None  # Coalesce missed daily ticks.
    assert child.due_prompt() is None
    assert child.abandon_fire()  # Admission rejected: leave it due.
    assert child.due_prompt() is not None
    child.clear()
    assert heartbeat.HeartbeatManager("compressed-child").state is None
    child.set("legacy", 600)
    assert not child.state.daily_times
    assert not child.state.timezone
    assert child.due_prompt(clock.now + 600)


@pytest.mark.parametrize("args", [
    "daily", "daily 08:10 UTC", "daily 8:10 UTC p", "daily 24:00 UTC p",
    "daily 08:60 UTC p", "daily 08:10, UTC p", "daily 08:10 Bad/Timezone p",
    "weekly", "weekly 08:10 12:00 UTC", "weekly 08:10 24:00 UTC p",
    "weekly 08:10 12:00, UTC p", "weekly 08:10 12:00 Bad/Timezone p",
])
def test_invalid_daily_command_does_not_replace_existing_heartbeat(clock, args):
    from fork_features.daily_heartbeat import configure_daily
    mgr = heartbeat.HeartbeatManager("invalid-daily")
    before = mgr.set("existing", 600).to_json()
    with pytest.raises(ValueError):
        configure_daily(mgr, args)
    assert heartbeat.load_heartbeat("invalid-daily").to_json() == before


def test_wall_clock_ignores_host_timezone_and_skips_dst_gap():
    from fork_features.daily_heartbeat import next_daily_after
    zone = ZoneInfo("America/New_York")
    anchor = datetime(2026, 3, 8, 0, 0, tzinfo=zone).timestamp()
    actual = next_daily_after(anchor, ("02:30",), "America/New_York")
    assert datetime.fromtimestamp(actual, zone).isoformat() == "2026-03-09T02:30:00-04:00"


@pytest.mark.asyncio
async def test_weekly_gateway_schedule_weekdays_four_weekends_noon(clock):
    from datetime import timedelta
    clock.now = epoch("2026-09-14T08:00:00")
    manager = heartbeat.HeartbeatManager("weekly-session")

    async def get_manager(event):
        return manager, None

    runner = SimpleNamespace(
        _get_heartbeat_manager_for_event=get_manager,
        _session_key_for_source=lambda source: "weekly-key",
        _register_heartbeat_watch=lambda *args: None,
    )
    event = MessageEvent(
        text="/heartbeat weekly 08:10,11:00,14:30,17:30 12:00 Asia/Shanghai 检查工作",
        source=SimpleNamespace(),
    )
    result = await GatewayGoalCommandsMixin._handle_heartbeat_command(runner, event)
    assert "♥" in result, result
    assert "Mon-Fri" in result and "Sat-Sun" in result
    start = datetime(2026, 9, 14)
    counts = []
    for offset in range(7):
        day = (start + timedelta(days=offset)).date().isoformat()
        fired = []
        for value in ("08:10", "11:00", "12:00", "14:30", "17:30"):
            clock.now = epoch(f"{day}T{value}:00")
            manager = heartbeat.HeartbeatManager("weekly-session")
            if manager.due_prompt() is not None:
                fired.append(value)
            assert manager.due_prompt() is None
        assert fired == (["08:10", "11:00", "14:30", "17:30"] if offset < 5 else ["12:00"])
        counts.append(len(fired))
    assert counts == [4, 4, 4, 4, 4, 1, 1]
    assert manager.state.next_fire_at() == epoch("2026-09-21T08:10:00")


def test_weekly_does_not_replay_friday_on_saturday_morning(clock):
    from fork_features.daily_heartbeat import configure_daily
    clock.now = epoch("2026-09-11T14:31:00")
    manager = heartbeat.HeartbeatManager("weekly-no-catchup")
    args = "weekly 08:10,11:00,14:30,17:30 12:00 Asia/Shanghai p"
    configure_daily(manager, args)
    initial = manager.state.to_json()
    clock.now = epoch("2026-09-12T08:10:00")
    configure_daily(manager, args)
    assert manager.state.to_json() == initial  # Do not duplicate/reset on repeated enable.
    assert manager.due_prompt() is None  # Missed Friday is not a Saturday morning job.
    assert manager.state.next_fire_at() == epoch("2026-09-12T12:00:00")
    clock.now = epoch("2026-09-12T12:00:00")
    assert manager.due_prompt() is not None
    assert heartbeat.migrate_heartbeat_to_session("weekly-no-catchup", "weekly-child")
    clock.now = epoch("2026-09-13T08:10:00")
    child = heartbeat.HeartbeatManager("weekly-child")
    assert child.state.weekend_times == ("12:00",)
    assert child.due_prompt() is None
    clock.now = epoch("2026-09-13T12:00:00")
    assert child.due_prompt() is not None
    # Calendar holidays are deliberately not special-cased.
    clock.now = epoch("2026-10-01T08:00:00")
    child.resume()
    assert child.state.next_fire_at() == epoch("2026-10-01T08:10:00")


@pytest.mark.asyncio
async def test_daily_restart_idle_wake_and_new_session_boundary(tmp_path, clock):
    import asyncio
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter, SendResult
    from gateway.run import GatewayRunner
    from gateway.run_heartbeat_restore import restore_heartbeat_watches
    from gateway.session import SessionSource, SessionStore

    class LocalWire(BasePlatformAdapter):
        async def connect(self, *, is_reconnect=False):
            return True

        async def disconnect(self):
            pass

        async def get_chat_info(self, chat_id):
            return {}

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            return SendResult(success=True, message_id="test-only")

    config = GatewayConfig()
    store = SessionStore(tmp_path / "sessions", config)
    source = SessionSource(platform=Platform("weixin"), chat_id="offline-work", user_id="owner", chat_type="dm")
    entry = store.get_or_create_session(source)
    mgr = heartbeat.HeartbeatManager(entry.session_id)
    mgr.set_daily("read the original work context", ["08:10", "11:00", "14:30", "17:30"], "Asia/Shanghai")
    store.close_all_db_handles()
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = SessionStore(tmp_path / "sessions", config)
    runner._running_agents = {}
    runner._heartbeat_watch = {}
    runner._start_heartbeat_poller = lambda: None
    runner._run_in_executor_with_context = asyncio.to_thread
    adapter = LocalWire(PlatformConfig(enabled=True, typing_indicator=False), Platform("weixin"))
    runner._adapter_for_source = lambda source: adapter
    received = []

    async def handler(event):
        event._heartbeat_execution_started = True
        received.append(event)
        return None

    adapter.set_message_handler(handler)
    try:
        await restore_heartbeat_watches(runner)
        await restore_heartbeat_watches(runner)
        assert list(runner._heartbeat_watch) == [entry.session_key]
        await runner._heartbeat_poll_once(runner._heartbeat_watch)
        assert received == []
        clock.now = epoch("2026-09-12T08:10:00")
        runner._running_agents[entry.session_key] = object()
        await runner._heartbeat_poll_once(runner._heartbeat_watch)
        assert received == []
        runner._running_agents.clear()
        await runner._heartbeat_poll_once(runner._heartbeat_watch)
        await asyncio.gather(*adapter._background_tasks)
        assert len(received) == 1
        assert received[0]._heartbeat_session_id == entry.session_id
        assert received[0].source.chat_id == source.chat_id
        assert "read the original work context" in received[0].text
        replacement = runner.session_store.reset_session(entry.session_key)
        clock.now = epoch("2026-09-12T11:00:00")
        await runner._heartbeat_poll_once(runner._heartbeat_watch)
        assert len(received) == 1
        assert heartbeat.load_heartbeat(entry.session_id) is None
        assert heartbeat.load_heartbeat(replacement.session_id) is None
        # A new conversation explicitly enables its own schedule; no old owner survives.
        heartbeat.HeartbeatManager(replacement.session_id).set_daily("new context", ["14:30"], "Asia/Shanghai")
        await restore_heartbeat_watches(runner)
        clock.now = epoch("2026-09-12T14:30:00")
        await runner._heartbeat_poll_once(runner._heartbeat_watch)
        await asyncio.gather(*adapter._background_tasks)
        assert len(received) == 2
        assert received[-1]._heartbeat_session_id == replacement.session_id
    finally:
        runner.session_store.close_all_db_handles()
