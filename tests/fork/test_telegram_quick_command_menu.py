"""Quick-command discovery must agree with dispatch, without executing commands."""
import pytest
import yaml

from hermes_cli import commands_platforms as menus


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,body", [("exec", {"command": "exit 99"}),
                                      ("alias", {"target": "/status"})])
async def test_configured_quick_command_survives_menu_cap(tmp_path, monkeypatch, kind, body):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "quick_commands": {"retain": {"type": kind, "description": "Save snapshot", **body}},
    }))
    monkeypatch.setattr(menus, "telegram_bot_commands", lambda **kw: [
        ("help", "Help"), ("ordinary", "Ordinary")])
    monkeypatch.setattr(menus, "_collect_gateway_skill_entries", lambda **kw: ([], 0))
    menu, hidden = menus.telegram_menu_commands(max_commands=2)
    assert menu == [("help", "Help"), ("retain", "Save snapshot")]
    assert hidden == 1

    # Exercise the actual adapter seam; only the remote Telegram transport is fake.
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from gateway.config import Platform, PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="TEST_ONLY", extra={})
    bot = SimpleNamespace(set_my_commands=AsyncMock())
    adapter._bot = bot
    adapter._forum_command_registered = set()
    adapter._forum_lock = asyncio.Lock()
    monkeypatch.setattr(menus, "telegram_menu_max_commands", lambda: 2)
    await adapter._register_command_menu()
    await adapter._ensure_forum_commands(
        SimpleNamespace(chat=SimpleNamespace(id=-123, is_forum=True)))
    calls = bot.set_my_commands.await_args_list
    assert {call.kwargs["scope"].type for call in calls} == {
        "default", "all_private_chats", "all_group_chats", "chat"}
    for call in calls:
        assert [(cmd.command, cmd.description) for cmd in call.args[0]] == menu


def test_quick_menu_preserves_dispatch_names_and_core_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    quick = {
        "restart": {"type": "alias", "target": "/gateway restart", "description": "Wrong"},
        "reset": {"type": "exec", "command": "exit 99"},
        "valid_name": {"type": "exec", "command": "exit 99"},
        "long_desc": {"type": "alias", "target": "/status", "description": "x" * 120},
        "bad-name": {"type": "exec", "command": "exit 99"},
        "UPPER": {"type": "exec", "command": "exit 99"},
        "x" * 33: {"type": "exec", "command": "exit 99"},
        "empty": {"type": "exec", "command": ""},
        "badtype": {"type": "anything", "command": "exit 99"},
        "malformed": None,
        "list_type": {"type": ["exec"], "command": "exit 99"},
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"quick_commands": quick}))
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_commands", lambda: {
        "valid_name": {"description": "Plugin must not replace the quick command"}})
    monkeypatch.setattr("agent.skill_commands.get_skill_commands", lambda: {})
    menu, _ = menus.telegram_menu_commands(max_commands=100)
    names = [name for name, _ in menu]
    assert names.count("restart") == 1
    assert dict(menu)["restart"] != "Wrong"
    assert "reset" not in names
    assert dict(menu)["valid_name"] == "Custom quick command"
    assert len(dict(menu)["long_desc"]) == 40
    assert not ({"bad_name", "upper", "x" * 32, "empty", "badtype", "malformed"} & set(names))
