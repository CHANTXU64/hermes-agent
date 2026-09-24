"""Kanban creator notifications and wakes must return to the originating bot."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.session import build_session_key
from gateway.kanban_watchers_notifier import _adapter_for_subscription, _KanbanNotification
from hermes_cli import kanban_db as kb, kanban_db_notify as notify_db
from hermes_cli.kanban_db_connect import connect
from tools.kanban_tools import _resolve_notify_target
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source, RestartTestAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [None, "work", "alerts"])
@pytest.mark.parametrize("online", [True, False])
async def test_creator_route_survives_subscription_storage_and_wake(tmp_path, monkeypatch, account, online):
    runner, primary = make_restart_runner()
    named = RestartTestAdapter()
    named.config.extra["account_id"] = account
    runner._profile_adapters = {}
    runner._resolve_profile_home_for_source = lambda s: tmp_path
    runner._deliver_kanban_artifacts = AsyncMock()
    if account and online:
        runner._telegram_accounts.live[account] = named
    source = make_restart_source(chat_id="42")
    source.account_id = account
    key = build_session_key(source)
    context = {"HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_ID": "42",
               "HERMES_SESSION_CHAT_TYPE": "dm", "HERMES_SESSION_KEY": key,
               "HERMES_SESSION_PROFILE": "default", "HERMES_SESSION_USER_ID": "u1"}
    monkeypatch.setattr("gateway.session_context.get_session_env", lambda k, default="": context.get(k, default))
    target = _resolve_notify_target()
    # Persist via the real DB contract, then reconstruct exactly what the notifier consumes.
    conn = connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="test", assignee="worker")
        task = kb.get_task(conn, task_id)
        assert target is not None
        notify_db.add_notify_sub(conn, task_id=task_id, **target)
        sub = notify_db.list_notify_subs(conn, task_id)[0]
    finally:
        conn.close()
    adapter = _adapter_for_subscription(runner, Platform.TELEGRAM, sub, "default")
    if account and not online:
        assert adapter is None
        return
    assert adapter is (named if account else primary)
    notification = _KanbanNotification(runner, {"sub": sub, "task": task, "events": [], "cursor": 1},
                                       platform_cls=Platform, sub_fail_counts={})
    notification.plat = Platform.TELEGRAM
    notification.adapter = adapter
    notification.synth = "completed"
    notification.wake_diagnostic = False
    wakes = []
    async def admit(event):
        wakes.append((adapter, build_session_key(event.source)))
        event._gateway_accepted = True
    async def present(fn, **kwargs):
        await fn()
        return True
    monkeypatch.setattr(adapter, "handle_message", admit)
    monkeypatch.setattr("gateway.warning_notifications.present_notification", present)
    await notification._send_event(SimpleNamespace(kind="completed", payload={}), "completed")
    await notification.wake()
    assert wakes == [(adapter, key)]
    assert len(adapter.sent_calls) == 1
    assert not (primary if account else named).sent_calls
