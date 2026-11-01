"""Retain's official plugin entry must follow real Gateway routing and profile scopes.

These are full GatewayRunner-host tests. They must not be skipped or replaced by
isolated probes while independent merge conflicts block importing the host.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner, _profile_runtime_scope
from gateway.session import AsyncSessionStore, SessionStore, SessionSource
from hermes_cli.plugins import get_plugin_manager, parse_manifest_file, _reset_plugin_managers_for_tests
from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
from fork_features.hindsight_retain import retain_integrity

PLUGIN = Path(__file__).resolve().parents[2] / "plugins/hindsight-retain-command"


@pytest.fixture
def routing(tmp_path, monkeypatch):
    import hermes_state
    root = tmp_path / "hermes"
    homes = {"default": root, "work": root / "profiles/work", "review": root / "profiles/review"}
    for home in homes.values():
        (home / "hindsight").mkdir(parents=True)
        settings = {"python_executable": sys.executable, "output_root": str(home / "retain-runs"),
                    "journal_path": str(home / "hindsight/retain-attempts.jsonl"),
                    "state_db_path": str(home / "state.db"),
                    "hindsight_config_path": str(home / "hindsight/config.json")}
        (home / "config.yaml").write_text(json.dumps({"plugins": {"enabled": [PLUGIN.name], "entries": {PLUGIN.name: {"enabled": True, "settings": settings}}}}))
        (home / "hindsight/config.json").write_text(json.dumps({"bank_id": "test-" + home.name}))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_SESSION_ID", "foreign-process-session")
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    token = set_hermes_home_override(root)
    _reset_plugin_managers_for_tests()
    config = GatewayConfig(multiplex_profiles=True)
    store = SessionStore(root / "sessions", config)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config, runner.session_store = config, store
    runner.adapters = {}
    runner._draining = False
    runner._hm_quick_commands = lambda: {}
    pool = ThreadPoolExecutor(max_workers=6)
    runner._get_executor = lambda: pool
    launches = []
    def spawn(argv, **kwargs):
        launches.append((str(get_hermes_home()), argv))
        return SimpleNamespace(pid=0)  # no real Retain worker
    manifest = parse_manifest_file(PLUGIN / "plugin.yaml", PLUGIN, "bundled", "")
    assert manifest is not None
    for home in homes.values():
        with _profile_runtime_scope(home, {}):
            manager = get_plugin_manager()
            manager._load_plugin(manifest)
            manager._discovered = True
            loaded = manager._plugins[PLUGIN.name]
            assert loaded.enabled and not loaded.error
            monkeypatch.setattr(loaded.module, "schedule_retain", partial(retain_integrity.schedule_retain, process_spawner=spawn))
    try:
        yield runner, store, homes, launches
    finally:
        pool.shutdown(wait=True)
        _reset_plugin_managers_for_tests()
        reset_hermes_home_override(token)
        for db in store._db_handles.values():
            if db is not None:
                db.close()


def source(profile="default", **kwargs):
    fields = dict(platform=Platform.TELEGRAM, chat_id="same-human", user_id="same-human", chat_type="dm", profile=profile)
    fields.update(kwargs)
    return SessionSource(**fields)


def rows(home):
    path = home / "hindsight/retain-attempts.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def dispatch(runner, src, home):
    with _profile_runtime_scope(home, {}):
        event = MessageEvent(text="/retain", message_type=MessageType.TEXT, source=src)
        handled, result, command = await runner._hm_dispatch_quick_and_plugin_commands(event, src, "retain")
        assert handled and command == "retain"
        return result


@pytest.mark.asyncio
async def test_shared_chat_id_profiles_use_their_own_durable_session(routing):
    runner, store, homes, launches = routing
    expected = {}
    for name, home in homes.items():
        with _profile_runtime_scope(home, {}):
            expected[name] = store.get_or_create_session(source(name)).session_id
    assert len(set(expected.values())) == len(homes)
    replies = await asyncio.gather(*(dispatch(runner, source(name), home) for name, home in homes.items()))
    assert all("已排期" in reply for reply in replies)
    for name, home in homes.items():
        assert [row["session_id"] for row in rows(home)] == [expected[name]]
    assert len(launches) == len(homes)
    for home, argv in launches:
        assert argv[argv.index("--hindsight-config") + 1] == str(Path(home) / "hindsight/config.json")


@pytest.mark.asyncio
async def test_missing_secondary_mapping_never_falls_back_to_default(routing):
    runner, store, homes, launches = routing
    with _profile_runtime_scope(homes["default"], {}):
        store.get_or_create_session(source("default"))
    before = set(store._entries)
    result = await dispatch(runner, source("work"), homes["work"])
    assert "没有可保存的会话" in result
    assert set(store._entries) == before
    assert launches == []
    assert rows(homes["work"]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("group_per_user,thread_per_user,thread_id", [(False, False, None), (True, True, "topic")])
async def test_retain_respects_store_group_and_thread_policy(routing, group_per_user, thread_per_user, thread_id):
    runner, store, homes, _ = routing
    store.config.group_sessions_per_user = group_per_user
    store.config.thread_sessions_per_user = thread_per_user
    src = source("work", chat_type="group", chat_id="shared-group", thread_id=thread_id)
    with _profile_runtime_scope(homes["work"], {}):
        expected = store.get_or_create_session(src).session_id
    assert "已排期" in await dispatch(runner, src, homes["work"])
    assert rows(homes["work"])[0]["session_id"] == expected


@pytest.mark.asyncio
async def test_official_async_lookup_keeps_profile_namespace(routing, monkeypatch):
    runner, store, homes, _ = routing
    src = source("work")
    with _profile_runtime_scope(homes["work"], {}):
        expected = store.get_or_create_session(src).session_id
    boundary = AsyncSessionStore(store)
    lookup = boundary.lookup_by_session_key
    seen = []
    async def checked(key):
        seen.append(key)
        return await lookup(key)
    monkeypatch.setattr(boundary, "lookup_by_session_key", checked)
    monkeypatch.setattr(GatewayRunner, "async_session_store", property(lambda _: boundary))
    assert "已排期" in await dispatch(runner, src, homes["work"])
    assert seen == [store._generate_session_key(src)]
    assert ":work:" in seen[0]
    assert rows(homes["work"])[0]["session_id"] == expected


@pytest.mark.asyncio
async def test_concurrent_channels_and_topics_keep_distinct_current_sessions(routing):
    runner, store, homes, launches = routing
    sources = [source(platform=Platform.WEIXIN, chat_id="wx-user"),
               source(platform=Platform.DISCORD, chat_id="discord", chat_type="thread", thread_id="a"),
               source(platform=Platform.DISCORD, chat_id="discord", chat_type="thread", thread_id="b"),
               source(), source(thread_id="17")]
    with _profile_runtime_scope(homes["default"], {}):
        expected = [store.get_or_create_session(src).session_id for src in sources]
    assert len(set(expected)) == len(sources)
    replies = await asyncio.gather(*(dispatch(runner, src, homes["default"]) for src in sources))
    assert all("已排期" in reply for reply in replies)
    assert sorted(row["session_id"] for row in rows(homes["default"])) == sorted(expected)
    assert len(launches) == len(sources)
