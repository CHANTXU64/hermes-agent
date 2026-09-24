"""Plugin-registered auxiliary tasks merge into the built-in task list and ``_reset_aux_to_auto``."""

from __future__ import annotations

import pytest

from hermes_cli.plugins import (
    PluginContext,
    PluginManager,
    PluginManifest,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def patched_manager(monkeypatch):
    """Replace the module-level singleton with a fresh manager for the test.

    Restored automatically after the test by monkeypatch.
    """
    from hermes_cli import plugins as plugins_mod

    fresh = PluginManager()
    fresh._discovered = True
    monkeypatch.setattr(plugins_mod, "_PLUGIN_MANAGER", fresh, raising=False)

    def _stub_get_manager() -> PluginManager:
        return fresh

    monkeypatch.setattr(plugins_mod, "get_plugin_manager", _stub_get_manager)
    monkeypatch.setattr(plugins_mod, "_ensure_plugins_discovered", _stub_get_manager)
    yield fresh


_USER_MEMORY_PROVIDER_SOURCE = '''
from agent.memory_provider import MemoryProvider


class ExampleUserMemoryProvider(MemoryProvider):
    @property
    def name(self):
        return "example_user_memory"

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        pass

    def get_tool_schemas(self):
        return []


def register(ctx):
    ctx.register_memory_provider(ExampleUserMemoryProvider())
    ctx.register_auxiliary_task(
        "example_user_memory_preprocessor",
        display_name="Example user memory preprocessor",
        description="side task of a memory provider installed under HERMES_HOME/plugins",
        defaults={"provider": "openai-codex", "timeout": 30},
    )
'''


def test_get_plugin_auxiliary_tasks_includes_user_installed_memory_provider(
    monkeypatch,
    patched_manager,
    tmp_path,
):
    """Memory providers now ship as user plugins (e.g. hindsight from the plugin catalog).

    Their auxiliary tasks never pass through general plugin discovery, so the active
    provider under ``$HERMES_HOME/plugins/<name>/`` must still surface them to the
    picker and ``auxiliary.<key>`` config routing.
    """
    from hermes_cli.plugins import get_plugin_auxiliary_tasks
    from plugins import memory as memory_plugins

    hermes_home = tmp_path / "home"
    provider_dir = hermes_home / "plugins" / "example_user_memory"
    provider_dir.mkdir(parents=True)
    (provider_dir / "__init__.py").write_text(_USER_MEMORY_PROVIDER_SOURCE, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(memory_plugins, "_MEMORY_AUXILIARY_TASKS", {})
    monkeypatch.setattr(
        memory_plugins,
        "_get_active_memory_provider",
        lambda: "example_user_memory",
    )

    tasks = get_plugin_auxiliary_tasks()
    task = next(
        entry
        for entry in tasks
        if entry["key"] == "example_user_memory_preprocessor"
    )

    assert task["plugin"] == "memory:example_user_memory"
    assert task["defaults"] == {
        "provider": "openai-codex",
        "model": "",
        "base_url": "",
        "api_key": "",
        "timeout": 30,
        "extra_body": {},
    }


def test_active_memory_provider_auxiliary_registration_is_cached(monkeypatch):
    from plugins import memory as memory_plugins

    monkeypatch.setattr(
        memory_plugins,
        "_MEMORY_AUXILIARY_TASKS",
        {},
    )
    monkeypatch.setattr(
        memory_plugins,
        "_get_active_memory_provider",
        lambda: "example_memory",
    )
    load_calls = []

    def _load_memory_provider(name):
        load_calls.append(name)
        memory_plugins._MEMORY_AUXILIARY_TASKS[name] = [
            {
                "key": "example_task",
                "display_name": "Example task",
                "description": "example",
                "defaults": {"provider": "auto"},
                "plugin": "memory:example_memory",
            }
        ]
        return object()

    monkeypatch.setattr(
        memory_plugins,
        "load_memory_provider",
        _load_memory_provider,
    )

    first = memory_plugins.get_memory_provider_auxiliary_tasks()
    second = memory_plugins.get_memory_provider_auxiliary_tasks()

    assert first == second
    assert load_calls == ["example_memory"]


def test_active_memory_provider_without_auxiliary_tasks_is_cached(monkeypatch):
    from plugins import memory as memory_plugins

    monkeypatch.setattr(memory_plugins, "_MEMORY_AUXILIARY_TASKS", {})
    monkeypatch.setattr(
        memory_plugins,
        "_get_active_memory_provider",
        lambda: "plain_memory",
    )
    load_calls = []

    def _load_memory_provider(name):
        load_calls.append(name)
        return object()

    monkeypatch.setattr(
        memory_plugins,
        "load_memory_provider",
        _load_memory_provider,
    )

    assert memory_plugins.get_memory_provider_auxiliary_tasks() == []
    assert memory_plugins.get_memory_provider_auxiliary_tasks() == []
    assert load_calls == ["plain_memory"]


# ── _all_aux_tasks merges built-in + plugin ──────────────────────────────────


def test_all_aux_tasks_includes_plugin_registered(patched_manager):
    from hermes_cli.main_provider_setup import _AUX_TASKS, _all_aux_tasks

    manifest = PluginManifest(name="hindsight")
    ctx = PluginContext(manifest, patched_manager)
    ctx.register_auxiliary_task(
        key="memory_retain_filter",
        display_name="Memory retain filter",
        description="hindsight pre-retain dedup/extract",
    )

    merged = _all_aux_tasks()
    keys = [k for k, _, _ in merged]
    # Built-ins preserved (and come first)
    builtin_keys = [k for k, _, _ in _AUX_TASKS]
    assert keys[: len(builtin_keys)] == builtin_keys
    # Plugin task appended
    assert "memory_retain_filter" in keys
    plugin_entry = next(t for t in merged if t[0] == "memory_retain_filter")
    assert plugin_entry == (
        "memory_retain_filter",
        "Memory retain filter",
        "hindsight pre-retain dedup/extract",
    )


# ── _reset_aux_to_auto includes plugin tasks ─────────────────────────────────


def test_reset_aux_to_auto_resets_plugin_tasks(tmp_path, monkeypatch, patched_manager):
    """Plugin task with non-auto config gets reset alongside built-ins."""
    from pathlib import Path
    from hermes_cli.config import load_config, save_config
    from hermes_cli.main_provider_setup import _reset_aux_to_auto

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".hermes").mkdir(exist_ok=True)

    manifest = PluginManifest(name="plug")
    ctx = PluginContext(manifest, patched_manager)
    ctx.register_auxiliary_task(
        key="my_aux",
        display_name="My Aux",
        description="d",
    )

    # Manually configure the plugin task to non-auto
    cfg = load_config()
    aux = cfg.setdefault("auxiliary", {})
    aux["my_aux"] = {"provider": "openrouter", "model": "gpt-4o", "base_url": "", "api_key": ""}
    save_config(cfg)

    n = _reset_aux_to_auto()
    assert n >= 1

    cfg = load_config()
    assert cfg["auxiliary"]["my_aux"]["provider"] == "auto"
    assert cfg["auxiliary"]["my_aux"]["model"] == ""
def _patch_plugin_task_config(monkeypatch, user_task_config):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"auxiliary": {"plugin_task": user_task_config}},
    )
    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_auxiliary_tasks",
        lambda: [
            {
                "key": "plugin_task",
                "defaults": {
                    "provider": "openai-codex",
                    "model": "gpt-5.6-luna",
                    "timeout": 30,
                    "extra_body": {"service_tier": "priority"},
                },
            }
        ],
    )
def test_provider_override_does_not_inherit_plugin_default_extra_body(monkeypatch):
    from agent import auxiliary_client as aux

    _patch_plugin_task_config(
        monkeypatch,
        {"provider": "xai-oauth", "model": "grok-4"},
    )

    assert aux._get_auxiliary_task_config("plugin_task") == {
        "provider": "xai-oauth",
        "model": "grok-4",
        "timeout": 30,
    }


def test_same_provider_override_keeps_plugin_default_extra_body(monkeypatch):
    from agent import auxiliary_client as aux

    _patch_plugin_task_config(
        monkeypatch,
        {"provider": "openai-codex", "model": "gpt-5.5"},
    )

    assert aux._get_auxiliary_task_config("plugin_task")["extra_body"] == {
        "service_tier": "priority"
    }


def test_provider_override_keeps_explicit_user_extra_body(monkeypatch):
    from agent import auxiliary_client as aux

    _patch_plugin_task_config(
        monkeypatch,
        {
            "provider": "xai-oauth",
            "model": "grok-4",
            "extra_body": {"custom_option": True},
        },
    )

    assert aux._get_auxiliary_task_config("plugin_task")["extra_body"] == {
        "custom_option": True
    }
