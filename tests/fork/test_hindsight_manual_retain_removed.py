"""Regression guards for the retired fork-owned Hindsight retain chain."""

from __future__ import annotations

from fork_features.hindsight_recall_cache import CarryResult, RecallSnapshot
from hermes_cli.commands import resolve_command
from plugins.memory.hindsight import HindsightMemoryProvider
from tests.plugins.memory.test_hindsight_provider import provider


def test_manual_retain_command_is_not_registered():
    assert resolve_command("retain") is None


def test_hindsight_retain_tool_preserves_bank_tags_metadata_and_unicode(provider):
    provider._bank_id = "Hermes"
    provider._retain_tags = ["default-tag"]
    provider._session_id = "session-1"
    provider._platform = "telegram"
    provider._user_id = "user-1"

    retain_schema = next(
        schema for schema in provider.get_tool_schemas()
        if schema["name"] == "hindsight_retain"
    )
    assert "occurred_at" in retain_schema["parameters"]["properties"]

    result = provider.handle_tool_call(
        "hindsight_retain",
        {
            "content": "中文偏好：保留原文",
            "context": "用户偏好",
            "tags": ["manual", "default-tag"],
            "occurred_at": "2026-08-20T14:30:00+08:00",
        },
    )

    assert "Memory stored successfully" in result
    call = provider._client.aretain_batch.call_args.kwargs
    assert call["bank_id"] == "Hermes"
    item = call["items"][0]
    assert item["content"] == "中文偏好：保留原文"
    assert item["context"] == "用户偏好"
    assert item["tags"] == ["default-tag", "manual"]
    assert item["timestamp"] == "2026-08-20T14:30:00+08:00"
    assert item["metadata"]["session_id"] == "session-1"
    assert item["metadata"]["platform"] == "telegram"
    assert item["metadata"]["user_id"] == "user-1"


def test_provider_does_not_expose_manual_retain_or_retain_on_new():
    provider = HindsightMemoryProvider()
    config_keys = {item["key"] for item in provider.get_config_schema()}

    assert "retain_on_new" not in config_keys
    assert "retain_on_new_timeout_seconds" not in config_keys
    assert not hasattr(provider, "retain_persisted_session_lineage")
    assert not hasattr(provider, "retain_before_session_reset")
    assert not hasattr(provider, "mark_persisted_turns_rewound")


def test_auto_retain_disabled_does_not_create_manual_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provider = HindsightMemoryProvider()
    provider._auto_retain = False

    provider.sync_turn("question", "answer", session_id="session-1")

    assert not (tmp_path / "hindsight" / "retain_turns.sqlite3").exists()


def test_rewind_only_invalidates_recall_without_manual_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provider = HindsightMemoryProvider()
    provider._session_id = "session-1"
    provider._recall_cache.switch_session("session-1")
    active = provider._recall_cache.begin_turn(
        requested_session_id="session-1",
        turn_id="turn-before-rewind",
    )
    assert active is not None
    provider._recall_cache.seed(
        RecallSnapshot(query="stale query", results=("stale recall",))
    )

    provider.on_session_rewind("session-1", turns_undone=1)

    assert provider._recall_cache.result == ""
    assert provider._recall_cache.snapshot is None
    assert provider._recall_cache.carry(
        RecallSnapshot(query="late", results=("late recall",)),
        expected_generation=active.generation,
        expected_session_id="session-1",
    ) is CarryResult.STALE_GENERATION
    assert not (tmp_path / "hindsight" / "retain_turns.sqlite3").exists()
