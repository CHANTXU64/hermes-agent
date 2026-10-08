"""Retiring same-profile extra bots keeps the supported profile route intact."""
from __future__ import annotations

import pytest

from agent.secret_scope import reset_secret_scope, set_secret_scope
from gateway.config import GatewayConfig, Platform
from gateway.config_env import _apply_env_overrides


@pytest.mark.parametrize("extra_name", ["WORK", "work", "bot-2"])
def test_extra_token_no_longer_creates_an_account(extra_name):
    token = set_secret_scope({
        "TELEGRAM_BOT_TOKEN": "111:primary",
        f"TELEGRAM_BOT_TOKEN_{extra_name}": "222:retired",
    })
    try:
        config = GatewayConfig()
        _apply_env_overrides(config)
    finally:
        reset_secret_scope(token)
    assert config.platforms[Platform.TELEGRAM].token == "111:primary"
    assert not config.platforms[Platform.TELEGRAM].extra.get("accounts")


def test_supported_runtime_imports_without_retired_package(tmp_path):
    import os
    import subprocess
    import sys

    probe = '''
import importlib.abc
import sys
class RetiredPackage(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("fork_features.multi_telegram_accounts"):
            raise ImportError("retired multi-bot package is not installed")
sys.meta_path.insert(0, RetiredPackage())
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from gateway.config import Platform, PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter
source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm")
assert build_session_key(source) == "agent:main:telegram:dm:123"
adapter = TelegramAdapter(PlatformConfig(enabled=True, token="111:fixture"))
assert adapter.platform == Platform.TELEGRAM
'''
    env = dict(os.environ, HERMES_HOME=str(tmp_path))
    result = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("profile", [None, "general", "main"])
def test_primary_profile_keys_round_trip(profile):
    from gateway.run import _parse_session_key
    from gateway.session import SessionSource, build_session_key
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm", thread_id="17")
    parsed = _parse_session_key(build_session_key(source, profile=profile))
    assert parsed["chat_id"] == "123"
    assert parsed["thread_id"] == "17"
    assert parsed.get("profile") == profile


def test_current_peer_does_not_adopt_retired_history(tmp_path, monkeypatch):
    from gateway.session import SessionSource, SessionStore
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    db = store._db
    old_key = "agent:main:telegram:dm:123:account:work"
    try:
        db.create_session("retired-history", "telegram", user_id="123", chat_id="123",
                          chat_type="dm", session_key=old_key)
        db.append_message("retired-history", "user", "Preserve this history")
        db.end_session("retired-history", "session_reset")
        entry = store.get_or_create_session(SessionSource(
            platform=Platform.TELEGRAM, chat_id="123", chat_type="dm", user_id="123"))
        assert entry.session_id != "retired-history"
        assert db.get_session("retired-history")["session_key"] == old_key
        assert store.load_transcript("retired-history")[0]["content"] == "Preserve this history"
    finally:
        db.close()
