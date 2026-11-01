"""Preserve the approved reset handoff at the relocated official reset boundary.

Uses real persistence-mixin methods and faked reset/storage boundaries. This is
not the complete live compression-to-plugin-recovery acceptance test.
"""
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gateway.config import Platform
from gateway.run_turn import GatewayTurnMixin
from gateway.session import SessionSource


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_ok", [True, False, None])
@pytest.mark.parametrize("deferred", [False, True])
async def test_auto_reset_restores_context_before_releasing_fresh_session(monkeypatch, restore_ok, deferred):
    from gateway import session_automation
    from fork_features.request_fork import session_recovery

    events = []
    old = SimpleNamespace(session_id="physical-old")
    fresh = SimpleNamespace(session_id="physical-new")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat")
    host = cast(Any, GatewayTurnMixin())
    host.async_session_store = object()
    deferred_resets = []

    def defer(entry, route, reset_source):
        if deferred:
            deferred_resets.append(lambda: host._hmwa_rotate_exhausted_session(entry, route, reset_source))
        return deferred

    host._hmwa_defer_exhaustion_reset = defer
    host._evict_cached_agent = lambda *args: events.append("evict")
    host._overflow_queue = lambda *args: []
    host._clear_conversation_scope = lambda *args, **kw: events.append("clear")
    host._sync_telegram_topic_binding = lambda *args, **kw: events.append("binding")

    async def reset(*args):
        events.append("reset")
        return fresh, lambda: events.append("release")

    async def move_wakes(*args):
        events.append("wakes")

    async def restore(store, *, old_session_id, new_session_id, platform):
        assert store is host.async_session_store
        assert (old_session_id, new_session_id, platform) == ("physical-old", "physical-new", "telegram")
        events.append("restore")
        return restore_ok

    monkeypatch.setattr(session_automation, "reset_and_rehome_messages", reset)
    monkeypatch.setattr(session_automation, "move_route_wakes", move_wakes)
    monkeypatch.setattr(session_recovery, "restore_reset_context", restore)
    response, entry = await host._hmwa_compression_exhaustion_reset(
        {"compression_exhausted": True}, "answer", old, "route", source,
    )
    if deferred:
        from gateway.session_authority import SessionAuthority
        assert entry is old and not events
        assert len(deferred_resets) == 1
        await SessionAuthority._run_deferred_reset("route", deferred_resets[0])
    assert "restore" in events
    assert events.index("clear") < events.index("restore") < events.index("binding") < events.index("release")
    assert events.count("release") == 1
    assert entry is (old if deferred else fresh)
    assert ("Session recovery context could not be saved." in response) is (restore_ok is False and not deferred)


@pytest.mark.asyncio
async def test_compression_lock_defer_does_not_reset_or_restore(monkeypatch):
    from fork_features.request_fork import session_recovery
    from gateway import session_automation

    async def forbidden(*args, **kwargs):
        raise AssertionError("A deferred compressor must not rotate or restore")

    monkeypatch.setattr(session_recovery, "restore_reset_context", forbidden)
    monkeypatch.setattr(session_automation, "reset_and_rehome_messages", forbidden)
    host = GatewayTurnMixin()
    old = SimpleNamespace(session_id="physical-old")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat")
    assert await host._hmwa_compression_exhaustion_reset(
        {"compression_deferred": True, "compression_exhausted": True}, "answer", old, "route", source,
    ) == ("answer", old)
