"""Official Retain command plugin: real loader/config/scheduler; workers intercepted."""
from functools import partial
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from hermes_cli.plugin_command_dispatch import invoke_plugin_command
from hermes_cli.plugins import PluginManager, parse_manifest_file
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from fork_features.hindsight_retain import retain_integrity

ID = "hindsight-retain-command"
PACKAGE = Path(__file__).resolve().parents[2] / "plugins" / ID


@pytest.fixture
def loaded(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    hindsight = home / "hindsight"
    hindsight.mkdir()
    (hindsight / "config.json").write_text(json.dumps({"bank_id": "test-bank"}))
    settings = {
        "python_executable": sys.executable,
        "output_root": str(home / "retain-runs"),
        "journal_path": str(hindsight / "retain-attempts.jsonl"),
        "state_db_path": str(home / "state.db"),
        "hindsight_config_path": str(hindsight / "config.json"),
    }
    config = {"plugins": {"enabled": [ID], "entries": {ID: {"enabled": True, "settings": settings}}}}
    (home / "config.yaml").write_text(json.dumps(config))
    token = set_hermes_home_override(str(home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SESSION_ID", "foreign-global-session")
    manager = PluginManager(scope_key=str(home))
    manifest = parse_manifest_file(PACKAGE / "plugin.yaml", PACKAGE, "bundled", "")
    assert manifest is not None
    manager._load_plugin(manifest)
    plugin = manager._plugins[ID]
    assert not plugin.error, plugin.error
    launches = []
    if plugin.module is not None:
        def spawn(argv, **kwargs):
            launches.append((argv, kwargs))
            return SimpleNamespace(pid=0)  # explicit interception: no real worker
        monkeypatch.setattr(plugin.module, "schedule_retain", partial(retain_integrity.schedule_retain, process_spawner=spawn), raising=False)
    try:
        yield SimpleNamespace(home=home, manager=manager, module=plugin.module, launches=launches, config=config, settings=settings)
    finally:
        manager.unload()
        reset_hermes_home_override(token)


def call(loaded, *, session_id="test-current-session", platform="telegram", raw_args="", source=None):
    source = source or SessionSource(platform=Platform(platform), chat_id="test-chat", user_id="test-user", chat_type="dm")
    entry = loaded.manager._plugin_commands.get("retain")
    assert entry is not None, "official plugin loader did not register /retain"
    return invoke_plugin_command(entry["handler"], raw_args, source=source, session_id=session_id, session_key="test-route", platform=platform)


@pytest.mark.parametrize("key", ["python_executable", "output_root", "journal_path", "state_db_path", "hindsight_config_path"])
@pytest.mark.parametrize("value", [None, "", "relative-file", 42])
def test_invalid_launch_settings_fail_without_fallback_or_worker(loaded, monkeypatch, key, value):
    monkeypatch.chdir(loaded.home)
    loaded.settings[key] = value
    (loaded.home / "config.yaml").write_text(json.dumps(loaded.config))
    result = call(loaded)
    assert "Retain 排期失败" in result
    assert loaded.launches == []
    assert not (loaded.home / "hindsight/retain-attempts.jsonl").exists()


@pytest.mark.parametrize("platform,source_platform", [("local", Platform.LOCAL), ("telegram", Platform.DISCORD), ("", Platform.TELEGRAM)])
def test_command_keeps_messaging_only_source_boundary(loaded, platform, source_platform):
    source = SessionSource(platform=source_platform, chat_id="test-chat", user_id="test-user", chat_type="dm")
    result = call(loaded, platform=platform, source=source)
    assert "仅支持消息平台" in result
    assert loaded.launches == []


def test_registered_handler_refuses_a_different_profile_scope(loaded, tmp_path):
    other = tmp_path / "other-profile"
    other.mkdir()
    (other / "config.yaml").write_text(json.dumps(loaded.config))
    token = set_hermes_home_override(str(other))
    try:
        result = call(loaded)
    finally:
        reset_hermes_home_override(token)
    assert "配置环境不匹配" in result
    assert loaded.launches == []


@pytest.mark.parametrize("user_id,allowed_commands,allowed", [("test-user", [], False), ("test-admin", [], True), ("test-user", ["retain"], True)])
def test_existing_slash_command_access_policy_is_preserved(loaded, user_id, allowed_commands, allowed):
    loaded.config["platforms"] = {"telegram": {"allow_admin_from": ["test-admin"], "user_allowed_commands": allowed_commands}}
    (loaded.home / "config.yaml").write_text(json.dumps(loaded.config))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="test-chat", user_id=user_id, chat_type="dm")
    result = call(loaded, source=source)
    assert bool(loaded.launches) is allowed
    if not allowed:
        assert "没有此命令的权限" in result


@pytest.mark.parametrize("name", ["_hm_run_exec_quick_command", "_hm_dispatch_quick_and_plugin_commands"])
def test_inbound_no_longer_shadows_the_official_command_entry(name):
    import re
    inbound = (PACKAGE.parents[1] / "gateway/run_inbound.py").read_text()
    assert re.search(r"^    async def " + re.escape(name) + r"\(", inbound, re.M) is None


def test_missing_session_never_uses_process_global_identity(loaded):
    result = call(loaded, session_id="")
    assert "没有可保存的会话" in result
    assert loaded.launches == []
    assert not Path(loaded.settings["journal_path"]).exists()


def test_registered_plugin_preserves_current_schedule_and_readable_receipt(loaded):
    result = call(loaded)
    assert result == "Retain 已排期，将在 20 分钟后保存本次会话。"
    events = [json.loads(line) for line in Path(loaded.settings["journal_path"]).read_text().splitlines()]
    assert len(events) == 1
    assert events[0]["session_id"] == "test-current-session"
    assert events[0]["document_id"] == "test-current-session"
    assert events[0]["delay_seconds"] == 1200
    assert len(loaded.launches) == 1
    argv, kwargs = loaded.launches[0]
    assert argv[0] == loaded.settings["python_executable"]
    for flag, key in [("--output-root", "output_root"), ("--journal", "journal_path"), ("--state-db", "state_db_path"), ("--hindsight-config", "hindsight_config_path")]:
        assert argv[argv.index(flag) + 1] == loaded.settings[key]
    assert kwargs["start_new_session"] is True


def test_typed_arguments_cannot_choose_another_session(loaded):
    call(loaded, raw_args="--session-id other-session")
    event = json.loads(Path(loaded.settings["journal_path"]).read_text().splitlines()[0])
    assert event["session_id"] == "test-current-session"


def test_invalid_bank_fails_before_any_worker(loaded):
    Path(loaded.settings["hindsight_config_path"]).write_text('{"bank_id":""}')
    assert call(loaded) == "Retain 排期失败（ValueError）。"
    assert loaded.launches == []
    assert not Path(loaded.settings["journal_path"]).exists()


def test_worker_start_error_is_not_success_and_never_echoes_error_content(loaded, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("private-config-probe-must-not-be-echoed")
    monkeypatch.setattr(loaded.module, "schedule_retain", partial(retain_integrity.schedule_retain, process_spawner=fail))
    assert call(loaded) == "Retain 排期失败（OSError）。"
    # The original scheduler's durable intent remains for its existing monitor.
    event = json.loads(Path(loaded.settings["journal_path"]).read_text().splitlines()[0])
    assert event["event"] == "scheduled"


def test_official_discovery_and_enablement_gate(loaded, monkeypatch):
    import hermes_cli.plugins as plugins
    monkeypatch.setattr(plugins, "get_bundled_plugins_dir", lambda: PACKAGE.parent)
    loaded.manager.discover_and_load(force=True)
    assert loaded.manager._plugins[ID].enabled
    assert "retain" in loaded.manager._plugin_commands
    loaded.config["plugins"]["enabled"] = []
    loaded.config["plugins"]["entries"][ID]["enabled"] = False
    (loaded.home / "config.yaml").write_text(json.dumps(loaded.config))
    loaded.manager.discover_and_load(force=True)
    assert "retain" not in loaded.manager._plugin_commands


def test_same_chat_different_profile_instances_keep_separate_settings_and_journals(loaded, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    other = tmp_path / "other-profile"
    (other / "hindsight").mkdir(parents=True)
    settings = {key: value if key == "python_executable" else value.replace(str(loaded.home), str(other))
                for key, value in loaded.settings.items()}
    config = {"plugins": {"enabled": [ID], "entries": {ID: {"enabled": True, "settings": settings}}}}
    (other / "config.yaml").write_text(json.dumps(config))
    (other / "hindsight/config.json").write_text('{"bank_id":"other-test-bank"}')
    manager = PluginManager(scope_key=str(other))
    manifest = parse_manifest_file(PACKAGE / "plugin.yaml", PACKAGE, "bundled", "")
    assert manifest is not None
    manager._load_plugin(manifest)
    module = manager._plugins[ID].module
    assert module is not loaded.module
    launches = []
    def spawn(argv, **kwargs):
        launches.append((argv, kwargs))
        return SimpleNamespace(pid=0)
    monkeypatch.setattr(module, "schedule_retain", partial(retain_integrity.schedule_retain, process_spawner=spawn))
    second = SimpleNamespace(home=other, manager=manager)
    def invoke(target, sid):
        token = set_hermes_home_override(str(target.home))
        try:
            return call(target, session_id=sid)
        finally:
            reset_hermes_home_override(token)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(invoke, loaded, "first-profile-session")
            second_result = executor.submit(invoke, second, "second-profile-session")
            assert "已排期" in first.result()
            assert "已排期" in second_result.result()
        event_a = json.loads(Path(loaded.settings["journal_path"]).read_text().splitlines()[0])
        event_b = json.loads(Path(settings["journal_path"]).read_text().splitlines()[0])
        assert event_a["session_id"] == "first-profile-session"
        assert event_b["session_id"] == "second-profile-session"
        assert len(loaded.launches) == len(launches) == 1
        for launch, expected in [(loaded.launches[0], loaded.settings), (launches[0], settings)]:
            argv = launch[0]
            assert argv[argv.index("--hindsight-config") + 1] == expected["hindsight_config_path"]
    finally:
        manager.unload()
