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
async def test_retain_command_uses_persisted_hindsight_lineage_when_available():
    runner = _make_runner()
    source = _make_source()
    session_key = build_session_key(source)
    provider = MagicMock()
    provider.retain_persisted_session_lineage.return_value = {"queued": True}
    memory_manager = SimpleNamespace(get_provider=lambda name: provider if name == "hindsight" else None)
    agent = SimpleNamespace(_memory_manager=memory_manager, session_id="sid-1")
    runner._agent_cache[session_key] = (agent, "sig")
    db = SimpleNamespace(get_session=lambda sid: {"parent_session_id": "parent-1"})
    runner.__dict__["session_store"] = SimpleNamespace(_db=db)

    result = await runner._handle_retain_command(_make_event())

    assert result == "Buffered session turns queued for retain."
    provider.retain_persisted_session_lineage.assert_called_once_with(
        session_id="sid-1",
        parent_session_id="parent-1",
    )
    provider.flush_retained_turns.assert_not_called()


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
async def test_retain_command_reports_no_loaded_agent_in_chinese():
    runner = _make_runner()

    result = await runner._handle_retain_command(_make_event())

    assert result == "当前会话还没有加载运行中的 Agent。/resume 后请先发送一条普通消息，再执行 /retain。"


@pytest.mark.asyncio
async def test_retain_command_reports_no_hindsight_provider_in_chinese():
    runner = _make_runner()
    source = _make_source()
    session_key = build_session_key(source)
    memory_manager = SimpleNamespace(get_provider=lambda name: None)
    runner._agent_cache[session_key] = (SimpleNamespace(_memory_manager=memory_manager), "sig")

    result = await runner._handle_retain_command(_make_event())

    assert result == "当前会话没有可用的 Hindsight 记忆 Provider，无法执行 /retain。"
