"""Fork-owned end-to-end Hindsight retain/document flow regressions.

These tests exercise the real Hermes boundaries that feed Hindsight retain
content. They deliberately use fake Hindsight clients and temp SessionDB/state
stores only: no LLM, OpenAI/Codex, Hindsight API, or external network calls.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.memory_manager import MemoryManager
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, SessionStore, build_session_key
from hermes_state import SessionDB
from plugins.memory.hindsight import HindsightMemoryProvider


_NETWORK_ENV_KEYS = (
    "HINDSIGHT_API_KEY",
    "HINDSIGHT_API_URL",
    "HINDSIGHT_BANK_ID",
    "HINDSIGHT_BUDGET",
    "HINDSIGHT_MODE",
    "HINDSIGHT_TIMEOUT",
    "HINDSIGHT_IDLE_TIMEOUT",
    "HINDSIGHT_LLM_API_KEY",
    "HINDSIGHT_RETAIN_TAGS",
    "HINDSIGHT_RETAIN_OBSERVATION_SCOPES",
    "HINDSIGHT_RETAIN_SOURCE",
    "HINDSIGHT_RETAIN_USER_PREFIX",
    "HINDSIGHT_RETAIN_ASSISTANT_PREFIX",
)


def _local_seconds(value):
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, timezone.utc)
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.astimezone().replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")


def _fake_hindsight_client():
    async def _no_real_retain(**_kwargs):
        return SimpleNamespace(ok=True)

    client = MagicMock(name="fake_hindsight_client")
    client.aretain_batch = AsyncMock(side_effect=_no_real_retain)
    client.aretain = AsyncMock(side_effect=AssertionError("real Hindsight retain must not be called"))
    client.arecall = AsyncMock(side_effect=AssertionError("real Hindsight recall must not be called"))
    client.areflect = AsyncMock(side_effect=AssertionError("real Hindsight reflect must not be called"))
    client.aclose = AsyncMock()
    return client


def _initialized_hindsight_provider(
    tmp_path,
    monkeypatch,
    *,
    session_id: str,
    parent_session_id: str = "",
    **config_overrides,
):
    """Build a real HindsightMemoryProvider backed only by tmp_path + fake client."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for key in _NETWORK_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    import plugins.memory.hindsight as hindsight_mod

    with hindsight_mod._append_capability_lock:
        hindsight_mod._append_capability_cache.clear()

    monkeypatch.setattr(hindsight_mod, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(hindsight_mod, "_fetch_hindsight_api_version", lambda *a, **kw: "0.5.6")
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "999.0.0")

    def _forbid_real_client_creation():
        raise AssertionError("real Hindsight client dependency/client creation attempted")

    monkeypatch.setattr(hindsight_mod, "_ensure_cloud_client_dependency", _forbid_real_client_creation)

    config = {
        "mode": "cloud",
        "apiKey": "fake-test-key",
        "api_url": "http://127.0.0.1:9",
        "bank_id": "test-bank",
        "budget": "mid",
        "memory_mode": "hybrid",
        "auto_retain": False,
        "auto_recall": False,
        "retain_async": False,
        "retain_context": "conversation between Hermes Agent and the User",
    }
    config.update(config_overrides)
    config_path = tmp_path / "hindsight" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config), encoding="utf-8")

    provider = HindsightMemoryProvider()
    provider.initialize(
        session_id=session_id,
        parent_session_id=parent_session_id,
        hermes_home=str(tmp_path),
        platform="telegram",
        user_id="user-1",
        user_name="User One",
        chat_id="chat-1",
        chat_type="dm",
    )
    client = _fake_hindsight_client()
    provider._client = client
    return provider, client


def test_hindsight_document_original_text_starts_from_real_first_user_turn_after_gateway_interrupt_multi_user_turn_flow_via_agent_sync(
    tmp_path,
    monkeypatch,
):
    """AIAgent -> MemoryManager -> Hindsight retain store must retain A before B.

    Business acceptance: when one logical gateway session first receives the
    real user request A, then an interrupt/correction user message B arrives
    before the assistant completes, the retained Hindsight Document original_text
    must start at A, not at B. The flow also filters tool-call shells, tool
    output, recent summaries, interrupt notices, empty assistant messages, and
    intermediate drafts.
    """
    session_id = "gateway-interrupt-flow-session"
    real_first_user = "real first user A: reconcile the project screenshots"
    corrective_user = "interrupt correction user B: stop and explain current status"
    final_answer = "final assistant answer after B"
    provider, client = _initialized_hindsight_provider(
        tmp_path,
        monkeypatch,
        session_id=session_id,
    )
    manager = MemoryManager()
    manager.add_provider(provider)

    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    setattr(agent, "_memory_manager", manager)
    setattr(agent, "session_id", session_id)

    messages = [
        {"role": "user", "content": real_first_user, "timestamp": 1710000000.0},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "ocr", "arguments": "{}"}}],
            "finish_reason": "tool_calls",
            "timestamp": 1710000001.0,
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "TOOL OUTPUT THAT MUST NOT BE RETAINED", "timestamp": 1710000002.0},
        {"role": "assistant", "content": "[Recent Summary (d0)]\nSUMMARY THAT MUST NOT BE RETAINED", "timestamp": 1710000003.0},
        {"role": "assistant", "content": "Operation interrupted: waiting for model response", "timestamp": 1710000003.5},
        {"role": "user", "content": "[Recent Summary (d0)]\nUSER SUMMARY THAT MUST NOT BE RETAINED", "timestamp": 1710000003.6},
        {"role": "user", "content": corrective_user, "timestamp": "2024-03-09T16:00:04+00:00"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-2", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}],
            "finish_reason": "tool_calls",
            "timestamp": "2024-03-09T16:00:05+00:00",
        },
        {"role": "tool", "tool_call_id": "call-2", "content": "SECOND TOOL OUTPUT THAT MUST NOT BE RETAINED", "timestamp": "2024-03-09T16:00:06+00:00"},
        {"role": "assistant", "content": "intermediate assistant draft that must not become original_text", "timestamp": "2024-03-09T16:00:07+00:00"},
        {"role": "assistant", "content": "", "timestamp": "2024-03-09T16:00:08+00:00"},
        {"role": "assistant", "content": final_answer, "timestamp": "2024-03-09T16:00:09+00:00"},
    ]

    try:
        agent._sync_external_memory_for_turn(
            original_user_message=corrective_user,
            final_response=final_answer,
            interrupted=False,
            messages=messages,
        )
        assert manager.flush_pending(timeout=5), "memory sync worker did not drain"

        info = provider.retain_persisted_session_lineage(session_id=session_id)
        provider._retain_queue.join()

        assert info["queued"] is True
        assert info["turn_count"] == 2, (
            "Hindsight Document original_text should contain the orphan real first user turn A "
            "and the completed correction turn B after gateway interrupt / multi-user-turn flow"
        )
        kwargs = client.aretain_batch.call_args.kwargs
        assert kwargs["bank_id"] == "test-bank"
        assert kwargs["document_id"] == session_id
        assert kwargs["retain_async"] is False
        item = kwargs["items"][0]
        assert item["update_mode"] == "replace"
        assert item["context"] == "conversation between Hermes Agent and the User"

        content = item["content"]
        turns = json.loads(content)
        assert turns[0][0]["content"] == f"User: {real_first_user}", (
            "Hindsight Document original_text starts from real first user turn after gateway "
            "interrupt / multi-user-turn flow; it must not start from the later correction B. "
            f"First retained message was: {turns[0][0].get('content')!r}"
        )
        assert len(turns[0]) == 1, "orphan user A should be retained as a user-only first turn"
        assert turns[0][0]["timestamp"] == _local_seconds(1710000000.0)
        assert turns[1][0]["content"] == f"User: {corrective_user}"
        assert turns[1][0]["timestamp"] == _local_seconds("2024-03-09T16:00:04+00:00")
        assert turns[1][1]["content"] == f"Assistant: {final_answer}"
        assert turns[1][1]["timestamp"] == _local_seconds("2024-03-09T16:00:09+00:00")
        for forbidden in (
            "TOOL OUTPUT THAT MUST NOT BE RETAINED",
            "SECOND TOOL OUTPUT THAT MUST NOT BE RETAINED",
            "SUMMARY THAT MUST NOT BE RETAINED",
            "USER SUMMARY THAT MUST NOT BE RETAINED",
            "Operation interrupted",
            "intermediate assistant draft",
            "tool_calls",
        ):
            assert forbidden not in content
    finally:
        manager.shutdown_all()


def test_hindsight_transcript_replay_after_provider_restart_dedupes_existing_persisted_turns(
    tmp_path,
    monkeypatch,
):
    """A restarted provider must not duplicate already-persisted active turns.

    The memory manager can pass the full clean transcript on each completed
    turn.  If the Hindsight provider is re-created after restart/compression,
    its in-memory ``_session_turns`` buffer is empty while ``retain_turns``
    already contains active rows for the same logical document.  Replaying the
    full transcript must append only the new tail turn.
    """
    session_id = "restart-replay-dedupe-session"
    messages_ab = [
        {"role": "user", "content": "first request before restart", "timestamp": 1710000100.0},
        {"role": "assistant", "content": "first answer before restart", "timestamp": 1710000101.0},
        {"role": "user", "content": "second request before restart", "timestamp": 1710000102.0},
        {"role": "assistant", "content": "second answer before restart", "timestamp": 1710000103.0},
    ]

    provider1, _client1 = _initialized_hindsight_provider(
        tmp_path,
        monkeypatch,
        session_id=session_id,
    )
    provider1.sync_turn(
        user_content="second request before restart",
        assistant_content="second answer before restart",
        session_id=session_id,
        messages=messages_ab,
    )
    provider1.shutdown()

    provider2, client2 = _initialized_hindsight_provider(
        tmp_path,
        monkeypatch,
        session_id=session_id,
    )
    messages_abc = [
        *messages_ab,
        {"role": "user", "content": "third request after restart", "timestamp": 1710000104.0},
        {"role": "assistant", "content": "third answer after restart", "timestamp": 1710000105.0},
    ]

    try:
        provider2.sync_turn(
            user_content="third request after restart",
            assistant_content="third answer after restart",
            session_id=session_id,
            messages=messages_abc,
        )
        info = provider2.retain_persisted_session_lineage(session_id=session_id)
        provider2._retain_queue.join()

        assert info["queued"] is True
        assert info["turn_count"] == 3, (
            "replayed transcript after provider restart should not duplicate "
            "turns already present in retain_turns.sqlite3"
        )
        content = client2.aretain_batch.call_args.kwargs["items"][0]["content"]
        turns = json.loads(content)
        retained_user_messages = [turn[0]["content"] for turn in turns]
        assert retained_user_messages == [
            "User: first request before restart",
            "User: second request before restart",
            "User: third request after restart",
        ]
        assert content.count("User: first request before restart") == 1
        assert content.count("User: second request before restart") == 1
        assert content.count("User: third request after restart") == 1
    finally:
        provider2.shutdown()


@pytest.mark.asyncio
async def test_gateway_retain_document_uses_sessionstore_sessiondb_lineage_and_clean_persisted_turns(
    tmp_path,
    monkeypatch,
):
    """Gateway /retain must retain provider-owned lineage turns, not noisy SessionDB transcript."""
    from gateway.run import GatewayRunner

    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr("hermes_state.SessionDB", lambda *a, **kw: db)
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    store._db = db

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="dm",
        user_id="user-1",
        user_name="User One",
    )
    session_key = build_session_key(source)
    root_session_id = "root-retain-session"
    child_session_id = "child-retain-session"
    now = datetime.now()
    entry = SessionEntry(
        session_key=session_key,
        session_id=child_session_id,
        created_at=now,
        updated_at=now,
        origin=source,
        display_name="User One",
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    store._loaded = True
    store._entries[session_key] = entry

    db.create_session(
        root_session_id,
        source="telegram",
        user_id="user-1",
        session_key=session_key,
        chat_id="chat-1",
        chat_type="dm",
    )
    db.create_session(
        child_session_id,
        source="telegram",
        user_id="user-1",
        session_key=session_key,
        chat_id="chat-1",
        chat_type="dm",
        parent_session_id=root_session_id,
    )
    db.append_message(root_session_id, "user", "SESSIONDB root user")
    db.append_message(root_session_id, "assistant", "SESSIONDB root assistant")
    db.append_message(child_session_id, "assistant", "[Recent Summary (d0)]\nSESSIONDB SUMMARY MUST NOT BE RETAINED")
    db.append_message(
        child_session_id,
        "assistant",
        "",
        tool_calls=[{"id": "call-noisy", "type": "function", "function": {"name": "browser", "arguments": "{}"}}],
        finish_reason="tool_calls",
    )
    db.append_message(child_session_id, "tool", "SESSIONDB TOOL OUTPUT MUST NOT BE RETAINED", tool_call_id="call-noisy")
    db.append_message(child_session_id, "assistant", "SESSIONDB intermediate draft MUST NOT BE RETAINED")

    provider, client = _initialized_hindsight_provider(
        tmp_path,
        monkeypatch,
        session_id=root_session_id,
    )
    manager = MemoryManager()
    manager.add_provider(provider)
    provider.sync_turn("provider real first user", "provider root final", session_id=root_session_id)
    provider.on_session_switch(child_session_id, parent_session_id=root_session_id)
    provider.sync_turn("provider child user", "provider child final", session_id=child_session_id)
    client.aretain_batch.assert_not_called()

    original_retain = provider.retain_persisted_session_lineage
    provider.retain_persisted_session_lineage = MagicMock(wraps=original_retain)
    agent = SimpleNamespace(_memory_manager=manager, session_id=child_session_id)
    runner = SimpleNamespace(
        session_store=store,
        _running_agents={},
        _agent_cache={session_key: (agent, object())},
        _agent_cache_lock=threading.Lock(),
    )
    runner._handle_retain_command = GatewayRunner._handle_retain_command.__get__(runner, type(runner))
    event = MessageEvent(text="/retain", message_type=MessageType.COMMAND, source=source)

    try:
        result = await runner._handle_retain_command(event)
        provider._retain_queue.join()

        assert result == "Buffered session turns queued for retain."
        provider.retain_persisted_session_lineage.assert_called_once_with(
            session_id=child_session_id,
            parent_session_id=root_session_id,
        )
        kwargs = client.aretain_batch.call_args.kwargs
        assert kwargs["bank_id"] == "test-bank"
        assert kwargs["document_id"] == root_session_id
        assert kwargs["retain_async"] is False
        item = kwargs["items"][0]
        assert item["update_mode"] == "replace"
        assert item["context"] == "conversation between Hermes Agent and the User"

        content = item["content"]
        turns = json.loads(content)
        assert [[m["content"] for m in turn] for turn in turns] == [
            ["User: provider real first user", "Assistant: provider root final"],
            ["User: provider child user", "Assistant: provider child final"],
        ]
        for turn in turns:
            for message in turn:
                assert message["timestamp"], "retained document messages must include timestamps"
        for forbidden in (
            "SESSIONDB SUMMARY MUST NOT BE RETAINED",
            "SESSIONDB TOOL OUTPUT MUST NOT BE RETAINED",
            "SESSIONDB intermediate draft MUST NOT BE RETAINED",
            "SESSIONDB root user",
            "tool_calls",
        ):
            assert forbidden not in content
    finally:
        manager.shutdown_all()
