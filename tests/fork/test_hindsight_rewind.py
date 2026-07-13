"""Fork-owned Hindsight rewind regressions for /undo paths."""

from types import SimpleNamespace
import threading

import pytest

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import AsyncSessionStore, SessionSource, build_session_key
from tests.gateway.test_undo_rewind_session import store as store  # re-export pytest fixture
from tests.tui_gateway.test_undo_command import (
    _call,
    db as db,
    hermes_home as hermes_home,
    server as server,
    session_with_history as session_with_history,
)


class _RecordingRewindProvider(MemoryProvider):
    @property
    def name(self) -> str:
        return "rec"

    def __init__(self):
        self.rewind_calls = []

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id="", **kwargs):
        pass

    def get_tool_schemas(self):
        return []

    def handle_tool_call(self, *args, **kwargs):
        return "{}"

    def sync_turn(self, *args, **kwargs):
        pass

    def on_session_rewind(self, session_id, turns_undone=1, **kwargs):
        self.rewind_calls.append(
            {"rewind": session_id, "turns_undone": turns_undone, "extra": kwargs}
        )


def test_manager_rewind_fans_out_to_provider_rewind_hook():
    mm = MemoryManager()
    p = _RecordingRewindProvider()
    mm.add_provider(p)

    mm.on_session_rewind("sess-1", turns_undone=3, reason="undo")

    assert p.rewind_calls == [
        {"rewind": "sess-1", "turns_undone": 3, "extra": {"reason": "undo"}}
    ]


@pytest.mark.asyncio
async def test_gateway_undo_notifies_cached_agent_memory_rewind(store):
    from gateway.slash_commands import GatewaySlashCommandsMixin

    class _Runner(GatewaySlashCommandsMixin):
        def __init__(self):
            self.session_store = store
            # Upstream gateway undo now goes through AsyncSessionStore.
            self.async_session_store = AsyncSessionStore(store)
            self._agent_cache = {}
            self._agent_cache_lock = threading.Lock()
            self.evicted = []

        def _evict_cached_agent(self, session_key):
            self.evicted.append(session_key)
            self._agent_cache.pop(session_key, None)

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        user_id="user-1",
        user_name="User One",
    )
    session_key = build_session_key(source)
    entry = store.get_or_create_session(source)
    for i in range(1, 4):
        store._db.append_message(entry.session_id, "user", f"q{i}")
        store._db.append_message(entry.session_id, "assistant", f"a{i}")
    mm = SimpleNamespace(on_session_rewind=lambda *args, **kwargs: None)
    calls = []
    mm.on_session_rewind = lambda *args, **kwargs: calls.append((args, kwargs))
    agent = SimpleNamespace(_memory_manager=mm)
    runner = _Runner()
    runner._agent_cache[session_key] = (agent, object())
    event = MessageEvent(text="/undo 2", message_type=MessageType.COMMAND, source=source)

    result = await runner._handle_undo_command(event)

    assert "Undid 2" in result or "撤销" in result
    assert calls == [((entry.session_id,), {"turns_undone": 2})]
    assert runner.evicted == [session_key]


@pytest.mark.asyncio
async def test_gateway_undo_marks_hindsight_rows_without_cached_agent(store, monkeypatch, tmp_path):
    from gateway.slash_commands import GatewaySlashCommandsMixin

    class _Runner(GatewaySlashCommandsMixin):
        def __init__(self):
            self.session_store = store
            # Upstream gateway undo now goes through AsyncSessionStore.
            self.async_session_store = AsyncSessionStore(store)
            self._agent_cache = {}
            self._agent_cache_lock = threading.Lock()
            self.evicted = []

        def _evict_cached_agent(self, session_key):
            self.evicted.append(session_key)
            self._agent_cache.pop(session_key, None)

    class _Provider:
        def __init__(self):
            self.initialize_calls = []
            self.rewind_calls = []
            self.shutdown_calls = 0

        def is_available(self):
            return True

        def initialize(self, **kwargs):
            self.initialize_calls.append(kwargs)

        def on_session_rewind(self, *args, **kwargs):
            self.rewind_calls.append((args, kwargs))

        def shutdown(self):
            self.shutdown_calls += 1

    provider = _Provider()
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"memory": {"provider": "hindsight"}})
    monkeypatch.setattr("hermes_cli.config.cfg_get", lambda config, *keys: config.get("memory", {}).get("provider"))
    monkeypatch.setattr("plugins.memory.load_memory_provider", lambda name: provider if name == "hindsight" else None)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-2",
        user_id="user-2",
        user_name="User Two",
    )
    session_key = build_session_key(source)
    entry = store.get_or_create_session(source)
    for i in range(1, 4):
        store._db.append_message(entry.session_id, "user", f"q{i}")
        store._db.append_message(entry.session_id, "assistant", f"a{i}")
    runner = _Runner()
    event = MessageEvent(text="/undo 2", message_type=MessageType.COMMAND, source=source)

    result = await runner._handle_undo_command(event)

    assert "Undid 2" in result or "撤销" in result
    assert provider.initialize_calls
    assert provider.initialize_calls[0]["session_id"] == entry.session_id
    assert provider.initialize_calls[0]["gateway_session_key"] == session_key
    assert provider.rewind_calls == [((entry.session_id,), {"turns_undone": 2})]
    assert provider.shutdown_calls == 1
    assert runner.evicted == [session_key]


def test_tui_undo_notifies_memory_provider_rewind(server, session_with_history):
    sid, session_key, _, agent = session_with_history
    _call(server, "command.dispatch", session_id=sid, name="undo", arg="")
    agent._memory_manager.on_session_rewind.assert_called_once_with(
        session_key,
        turns_undone=1,
    )
