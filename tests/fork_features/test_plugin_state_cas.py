"""Atomic compare-and-set contract for Profile-scoped plugin state."""

from hermes_cli.plugins import PluginState


def test_plugin_state_compare_and_set_rejects_stale_expected_value(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    first_runtime = PluginState("cas-test-plugin")
    second_runtime = PluginState("cas-test-plugin")
    first_runtime.set("authority", {"revision": 1})

    assert second_runtime.compare_and_set(
        "authority",
        expected={"revision": 1},
        value={"revision": 2},
    ) is True
    assert first_runtime.compare_and_set(
        "authority",
        expected={"revision": 1},
        value={"revision": 3},
    ) is False
    assert first_runtime.get("authority") == {"revision": 2}
    assert second_runtime.get("authority") == {"revision": 2}
