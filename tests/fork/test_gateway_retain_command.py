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
async def test_retain_on_new_waits_for_pending_turns_and_api_acknowledgement():
    runner = _make_runner()
    source = _make_source()
    session_key = build_session_key(source)
    provider = MagicMock()
    provider.retain_on_new_enabled = True
    provider.retain_before_session_reset.return_value = {
        "queued": True,
        "turn_count": 2,
    }
    memory_manager = SimpleNamespace(
        get_provider=lambda name: provider if name == "hindsight" else None,
        flush_pending=MagicMock(return_value=True),
    )
    agent = SimpleNamespace(_memory_manager=memory_manager, session_id="sid-1")
    runner._agent_cache[session_key] = (agent, "sig")
    session_entry = SimpleNamespace(session_key=session_key, session_id="sid-1")
    db = SimpleNamespace(get_session=lambda sid: {"parent_session_id": "parent-1"})
    runner.__dict__["session_store"] = SimpleNamespace(
        _db=db,
        get_or_create_session=MagicMock(return_value=session_entry),
    )

    result = await runner._retain_hindsight_session(
        _make_event(),
        wait=True,
        only_if_retain_on_new=True,
    )

    assert result == {"queued": True, "turn_count": 2}
    provider.retain_before_session_reset.assert_called_once_with(
        session_id="sid-1",
        parent_session_id="parent-1",
        flush_pending=memory_manager.flush_pending,
    )
    provider.retain_persisted_session_lineage.assert_not_called()


@pytest.mark.asyncio
async def test_retain_on_new_disabled_does_not_drain_or_retain():
    runner = _make_runner()
    source = _make_source()
    session_key = build_session_key(source)
    provider = MagicMock()
    provider.retain_on_new_enabled = False
    memory_manager = SimpleNamespace(
        get_provider=lambda name: provider if name == "hindsight" else None,
        flush_pending=MagicMock(return_value=True),
    )
    runner._agent_cache[session_key] = (
        SimpleNamespace(_memory_manager=memory_manager, session_id="sid-1"),
        "sig",
    )
    runner.__dict__["session_store"] = SimpleNamespace(
        _db=SimpleNamespace(get_session=lambda sid: {"parent_session_id": ""}),
        get_or_create_session=MagicMock(
            return_value=SimpleNamespace(session_key=session_key, session_id="sid-1")
        ),
    )

    result = await runner._retain_hindsight_session(
        _make_event(),
        wait=True,
        only_if_retain_on_new=True,
    )

    assert result == {"enabled": False, "queued": False}
    memory_manager.flush_pending.assert_not_called()
    provider.retain_before_session_reset.assert_not_called()


@pytest.mark.asyncio
async def test_retain_on_new_cold_path_loads_provider(monkeypatch):
    import hermes_cli.config as config_module
    import plugins.memory as memory_plugins
    import plugins.memory.hindsight as hindsight_module

    runner = _make_runner()
    source = _make_source()
    session_key = build_session_key(source)
    session_entry = SimpleNamespace(session_key=session_key, session_id="sid-1")
    runner.__dict__["session_store"] = SimpleNamespace(
        _db=SimpleNamespace(
            get_session=lambda sid: {"parent_session_id": "parent-1"}
        ),
        get_or_create_session=MagicMock(return_value=session_entry),
    )
    provider = MagicMock()
    provider.retain_before_session_reset.return_value = {
        "queued": True,
        "turn_count": 2,
    }
    load_provider = MagicMock(return_value=provider)
    monkeypatch.setattr(
        hindsight_module,
        "get_retain_on_new_settings",
        lambda: (True, 7.0),
    )
    monkeypatch.setattr(
        memory_plugins,
        "load_memory_provider",
        load_provider,
    )
    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {"memory": {"provider": "hindsight"}},
    )

    result = await runner._retain_hindsight_session(
        _make_event(),
        wait=True,
        only_if_retain_on_new=True,
    )

    assert result == {"queued": True, "turn_count": 2}
    load_provider.assert_called_once_with("hindsight")
    provider.initialize.assert_called_once()
    provider.retain_before_session_reset.assert_called_once()
    provider.shutdown.assert_called_once()


@pytest.mark.asyncio
async def test_retain_command_uses_persisted_turn_store_even_when_session_transcript_available():
    runner = _make_runner()
    source = _make_source()
    session_key = build_session_key(source)
    provider = MagicMock()
    provider.retain_persisted_session_lineage.return_value = {"queued": True}
    memory_manager = SimpleNamespace(get_provider=lambda name: provider if name == "hindsight" else None)
    agent = SimpleNamespace(_memory_manager=memory_manager, session_id="sid-1")
    runner._agent_cache[session_key] = (agent, "sig")
    messages = [
        {"role": "user", "content": "[Recent Summary (d0)]\ncompressed context"},
        {"role": "user", "content": "next user"},
        {"role": "assistant", "content": "next assistant"},
    ]
    db = SimpleNamespace(get_session=lambda sid: {"parent_session_id": "parent-1"})
    load_transcript = MagicMock(return_value=messages)
    runner.__dict__["session_store"] = SimpleNamespace(_db=db, load_transcript=load_transcript)

    result = await runner._handle_retain_command(_make_event())

    assert result == "Buffered session turns queued for retain."
    provider.retain_persisted_session_lineage.assert_called_once_with(
        session_id="sid-1",
        parent_session_id="parent-1",
    )
    provider.retain_conversation_messages.assert_not_called()
    load_transcript.assert_not_called()
    provider.flush_retained_turns.assert_not_called()


@pytest.mark.asyncio
async def test_retain_command_does_not_read_sessiondb_lineage_as_content():
    runner = _make_runner()
    source = _make_source()
    session_key = build_session_key(source)
    provider = MagicMock()
    provider.retain_persisted_session_lineage.return_value = {"queued": True}
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
    provider.retain_persisted_session_lineage.assert_called_once_with(
        session_id="child-sid",
        parent_session_id="root-sid",
    )
    provider.retain_conversation_messages.assert_not_called()
    db.get_messages_as_conversation.assert_not_called()


@pytest.mark.asyncio
async def test_retain_command_does_not_fallback_to_flush_when_persisted_store_empty():
    runner = _make_runner()
    source = _make_source()
    session_key = build_session_key(source)
    provider = SimpleNamespace(
        retain_persisted_session_lineage=MagicMock(
            return_value={"queued": False, "message": "No persisted turns to retain."}
        ),
        flush_retained_turns=MagicMock(return_value={"queued": True}),
    )
    memory_manager = SimpleNamespace(get_provider=lambda name: provider if name == "hindsight" else None)
    agent = SimpleNamespace(_memory_manager=memory_manager, session_id="sid-1")
    runner._agent_cache[session_key] = (agent, "sig")
    db = SimpleNamespace(get_session=lambda sid: {"parent_session_id": "parent-1"})
    runner.__dict__["session_store"] = SimpleNamespace(_db=db)

    result = await runner._handle_retain_command(_make_event())

    assert result == "No persisted turns to retain."
    provider.retain_persisted_session_lineage.assert_called_once_with(
        session_id="sid-1",
        parent_session_id="parent-1",
    )
    provider.flush_retained_turns.assert_not_called()


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
