"""Tests for SessionStore.rewind_session — the gateway /undo [N] primitive.

The gateway /undo backs up N user turns by soft-deleting the truncated rows
in state.db (active=0, kept for audit, hidden from re-prompts/search) via
SessionDB.rewind_to_message, rather than the old hard rewrite_transcript.
load_transcript returns only the active view. See issue #21910.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from hermes_state import SessionDB
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key
from gateway.session import SessionStore


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db = SessionDB(db_path=tmp_path / "state.db")
    s = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    s._db = db  # use the same DB instance the fixture seeds
    return s


def _seed(store, sid, source="telegram", turns=3):
    store._db.create_session(sid, source=source)
    for i in range(1, turns + 1):
        store._db.append_message(sid, "user", f"q{i}")
        store._db.append_message(sid, "assistant", f"a{i}")
    return sid


def test_rewind_default_one_turn(store):
    sid = _seed(store, "gw-1")
    res = store.rewind_session(sid)
    assert res["turns_undone"] == 1
    assert res["target_text"] == "q3"
    assert res["rewound_count"] == 2  # q3 + a3
    active = store.load_transcript(sid)
    assert [m["role"] for m in active] == ["user", "assistant", "user", "assistant"]


def test_rewind_n_turns(store):
    sid = _seed(store, "gw-2")
    res = store.rewind_session(sid, 2)
    assert res["turns_undone"] == 2
    assert res["target_text"] == "q2"
    assert res["rewound_count"] == 4  # q2,a2,q3,a3
    assert len(store.load_transcript(sid)) == 2  # q1,a1


def test_rewind_soft_deletes_rows_for_audit(store):
    sid = _seed(store, "gw-3")
    store.rewind_session(sid, 1)
    all_rows = store._db.get_messages(sid, include_inactive=True)
    assert len(all_rows) == 6  # nothing hard-deleted
    assert sum(1 for r in all_rows if r["active"] == 1) == 4
    assert store._db.get_session(sid)["rewind_count"] == 1


def test_rewind_clamps_to_oldest_turn(store):
    sid = _seed(store, "gw-4", turns=2)
    res = store.rewind_session(sid, 99)
    assert res["target_text"] == "q1"
    assert len(store.load_transcript(sid)) == 0


def test_rewind_empty_session_returns_none(store):
    store._db.create_session("gw-5", source="discord")
    assert store.rewind_session("gw-5") is None


def test_rewind_clamps_negative_count_to_one(store):
    sid = _seed(store, "gw-6")
    res = store.rewind_session(sid, -5)
    assert res["turns_undone"] == 1
    assert res["target_text"] == "q3"


@pytest.mark.asyncio
async def test_gateway_undo_notifies_cached_agent_memory_rewind(store):
    from gateway.slash_commands import GatewaySlashCommandsMixin

    class _Runner(GatewaySlashCommandsMixin):
        def __init__(self):
            self.session_store = store
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
