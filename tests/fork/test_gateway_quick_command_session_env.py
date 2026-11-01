"""Retain identity regressions migrated from the retired exec session_env bridge.

The old strict-boolean exec switch itself is intentionally retired. Identity now
arrives through the official command-handler keyword, never ambient environment.
Real routing/profile entrypoint regressions remain in the sibling routing file.
"""
import json
import os
from pathlib import Path

import pytest
from gateway.config import Platform
from gateway.session import SessionSource
from tests.fork.test_retain_command_plugin import call, loaded  # noqa: F401


def test_plugin_fails_closed_without_current_session(loaded):
    assert "没有可保存的会话" in call(loaded, session_id="")
    assert loaded.launches == []
    assert not Path(loaded.settings["journal_path"]).exists()


def test_plugin_uses_official_identity_without_modifying_global_environment(loaded, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", "stale-process-session")
    assert "已排期" in call(loaded, session_id="explicit-current-session")
    event = json.loads(Path(loaded.settings["journal_path"]).read_text().splitlines()[0])
    assert event["session_id"] == "explicit-current-session"
    assert os.environ["HERMES_SESSION_ID"] == "stale-process-session"


@pytest.mark.parametrize("platform,chat_type,thread_id", [
    (Platform.WEIXIN, "dm", None),
    (Platform.DISCORD, "thread", "discord-thread-a"),
    (Platform.DISCORD, "thread", "discord-thread-b"),
    (Platform.TELEGRAM, "dm", None),
    (Platform.TELEGRAM, "dm", "17"),
])
def test_plugin_supports_the_previous_messaging_source_shapes(loaded, platform, chat_type, thread_id):
    source = SessionSource(platform=platform, chat_id="test-chat", chat_type=chat_type,
                           user_id="test-user", thread_id=thread_id)
    assert "已排期" in call(loaded, source=source, platform=platform.value)
    event = json.loads(Path(loaded.settings["journal_path"]).read_text().splitlines()[0])
    assert event["session_id"] == "test-current-session"
