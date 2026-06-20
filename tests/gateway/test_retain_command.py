"""Tests for the gateway /retain command."""

import threading
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event() -> MessageEvent:
    return MessageEvent(text="/retain", source=_make_source(), message_id="m1")


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner._running_agents = {}
    runner._agent_cache = OrderedDict()
    runner._agent_cache_lock = threading.Lock()
    return runner


@pytest.mark.asyncio
async def test_retain_command_uses_session_transcript_when_available():
    runner = _make_runner()
    source = _make_source()
    session_key = build_session_key(source)
    provider = MagicMock()
    provider.retain_conversation_messages.return_value = {"queued": True}
    memory_manager = SimpleNamespace(get_provider=lambda name: provider if name == "hindsight" else None)
    agent = SimpleNamespace(_memory_manager=memory_manager, session_id="sid-1")
    runner._agent_cache[session_key] = (agent, "sig")
    messages = [
        {"role": "user", "content": "leading interrupted user"},
        {"role": "user", "content": "next user"},
        {"role": "assistant", "content": "next assistant"},
    ]
    db = SimpleNamespace(get_session=lambda sid: {"parent_session_id": "parent-1"})
    runner.__dict__["session_store"] = SimpleNamespace(_db=db, load_transcript=MagicMock(return_value=messages))

    result = await runner._handle_retain_command(_make_event())

    assert result == "Buffered session turns queued for retain."
    provider.retain_conversation_messages.assert_called_once_with(
        messages,
        session_id="sid-1",
        parent_session_id="parent-1",
    )
    provider.retain_persisted_session_lineage.assert_not_called()
    provider.flush_retained_turns.assert_not_called()


@pytest.mark.asyncio
async def test_retain_command_uses_session_transcript_lineage_when_available():
    runner = _make_runner()
    source = _make_source()
    session_key = build_session_key(source)
    provider = MagicMock()
    provider.retain_conversation_messages.return_value = {"queued": True}
    memory_manager = SimpleNamespace(get_provider=lambda name: provider if name == "hindsight" else None)
    agent = SimpleNamespace(_memory_manager=memory_manager, session_id="child-sid")
    runner._agent_cache[session_key] = (agent, "sig")

    sessions = {
        "root-sid": {"parent_session_id": ""},
        "child-sid": {"parent_session_id": "root-sid"},
    }
    transcripts = {
        "root-sid": [
            {"role": "user", "content": "root upload"},
            {"role": "assistant", "content": "root ack"},
            {"role": "user", "content": "boundary instruction"},
            {"role": "assistant", "content": "boundary response"},
        ],
        "child-sid": [
            {"role": "assistant", "content": "[Recent Summary (d0)]\nsummary"},
            {"role": "user", "content": "boundary instruction"},
            {"role": "assistant", "content": "boundary response"},
            {"role": "user", "content": "child new"},
            {"role": "assistant", "content": "child response"},
        ],
    }
    db = SimpleNamespace(
        get_session=lambda sid: sessions.get(sid, {"parent_session_id": ""}),
        get_messages_as_conversation=MagicMock(side_effect=lambda sid, **kwargs: transcripts[sid]),
    )
    runner.__dict__["session_store"] = SimpleNamespace(_db=db)

    result = await runner._handle_retain_command(_make_event())

    assert result == "Buffered session turns queued for retain."
    provider.retain_conversation_messages.assert_called_once()
    messages = provider.retain_conversation_messages.call_args.args[0]
    assert [m["_session_id"] for m in messages] == [
        "root-sid", "root-sid", "root-sid", "root-sid",
        "child-sid", "child-sid", "child-sid", "child-sid", "child-sid",
    ]
    assert db.get_messages_as_conversation.call_args_list[0].kwargs == {"include_timestamps": True, "order_by": "id"}
    assert db.get_messages_as_conversation.call_args_list[1].kwargs == {"include_timestamps": True, "order_by": "id"}
    assert provider.retain_conversation_messages.call_args.kwargs == {
        "session_id": "child-sid",
        "parent_session_id": "root-sid",
    }


@pytest.mark.asyncio
async def test_retain_command_flushes_cached_agent_provider():
    runner = _make_runner()
    source = _make_source()
    session_key = build_session_key(source)
    provider = SimpleNamespace(flush_retained_turns=MagicMock(return_value={"queued": True}))
    memory_manager = SimpleNamespace(get_provider=lambda name: provider if name == "hindsight" else None)
    agent = SimpleNamespace(_memory_manager=memory_manager)
    runner._agent_cache[session_key] = (agent, "sig")

    result = await runner._handle_retain_command(_make_event())

    assert result == "Buffered session turns queued for retain."
    provider.flush_retained_turns.assert_called_once_with()


@pytest.mark.asyncio
async def test_retain_command_loads_hindsight_provider_from_current_session_after_resume(monkeypatch):
    runner = _make_runner()
    source = _make_source()
    session_key = build_session_key(source)
    session_entry = SimpleNamespace(session_key=session_key, session_id="resumed-sid")
    db = SimpleNamespace(
        get_session=lambda sid: {"parent_session_id": "parent-sid"},
        get_session_title=lambda sid: "Resumed Session",
    )
    store = SimpleNamespace(
        _db=db,
        get_or_create_session=MagicMock(return_value=session_entry),
    )
    runner.__dict__["session_store"] = store
    provider = MagicMock()
    provider.is_available.return_value = True
    provider.retain_persisted_session_lineage.return_value = {"queued": True}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"memory": {"provider": "hindsight"}})
    monkeypatch.setattr("plugins.memory.load_memory_provider", lambda name: provider if name == "hindsight" else None)

    result = await runner._handle_retain_command(_make_event())

    assert result == "Buffered session turns queued for retain."
    store.get_or_create_session.assert_called_once()
    provider.initialize.assert_called_once()
    init_kwargs = provider.initialize.call_args.kwargs
    assert init_kwargs["session_id"] == "resumed-sid"
    assert init_kwargs["gateway_session_key"] == session_key
    assert init_kwargs["session_title"] == "Resumed Session"
    provider.retain_persisted_session_lineage.assert_called_once_with(
        session_id="resumed-sid",
        parent_session_id="parent-sid",
    )


@pytest.mark.asyncio
async def test_retain_command_reports_no_hindsight_provider_in_chinese(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"memory": {"provider": "builtin"}})
    runner = _make_runner()
    source = _make_source()
    session_key = build_session_key(source)
    memory_manager = SimpleNamespace(get_provider=lambda name: None)
    runner._agent_cache[session_key] = (SimpleNamespace(_memory_manager=memory_manager), "sig")

    result = await runner._handle_retain_command(_make_event())

    assert result == "当前会话没有可用的 Hindsight 记忆 Provider，无法执行 /retain。"
