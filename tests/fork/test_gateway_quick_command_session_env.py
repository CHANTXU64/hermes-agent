"""Gateway-only session identity for config-defined exec Quick Commands."""

import asyncio
import os
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key

_MISSING = object()


class _Store:
    def __init__(self, session_ids: dict[str, str]):
        self.session_ids = session_ids
        self.peeked_keys: list[str] = []

    def _generate_session_key(self, source: SessionSource) -> str:
        return build_session_key(source)

    def peek_session_id(self, session_key: str):
        self.peeked_keys.append(session_key)
        return self.session_ids.get(session_key)


class _Proc:
    returncode = 0

    def __init__(self, session_id: str):
        self.session_id = session_id

    async def communicate(self):
        await asyncio.sleep(0)
        return self.session_id.encode(), b""


def _runner(
    session_ids: dict[str, str],
    *,
    session_env: object = _MISSING,
    command: str = "print-session",
):
    qcmd: dict[str, object] = {"type": "exec", "command": command}
    if session_env is not _MISSING:
        qcmd["session_env"] = session_env

    runner = cast(Any, GatewayRunner.__new__(GatewayRunner))
    runner.config = {"quick_commands": {"retain": qcmd}}
    runner.session_store = _Store(session_ids)
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._is_user_authorized = MagicMock(return_value=True)
    return runner


def _event(source: SessionSource) -> MessageEvent:
    return MessageEvent(
        text="/retain",
        message_type=MessageType.TEXT,
        source=source,
    )


@pytest.mark.asyncio
async def test_session_env_fails_closed_without_current_route_mapping():
    source = SessionSource(
        platform=Platform.WEIXIN,
        chat_id="wx-user-a",
        chat_type="dm",
        user_id="wx-user-a",
    )
    runner = _runner({}, session_env=True, command="must-not-run")
    called = False

    async def _must_not_create(*_args, **_kwargs):
        nonlocal called
        called = True
        return _Proc("unexpected")

    with patch("asyncio.create_subprocess_shell", side_effect=_must_not_create):
        result = await runner._handle_message(_event(source))

    assert called is False
    assert result is not None
    assert "requires an active Hermes session" in result
    assert runner.session_store.peeked_keys == [build_session_key(source)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_env",
    [
        pytest.param(_MISSING, id="absent"),
        pytest.param(None, id="null"),
        pytest.param(False, id="false"),
        pytest.param("false", id="string-false"),
        pytest.param(1, id="integer"),
    ],
)
async def test_session_env_requires_strict_boolean_opt_in(
    monkeypatch, session_env
):
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="5612546357",
        chat_type="dm",
        user_id="5612546357",
        account_id="work",
    )
    key = build_session_key(source)
    runner = _runner(
        {key: "must-not-leak"},
        session_env=session_env,
    )
    monkeypatch.setenv("HERMES_SESSION_ID", "stale-process-session")

    async def _create_subprocess_shell(_command, **kwargs):
        child_env = dict(kwargs["env"])
        return _Proc(child_env.get("HERMES_SESSION_ID", "missing"))

    with patch(
        "asyncio.create_subprocess_shell",
        side_effect=_create_subprocess_shell,
    ):
        result = await runner._handle_message(_event(source))

    assert result == "missing"
    assert runner.session_store.peeked_keys == []
    assert os.environ["HERMES_SESSION_ID"] == "stale-process-session"


@pytest.mark.asyncio
async def test_session_env_isolated_across_channels_threads_and_accounts(
    monkeypatch,
):
    sources = [
        SessionSource(
            platform=Platform.WEIXIN,
            chat_id="wx-user-a",
            chat_type="dm",
            user_id="wx-user-a",
        ),
        SessionSource(
            platform=Platform.DISCORD,
            chat_id="discord-thread-a",
            chat_type="thread",
            user_id="discord-user",
            thread_id="discord-thread-a",
        ),
        SessionSource(
            platform=Platform.DISCORD,
            chat_id="discord-thread-b",
            chat_type="thread",
            user_id="discord-user",
            thread_id="discord-thread-b",
        ),
        SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="5612546357",
            chat_type="dm",
            user_id="5612546357",
        ),
        SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="5612546357",
            chat_type="dm",
            user_id="5612546357",
            account_id="work",
        ),
    ]
    session_ids = {
        build_session_key(source): f"durable-session-{index}"
        for index, source in enumerate(sources)
    }
    runner = _runner(session_ids, session_env=True)
    seen_child_ids: list[str] = []
    monkeypatch.setenv("HERMES_SESSION_ID", "stale-process-session")

    async def _create_subprocess_shell(_command, **kwargs):
        child_env = dict(kwargs["env"])
        session_id = child_env.get("HERMES_SESSION_ID", "")
        seen_child_ids.append(session_id)
        return _Proc(session_id)

    with patch(
        "asyncio.create_subprocess_shell",
        side_effect=_create_subprocess_shell,
    ):
        results = await asyncio.gather(
            *(runner._handle_message(_event(source)) for source in sources)
        )

    expected_results = [
        session_ids[build_session_key(source)] for source in sources
    ]
    assert results == expected_results
    assert set(seen_child_ids) == set(expected_results)
    assert os.environ["HERMES_SESSION_ID"] == "stale-process-session"
    assert build_session_key(sources[-2]) != build_session_key(sources[-1])
    assert build_session_key(sources[1]) != build_session_key(sources[2])
