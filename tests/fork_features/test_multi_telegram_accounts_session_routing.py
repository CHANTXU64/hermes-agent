from __future__ import annotations

from types import SimpleNamespace

import pytest

from fork_features.multi_telegram_accounts.session_routing import (
    cleanup_detached_session_routes,
    plan_cross_account_resume,
    switch_resumed_session,
)


class Store:
    def __init__(self, route_keys):
        self.route_keys = route_keys
        self.calls: list[tuple] = []

    async def routing_keys_for_session_id(self, target_id):
        self.calls.append(("routes", target_id))
        return list(self.route_keys)

    async def transfer_session(self, session_key, target_id):
        self.calls.append(("transfer", session_key, target_id))
        return SimpleNamespace(session_id=target_id), [self.route_keys[0]]

    async def switch_session(self, session_key, target_id):
        self.calls.append(("switch", session_key, target_id))
        return SimpleNamespace(session_id=target_id)


@pytest.mark.asyncio
async def test_cross_account_resume_detects_running_route_without_rewriting_user_identity() -> None:
    base = "agent:main:telegram:dm:5612546357"
    work = f"{base}:account:work"
    home = f"{base}:account:home"
    store = Store([home, work])

    plan = await plan_cross_account_resume(
        store,
        session_key=work,
        target_id="session-1",
        running_session_keys={home},
    )

    assert plan.route_keys == (home,)
    assert plan.blocked_by_running is True


@pytest.mark.asyncio
async def test_cross_account_resume_transfers_but_same_account_resume_switches() -> None:
    base = "agent:main:telegram:dm:5612546357"
    work = f"{base}:account:work"
    home = f"{base}:account:home"

    cross_store = Store([home])
    cross_plan = await plan_cross_account_resume(
        cross_store,
        session_key=work,
        target_id="session-1",
        running_session_keys=set(),
    )
    entry, detached = await switch_resumed_session(
        cross_store,
        session_key=work,
        target_id="session-1",
        plan=cross_plan,
    )
    assert entry.session_id == "session-1"
    assert detached == [home]
    assert ("transfer", work, "session-1") in cross_store.calls

    same_store = Store([work])
    same_plan = await plan_cross_account_resume(
        same_store,
        session_key=work,
        target_id="session-2",
        running_session_keys=set(),
    )
    entry, detached = await switch_resumed_session(
        same_store,
        session_key=work,
        target_id="session-2",
        plan=same_plan,
    )
    assert entry.session_id == "session-2"
    assert detached == []
    assert ("switch", work, "session-2") in same_store.calls


def test_cleanup_detached_routes_uses_existing_conversation_boundary() -> None:
    calls: list[tuple] = []
    host = SimpleNamespace(
        _release_running_agent_state=lambda key: calls.append(("release", key)),
        _clear_conversation_scope=lambda key, reason: calls.append(
            ("clear", key, reason)
        ),
        _evict_cached_agent=lambda key: calls.append(("evict", key)),
        _pending_messages={"old": ["message"]},
        _pending_native_image_paths_by_session={"old": ["image"]},
    )

    cleanup_detached_session_routes(host, ["old"])

    assert calls == [
        ("release", "old"),
        ("clear", "old", "resume_transfer"),
        ("evict", "old"),
    ]
    assert host._pending_messages == {}
    assert host._pending_native_image_paths_by_session == {}
