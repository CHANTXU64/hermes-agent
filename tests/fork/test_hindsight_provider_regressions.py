"""Fork-owned Hindsight provider regressions.

These tests protect local Hindsight behavior while upstream owns the baseline
provider tests. They are intentionally outside tests/plugins/ to reduce fork
merge conflicts.
"""

import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.memory.hindsight import (
    RETAIN_SESSION_SCHEMA,
    _append_capability_cache,
    _append_capability_lock,
)
from tests.plugins.memory.test_hindsight_provider import (
    _local_seconds,
    _make_mock_client,
    provider,
    provider_with_config,
)


def _fake_recall(query: str, result: str):
    class _Response:
        results = [result]
        documents = []
        relations = []
        graph_context = None

    return _Response()


def _clear_capability_cache():
    with _append_capability_lock:
        _append_capability_cache.clear()


def test_retain_session_schema_has_no_required_params():
    assert RETAIN_SESSION_SCHEMA["name"] == "hindsight_retain_session"
    assert RETAIN_SESSION_SCHEMA["parameters"]["properties"] == {}
    assert RETAIN_SESSION_SCHEMA["parameters"]["required"] == []

def test_get_tool_schemas_does_not_expose_retain_session(provider):
    schemas = provider.get_tool_schemas()
    assert len(schemas) == 3
    names = {s["name"] for s in schemas}
    assert names == {
        "hindsight_retain",
        "hindsight_recall",
        "hindsight_reflect",
    }

def test_retain_session_flushes_persisted_turns_cleanly(provider_with_config):
    p = provider_with_config(auto_retain=False, retain_tags=["configured-tag"])
    p.sync_turn("你好", "收到")
    p._client.aretain_batch.assert_not_called()

    result = json.loads(p.handle_tool_call("hindsight_retain_session", {}))

    assert result["result"] == "Buffered session turns queued for retain."
    p._retain_queue.join()
    call_kwargs = p._client.aretain_batch.call_args.kwargs
    assert call_kwargs["bank_id"] == "test-bank"
    assert call_kwargs["document_id"] == "test-session"
    item = call_kwargs["items"][0]
    assert "metadata" not in item
    assert "tags" not in item
    assert item["context"] == p._retain_context
    assert "你好" in item["content"]
    assert "\\u4f60" not in item["content"]

def test_retain_session_direct_flush_works_in_context_mode(provider_with_config):
    p = provider_with_config(auto_retain=False, memory_mode="context")
    assert p.get_tool_schemas() == []
    p.sync_turn("context user", "context assistant")

    info = p.flush_retained_turns()

    assert info["queued"] is True
    p._retain_queue.join()
    call_kwargs = p._client.aretain_batch.call_args.kwargs
    assert call_kwargs["document_id"].startswith("test-session-")
    assert "context user" in call_kwargs["items"][0]["content"]

def test_retain_session_second_call_does_not_repeat_same_turns(provider_with_config):
    p = provider_with_config(auto_retain=False)
    p.sync_turn("once", "stored")
    first = p.flush_retained_turns()
    assert first["queued"] is True
    p._retain_queue.join()
    p._client.aretain_batch.assert_called_once()

    p._client.aretain_batch.reset_mock()
    second = p.flush_retained_turns()

    assert second["queued"] is False
    assert second["message"] == "No new buffered turns to retain."
    p._retain_queue.join()
    p._client.aretain_batch.assert_not_called()

def test_auto_retain_then_manual_retain_does_not_repeat_same_turns(provider):
    provider.sync_turn("auto", "stored")
    provider._retain_queue.join()
    provider._client.aretain_batch.assert_called_once()

    provider._client.aretain_batch.reset_mock()
    info = provider.flush_retained_turns()

    assert info["queued"] is False
    assert info["message"] == "No new buffered turns to retain."
    provider._retain_queue.join()
    provider._client.aretain_batch.assert_not_called()

def test_append_mode_manual_retain_flushes_only_new_turns(provider_with_config, monkeypatch):
    from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
    with _append_capability_lock:
        _append_capability_cache.clear()
    monkeypatch.setattr(
        "plugins.memory.hindsight._fetch_hindsight_api_version",
        lambda *a, **kw: "0.5.6",
    )
    p = provider_with_config(auto_retain=False)
    p.sync_turn("first-user", "first-assistant")
    p.sync_turn("second-user", "second-assistant")
    p.flush_retained_turns()
    p._retain_queue.join()

    p._client.aretain_batch.reset_mock()
    p.sync_turn("third-user", "third-assistant")
    info = p.flush_retained_turns()
    p._retain_queue.join()

    assert info["queued"] is True
    kw = p._client.aretain_batch.call_args.kwargs
    assert kw["document_id"] == "test-session"
    item = kw["items"][0]
    assert item["update_mode"] == "append"
    assert "third-user" in item["content"]
    assert "first-user" not in item["content"]
    assert "second-user" not in item["content"]

def test_retain_flush_rejects_second_job_while_pending(provider_with_config, monkeypatch):
    from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
    with _append_capability_lock:
        _append_capability_cache.clear()
    monkeypatch.setattr(
        "plugins.memory.hindsight._fetch_hindsight_api_version",
        lambda *a, **kw: "0.5.6",
    )
    p = provider_with_config(auto_retain=False)
    monkeypatch.setattr(p, "_ensure_writer", lambda: None)
    p.sync_turn("first-user", "first-assistant")

    first = p.flush_retained_turns()
    p.sync_turn("second-user", "second-assistant")
    second = p.flush_retained_turns()

    assert first["queued"] is True
    assert second == {
        "queued": False,
        "turn_count": 0,
        "message": "A retain flush is already queued.",
    }
    assert p._retain_queue.qsize() == 1
    assert p._last_queued_flush_count == 1
    assert p._last_flushed_turn_count == 0

def test_failed_pending_retain_clears_pending_and_can_retry(provider_with_config, monkeypatch):
    p = provider_with_config(auto_retain=False)
    monkeypatch.setattr(p, "_ensure_writer", lambda: None)
    p.sync_turn("first-user", "first-assistant")
    first = p.flush_retained_turns()
    assert first["queued"] is True
    job = p._retain_queue.get_nowait()
    monkeypatch.setattr(
        p,
        "_run_hindsight_operation",
        lambda _operation: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(RuntimeError, match="boom"):
        job()
    p._retain_queue.task_done()

    assert p._retain_flush_pending is False
    assert p._last_queued_flush_count == 0
    assert p._last_flushed_turn_count == 0

    retry = p.flush_retained_turns()
    assert retry["queued"] is True
    assert retry["flush_up_to"] == 1

def test_old_pending_retain_success_does_not_pollute_new_session(provider_with_config, monkeypatch):
    from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
    with _append_capability_lock:
        _append_capability_cache.clear()
    monkeypatch.setattr(
        "plugins.memory.hindsight._fetch_hindsight_api_version",
        lambda *a, **kw: "0.5.6",
    )
    p = provider_with_config(auto_retain=False)
    monkeypatch.setattr(p, "_ensure_writer", lambda: None)
    p.sync_turn("old-user", "old-assistant")
    first = p.flush_retained_turns()
    assert first["queued"] is True
    old_job = p._retain_queue.get_nowait()

    p.on_session_switch("new-sid")
    old_job()
    p._retain_queue.task_done()

    assert p._session_id == "new-sid"
    assert p._last_flushed_turn_count == 0
    assert p._last_queued_flush_count == 0
    assert p._retain_flush_pending is False

def test_old_pending_retain_failure_does_not_roll_back_new_session(provider_with_config, monkeypatch):
    from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
    with _append_capability_lock:
        _append_capability_cache.clear()
    monkeypatch.setattr(
        "plugins.memory.hindsight._fetch_hindsight_api_version",
        lambda *a, **kw: "0.5.6",
    )
    p = provider_with_config(auto_retain=False)
    monkeypatch.setattr(p, "_ensure_writer", lambda: None)
    p.sync_turn("old-user", "old-assistant")
    assert p.flush_retained_turns()["queued"] is True
    old_job = p._retain_queue.get_nowait()

    p.on_session_switch("new-sid")
    p.sync_turn("new-user", "new-assistant")
    assert p.flush_retained_turns()["queued"] is True
    assert p._last_queued_flush_count == 1
    assert p._retain_flush_pending is True
    monkeypatch.setattr(
        p,
        "_run_hindsight_operation",
        lambda _operation: (_ for _ in ()).throw(RuntimeError("old boom")),
    )

    with pytest.raises(RuntimeError, match="old boom"):
        old_job()
    p._retain_queue.task_done()

    assert p._session_id == "new-sid"
    assert p._last_queued_flush_count == 1
    assert p._last_flushed_turn_count == 0
    assert p._retain_flush_pending is True

def test_persisted_retain_uses_hindsight_turn_payload_and_parent_lineage(provider_with_config, monkeypatch):
    from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
    with _append_capability_lock:
        _append_capability_cache.clear()
    monkeypatch.setattr(
        "plugins.memory.hindsight._fetch_hindsight_api_version",
        lambda *a, **kw: "0.5.6",
    )
    p = provider_with_config(auto_retain=False)
    p.initialize(session_id="root-session", hermes_home=str(p._retain_store_path.parents[1]), platform="cli")
    p._client = _make_mock_client()
    p.sync_turn("root user", "root assistant")
    p.on_session_switch("child-session", parent_session_id="root-session")
    p.sync_turn("child user", "child assistant")

    info = p.retain_persisted_session_lineage(session_id="child-session", parent_session_id="root-session")
    p._retain_queue.join()

    assert info["queued"] is True
    assert info["turn_count"] == 2
    assert info["lineage_session_ids"] == ["root-session", "child-session"]
    kw = p._client.aretain_batch.call_args.kwargs
    assert kw["document_id"] == "root-session"
    item = kw["items"][0]
    assert item["update_mode"] == "replace"
    assert "metadata" not in item
    assert "tags" not in item
    assert item["context"] == p._retain_context
    assert "User: root user" in item["content"]
    assert "Assistant: root assistant" in item["content"]
    assert "User: child user" in item["content"]
    assert "Assistant: child assistant" in item["content"]
    assert "tool output" not in item["content"]
    assert p._retain_store_path.name == "retain_turns.sqlite3"

def test_persisted_retain_lineage_uses_prior_non_empty_parent_when_latest_row_is_empty(provider_with_config, monkeypatch):
    from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
    with _append_capability_lock:
        _append_capability_cache.clear()
    monkeypatch.setattr(
        "plugins.memory.hindsight._fetch_hindsight_api_version",
        lambda *a, **kw: "0.5.6",
    )
    p = provider_with_config(auto_retain=False)
    p.initialize(session_id="root-session", hermes_home=str(p._retain_store_path.parents[1]), platform="cli")
    p._client = _make_mock_client()
    p.sync_turn("root user", "root assistant")
    p.on_session_switch("child-session", parent_session_id="root-session")
    p.sync_turn("child user", "child assistant")

    # Simulate a later provider lifecycle that persisted the same child
    # session with an empty parent. This matched the observed production
    # retain_turns.sqlite3 rows for 20260529_120758_e5d0e5.
    p._parent_session_id = ""
    p.sync_turn("child later user", "child later assistant")

    info = p.retain_persisted_session_lineage(session_id="child-session")
    p._retain_queue.join()

    assert info["queued"] is True
    assert info["turn_count"] == 3
    assert info["lineage_session_ids"] == ["root-session", "child-session"]
    content = p._client.aretain_batch.call_args.kwargs["items"][0]["content"]
    assert "root user" in content
    assert "child user" in content
    assert "child later user" in content

def test_manual_retain_groups_compression_sibling_sessions_by_retain_document_id(provider_with_config, monkeypatch):
    from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
    with _append_capability_lock:
        _append_capability_cache.clear()
    monkeypatch.setattr(
        "plugins.memory.hindsight._fetch_hindsight_api_version",
        lambda *a, **kw: "0.5.6",
    )
    p = provider_with_config(auto_retain=False)
    p.initialize(session_id="root-session", hermes_home=str(p._retain_store_path.parents[1]), platform="cli")
    p._client = _make_mock_client()
    p.sync_turn("root user", "root assistant")

    # C1 and C2 are observed as siblings in state.db after gateway/session
    # pointer drift, but they still belong to the same logical compression
    # document. Manual retain should group them by retain_document_id rather
    # than lose C1 when retaining from C2.
    p.on_session_switch("child-one", parent_session_id="root-session")
    p.sync_turn("child one user", "child one assistant")
    p.on_session_switch("child-two", parent_session_id="root-session")
    p.sync_turn("child two user", "child two assistant")

    info = p.retain_persisted_session_lineage(session_id="child-two")
    p._retain_queue.join()

    assert info["queued"] is True
    assert info["turn_count"] == 3
    assert info["document_id"] == "root-session"
    assert info["lineage_session_ids"] == ["root-session", "child-one", "child-two"]
    content = p._client.aretain_batch.call_args.kwargs["items"][0]["content"]
    assert "root user" in content
    assert "child one user" in content
    assert "child two user" in content

def test_rewound_persisted_turns_are_excluded_from_manual_retain(provider_with_config, monkeypatch):
    from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
    with _append_capability_lock:
        _append_capability_cache.clear()
    monkeypatch.setattr(
        "plugins.memory.hindsight._fetch_hindsight_api_version",
        lambda *a, **kw: "0.5.6",
    )
    p = provider_with_config(auto_retain=False)
    p.sync_turn("keep user", "keep assistant")
    p.sync_turn("undo one user", "undo one assistant")
    p.sync_turn("undo two user", "undo two assistant")

    assert p.mark_persisted_turns_rewound("test-session", 2) == 2
    info = p.retain_persisted_session_lineage(session_id="test-session")
    p._retain_queue.join()

    assert info["queued"] is True
    assert info["turn_count"] == 1
    content = p._client.aretain_batch.call_args.kwargs["items"][0]["content"]
    assert "keep user" in content
    assert "undo one user" not in content
    assert "undo two user" not in content

def test_rewind_excludes_only_target_session_from_grouped_document(provider_with_config, monkeypatch):
    from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
    with _append_capability_lock:
        _append_capability_cache.clear()
    monkeypatch.setattr(
        "plugins.memory.hindsight._fetch_hindsight_api_version",
        lambda *a, **kw: "0.5.6",
    )
    p = provider_with_config(auto_retain=False)
    p.initialize(session_id="root-session", hermes_home=str(p._retain_store_path.parents[1]), platform="cli")
    p._client = _make_mock_client()
    p.sync_turn("root user", "root assistant")
    p.on_session_switch("child-one", parent_session_id="root-session")
    p.sync_turn("child one user", "child one assistant")
    p.on_session_switch("child-two", parent_session_id="root-session")
    p.sync_turn("child two keep user", "child two keep assistant")
    p.sync_turn("child two undo user", "child two undo assistant")

    assert p.mark_persisted_turns_rewound("child-two", 1) == 1
    info = p.retain_persisted_session_lineage(session_id="child-two")
    p._retain_queue.join()

    assert info["turn_count"] == 3
    assert info["lineage_session_ids"] == ["root-session", "child-one", "child-two"]
    content = p._client.aretain_batch.call_args.kwargs["items"][0]["content"]
    assert "root user" in content
    assert "child one user" in content
    assert "child two keep user" in content
    assert "child two undo user" not in content

def test_rewind_hook_marks_persisted_turns_without_flushing_buffer(provider_with_config, monkeypatch):
    from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
    with _append_capability_lock:
        _append_capability_cache.clear()
    monkeypatch.setattr(
        "plugins.memory.hindsight._fetch_hindsight_api_version",
        lambda *a, **kw: "0.5.6",
    )
    p = provider_with_config(auto_retain=True, retain_every_n_turns=999)
    p.sync_turn("keep user", "keep assistant")
    p.sync_turn("undo user", "undo assistant")
    p._client.aretain_batch.reset_mock()

    p.on_session_rewind("test-session", turns_undone=1)

    assert p._client.aretain_batch.call_count == 0
    info = p.retain_persisted_session_lineage(session_id="test-session")
    p._retain_queue.join()
    assert info["turn_count"] == 1
    content = p._client.aretain_batch.call_args.kwargs["items"][0]["content"]
    assert "keep user" in content
    assert "undo user" not in content

def test_persisted_retain_ignores_stored_bank_id_and_submits_current_bank(provider_with_config, monkeypatch):
    from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
    with _append_capability_lock:
        _append_capability_cache.clear()
    monkeypatch.setattr(
        "plugins.memory.hindsight._fetch_hindsight_api_version",
        lambda *a, **kw: "0.5.6",
    )
    p = provider_with_config(auto_retain=False)
    p._bank_id = "hermes"
    p.sync_turn("old bank user", "old bank assistant")
    p._bank_id = "Hermes"
    p.sync_turn("new bank user", "new bank assistant")

    info = p.retain_persisted_session_lineage(session_id="test-session")
    p._retain_queue.join()

    assert info["turn_count"] == 2
    kw = p._client.aretain_batch.call_args.kwargs
    assert kw["bank_id"] == "Hermes"
    content = kw["items"][0]["content"]
    assert "old bank user" in content
    assert "new bank user" in content

def test_transcript_retain_preserves_leading_orphan_user_message(provider_with_config, monkeypatch):
    from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
    with _append_capability_lock:
        _append_capability_cache.clear()
    monkeypatch.setattr(
        "plugins.memory.hindsight._fetch_hindsight_api_version",
        lambda *a, **kw: "0.5.6",
    )
    p = provider_with_config(auto_retain=False)
    messages = [
        {
            "role": "user",
            "content": "FIP平台有12个合同信息完善及更正单，全部提交",
            "timestamp": 1710000000.0,
        },
        {"role": "user", "content": "你在搞什么啊？", "timestamp": "2024-03-09T16:00:01+00:00"},
        {"role": "assistant", "content": "我停了。", "timestamp": "2024-03-09T16:00:02+00:00"},
    ]

    info = p.retain_conversation_messages(messages, session_id="test-session")
    p._retain_queue.join()

    assert info["queued"] is True
    assert info["turn_count"] == 2
    content = p._client.aretain_batch.call_args.kwargs["items"][0]["content"]
    turns = json.loads(content)
    assert len(turns[0]) == 1
    assert turns[0][0]["role"] == "user"
    assert turns[0][0]["content"] == "User: FIP平台有12个合同信息完善及更正单，全部提交"
    assert turns[0][0]["timestamp"] == _local_seconds(1710000000.0)
    assert turns[1][0]["content"] == "User: 你在搞什么啊？"
    assert turns[1][0]["timestamp"] == _local_seconds("2024-03-09T16:00:01+00:00")
    assert turns[1][1]["content"] == "Assistant: 我停了。"
    assert turns[1][1]["timestamp"] == _local_seconds("2024-03-09T16:00:02+00:00")
    assert content.index("FIP平台") < content.index("你在搞什么啊")
    assert "Assistant: " not in json.dumps(turns[0], ensure_ascii=False)
    assert "\\u" not in content

def test_transcript_retain_uses_sessiondb_private_timestamp(provider_with_config, monkeypatch):
    from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
    with _append_capability_lock:
        _append_capability_cache.clear()
    monkeypatch.setattr(
        "plugins.memory.hindsight._fetch_hindsight_api_version",
        lambda *a, **kw: "0.5.6",
    )
    p = provider_with_config(auto_retain=False)
    messages = [
        {"role": "user", "content": "original user", "_timestamp": 1710000000.0},
        {"role": "assistant", "content": "original assistant", "_timestamp": 1710000001.0},
    ]

    info = p.retain_conversation_messages(messages, session_id="test-session")
    p._retain_queue.join()

    assert info["queued"] is True
    content = p._client.aretain_batch.call_args.kwargs["items"][0]["content"]
    turn = json.loads(content)[0]
    assert turn[0]["timestamp"] == _local_seconds(1710000000.0)
    assert turn[1]["timestamp"] == _local_seconds(1710000001.0)

def test_transcript_retain_pairs_user_with_last_assistant_in_segment(provider_with_config, monkeypatch):
    from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
    with _append_capability_lock:
        _append_capability_cache.clear()
    monkeypatch.setattr(
        "plugins.memory.hindsight._fetch_hindsight_api_version",
        lambda *a, **kw: "0.5.6",
    )
    p = provider_with_config(auto_retain=False)
    messages = [
        {"role": "user", "content": "好", "timestamp": 1710000000.0},
        {"role": "assistant", "content": "Need template patch.", "timestamp": 1710000001.0},
        {"role": "assistant", "content": "Need testing strategy patch.", "timestamp": 1710000002.0},
        {"role": "assistant", "content": "已落地。正式结果", "timestamp": 1710000003.0},
    ]

    info = p.retain_conversation_messages(messages, session_id="test-session")
    p._retain_queue.join()

    assert info["queued"] is True
    content = p._client.aretain_batch.call_args.kwargs["items"][0]["content"]
    turns = json.loads(content)
    assert len(turns) == 1
    assert turns[0][0]["content"] == "User: 好"
    assert turns[0][1]["content"] == "Assistant: 已落地。正式结果"
    assert turns[0][1]["timestamp"] == _local_seconds(1710000003.0)
    assert "Need template patch" not in content
    assert "Need testing strategy patch" not in content

def test_transcript_retain_full_lineage_is_authoritative_and_replaces_document(provider_with_config, monkeypatch):
    from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
    with _append_capability_lock:
        _append_capability_cache.clear()
    monkeypatch.setattr(
        "plugins.memory.hindsight._fetch_hindsight_api_version",
        lambda *a, **kw: "0.5.6",
    )
    p = provider_with_config(auto_retain=False)
    p.initialize(session_id="root-session", hermes_home=str(p._retain_store_path.parents[1]), platform="cli")
    p._client = _make_mock_client()
    p.sync_turn("persisted stale root", "persisted stale root assistant")
    p.on_session_switch("parent-session", parent_session_id="root-session")
    p.sync_turn("persisted stale parent", "persisted stale parent assistant")
    p.on_session_switch("current-session", parent_session_id="parent-session")
    p.sync_turn("persisted stale current", "persisted stale current assistant")
    messages = [
        {"_session_id": "root-session", "role": "user", "content": "transcript root request"},
        {"_session_id": "root-session", "role": "assistant", "content": "transcript root response"},
        {"_session_id": "parent-session", "role": "assistant", "content": "[Recent Summary (d0)]\nsummary"},
        {"_session_id": "parent-session", "role": "user", "content": "transcript parent request"},
        {"_session_id": "parent-session", "role": "assistant", "content": "transcript parent response"},
        {"_session_id": "current-session", "role": "user", "content": "transcript current request"},
        {"_session_id": "current-session", "role": "assistant", "content": "Need template patch."},
        {"_session_id": "current-session", "role": "assistant", "content": "transcript current final response"},
    ]

    info = p.retain_conversation_messages(
        messages,
        session_id="current-session",
        parent_session_id="parent-session",
    )
    p._retain_queue.join()

    assert info["queued"] is True
    assert info["document_id"] == "root-session"
    assert info["update_mode"] == "replace"
    assert info["turn_count"] == 3
    kw = p._client.aretain_batch.call_args.kwargs
    assert kw["document_id"] == "root-session"
    item = kw["items"][0]
    assert item["update_mode"] == "replace"
    content = item["content"]
    turns = json.loads(content)
    assert len(turns) == 3
    assert "transcript root request" in content
    assert "transcript parent request" in content
    assert "transcript current request" in content
    assert "transcript current final response" in content
    assert "Need template patch" not in content
    assert "persisted stale root" not in content
    assert "persisted stale parent" not in content
    assert "persisted stale current" not in content

def test_transcript_retain_keeps_existing_retain_document_sibling_turns(provider_with_config, monkeypatch):
    from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
    with _append_capability_lock:
        _append_capability_cache.clear()
    monkeypatch.setattr(
        "plugins.memory.hindsight._fetch_hindsight_api_version",
        lambda *a, **kw: "0.5.6",
    )
    p = provider_with_config(auto_retain=False)
    p.initialize(session_id="root-session", hermes_home=str(p._retain_store_path.parents[1]), platform="cli")
    p._client = _make_mock_client()
    p.sync_turn("root user", "root assistant")
    p.on_session_switch("child-one", parent_session_id="root-session")
    p.sync_turn("child one user", "child one assistant")
    p.on_session_switch("child-two", parent_session_id="root-session")
    p.sync_turn("stale child two user", "stale child two assistant")
    messages = [
        {"role": "user", "content": "child two interrupted first"},
        {"role": "user", "content": "child two current user"},
        {"role": "assistant", "content": "child two current assistant"},
    ]

    info = p.retain_conversation_messages(
        messages,
        session_id="child-two",
        parent_session_id="root-session",
    )
    p._retain_queue.join()

    assert info["queued"] is True
    assert info["document_id"] == "root-session"
    content = p._client.aretain_batch.call_args.kwargs["items"][0]["content"]
    assert "root user" in content
    assert "child one user" in content
    assert "child two interrupted first" in content
    assert "child two current user" in content
    assert "stale child two user" not in content

def test_transcript_retain_prefers_parent_document_when_current_session_has_split_document(provider_with_config, monkeypatch):
    from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
    with _append_capability_lock:
        _append_capability_cache.clear()
    monkeypatch.setattr(
        "plugins.memory.hindsight._fetch_hindsight_api_version",
        lambda *a, **kw: "0.5.6",
    )
    p = provider_with_config(auto_retain=False)
    p.initialize(session_id="root-session", hermes_home=str(p._retain_store_path.parents[1]), platform="cli")
    p._client = _make_mock_client()
    p.sync_turn("root file upload", "root acknowledged")
    p.on_session_switch("parent-session", parent_session_id="root-session")
    p.sync_turn("parent instruction", "parent response")
    p.on_session_switch("current-session", parent_session_id="parent-session")
    p.sync_turn("current inherited turn", "current inherited response")

    # Simulate the observed production split: the current continuation first
    # inherited the parent/root retain document, then a later provider
    # lifecycle wrote rows under the current session's own document. Manual
    # transcript retain must not let that later current-session document id
    # hide the parent/root conversation.
    p._parent_session_id = ""
    p._retain_document_id = "current-session"
    p.sync_turn("stale current-only turn", "stale current-only response")
    messages = [
        {"role": "user", "content": "current transcript first"},
        {"role": "assistant", "content": "current transcript response"},
    ]

    info = p.retain_conversation_messages(
        messages,
        session_id="current-session",
        parent_session_id="parent-session",
    )
    p._retain_queue.join()

    assert info["queued"] is True
    assert info["document_id"] == "root-session"
    content = p._client.aretain_batch.call_args.kwargs["items"][0]["content"]
    assert "root file upload" in content
    assert "parent instruction" in content
    assert "current inherited turn" not in content
    assert "stale current-only turn" not in content
    assert "current transcript first" in content

def test_transcript_retain_prefers_root_document_when_parent_session_has_split_document(provider_with_config, monkeypatch):
    from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
    with _append_capability_lock:
        _append_capability_cache.clear()
    monkeypatch.setattr(
        "plugins.memory.hindsight._fetch_hindsight_api_version",
        lambda *a, **kw: "0.5.6",
    )
    p = provider_with_config(auto_retain=False)
    p.initialize(session_id="root-session", hermes_home=str(p._retain_store_path.parents[1]), platform="cli")
    p._client = _make_mock_client()
    p.sync_turn("root original request", "root response")
    p.on_session_switch("parent-session", parent_session_id="root-session")
    p.sync_turn("parent inherited turn", "parent inherited response")

    # A later lifecycle for the parent itself can also create a split
    # parent-session document. Current descendants must still resolve to
    # the original root document rather than the parent's latest split doc.
    p._parent_session_id = ""
    p._retain_document_id = "parent-session"
    p.sync_turn("stale parent split turn", "stale parent split response")
    p.on_session_switch("current-session", parent_session_id="parent-session")
    p.sync_turn("stale current inherited from split parent", "current inherited response")
    messages = [
        {"role": "user", "content": "current transcript after parent split"},
        {"role": "assistant", "content": "current transcript response"},
    ]

    info = p.retain_conversation_messages(
        messages,
        session_id="current-session",
        parent_session_id="parent-session",
    )
    p._retain_queue.join()

    assert info["queued"] is True
    assert info["document_id"] == "root-session"
    content = p._client.aretain_batch.call_args.kwargs["items"][0]["content"]
    assert "root original request" in content
    assert "parent inherited turn" in content
    assert "stale parent split turn" not in content
    assert "stale current inherited from split parent" not in content
    assert "current transcript after parent split" in content

def test_transcript_retain_dedupes_parent_child_boundary_overlap_only(provider_with_config, monkeypatch):
    from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
    with _append_capability_lock:
        _append_capability_cache.clear()
    monkeypatch.setattr(
        "plugins.memory.hindsight._fetch_hindsight_api_version",
        lambda *a, **kw: "0.5.6",
    )
    p = provider_with_config(auto_retain=False)
    messages = [
        {"_session_id": "root", "role": "user", "content": "first real request"},
        {"_session_id": "root", "role": "assistant", "content": "first response"},
        {"_session_id": "root", "role": "user", "content": "boundary instruction"},
        {"_session_id": "root", "role": "assistant", "content": "boundary response"},
        {"_session_id": "child", "role": "assistant", "content": "[Recent Summary (d0)]\nsummary"},
        {"_session_id": "child", "role": "tool", "content": "ignored tool output"},
        {"_session_id": "child", "role": "user", "content": "boundary instruction"},
        {"_session_id": "child", "role": "assistant", "content": "boundary response"},
        {"_session_id": "child", "role": "user", "content": "new child request"},
        {"_session_id": "child", "role": "assistant", "content": "new child response"},
        {"_session_id": "child", "role": "user", "content": "boundary instruction"},
        {"_session_id": "child", "role": "assistant", "content": "legitimate repeated later"},
    ]

    info = p.retain_conversation_messages(messages, session_id="child", parent_session_id="root")
    p._retain_queue.join()

    assert info["queued"] is True
    content = p._client.aretain_batch.call_args.kwargs["items"][0]["content"]
    assert content.count("boundary instruction") == 2
    assert content.count("boundary response") == 1
    assert "new child request" in content
    assert "legitimate repeated later" in content

def test_persisted_retain_without_turns_returns_message(provider_with_config):
    p = provider_with_config(auto_retain=False)

    info = p.retain_persisted_session_lineage(session_id="db-session")

    assert info == {"queued": False, "turn_count": 0, "message": "No persisted turns to retain."}
    p._client.aretain_batch.assert_not_called()

def test_retain_session_without_buffer_returns_message(provider):
    result = json.loads(provider.handle_tool_call("hindsight_retain_session", {}))
    assert result["result"] == "No persisted turns to retain."

def test_prefetch_returns_empty_when_no_result_and_sync_disabled(provider_with_config):
    p = provider_with_config(recall_sync_on_cache_miss=False)
    assert p.prefetch("test") == ""
    p._client.arecall.assert_not_called()

def test_prefetch_cache_miss_sync_recalls_current_query(provider):
    result = provider.prefetch("current user question")
    assert "Hindsight Memory" in result
    assert "Memory 1" in result
    provider._client.arecall.assert_called_once()
    call_kwargs = provider._client.arecall.call_args.kwargs
    assert call_kwargs["query"] == "current user question"

def test_prefetch_sync_fallback_does_not_cache_for_next_turn(provider):
    result = provider.prefetch("first turn")
    assert "Memory 1" in result
    provider._client.arecall.reset_mock()
    provider._client.arecall.return_value = SimpleNamespace(results=[])
    assert provider.prefetch("second turn") == ""
    provider._client.arecall.assert_called_once()

def test_prefetch_sync_skipped_in_tools_mode(provider_with_config):
    p = provider_with_config(memory_mode="tools")
    assert p.prefetch("test") == ""
    p._client.arecall.assert_not_called()

def test_prefetch_sync_skipped_when_auto_recall_off(provider_with_config):
    p = provider_with_config(auto_recall=False)
    assert p.prefetch("test") == ""
    p._client.arecall.assert_not_called()

def test_prefetch_sync_reflect_mode(provider_with_config):
    p = provider_with_config(recall_prefetch_method="reflect")
    result = p.prefetch("summarize user")
    assert "Synthesized answer" in result
    p._client.areflect.assert_called_once()
    assert p._client.areflect.call_args.kwargs["query"] == "summarize user"

def test_prefetch_sync_errors_are_best_effort(provider):
    provider._client.arecall = AsyncMock(side_effect=RuntimeError("boom"))
    assert provider.prefetch("test") == ""

def test_late_prefetch_generation_cannot_overwrite_newer_result(provider):
    import threading

    old_started = threading.Event()
    release_old = threading.Event()

    def _fake_recall(query, *, timeout=None):
        if query == "old query":
            old_started.set()
            release_old.wait(timeout=5.0)
            return "- old recall"
        return "- new recall"

    provider._recall_for_query = _fake_recall
    provider.queue_prefetch("old query")
    assert old_started.wait(timeout=5.0)
    old_thread = provider._prefetch_thread

    provider.queue_prefetch("new query")
    new_thread = provider._prefetch_thread
    new_thread.join(timeout=5.0)
    release_old.set()
    old_thread.join(timeout=5.0)

    assert provider._prefetch_result == "- new recall"

def test_queue_prefetch_clears_cached_result_when_new_generation_returns_empty(provider):
    provider._prefetch_result = "- old cached recall"
    provider._recall_for_query = lambda query, *, timeout=None: ""

    provider.queue_prefetch("new query")
    provider._prefetch_thread.join(timeout=5.0)

    assert provider._prefetch_result == ""

def test_sync_turn_buffers_when_auto_retain_off(provider_with_config):
    p = provider_with_config(auto_retain=False)
    p.sync_turn("hello", "hi")
    assert p._sync_thread is None
    assert len(p._session_turns) == 1
    p._client.aretain_batch.assert_not_called()

def test_auto_retain_false_does_not_flush_on_session_switch(provider_with_config):
    p = provider_with_config(auto_retain=False)
    p.sync_turn("manual-only", "do not auto retain")
    assert len(p._session_turns) == 1

    p.on_session_switch("new-sid", parent_session_id="test-session", reset=True)
    p._retain_queue.join()

    p._client.aretain_batch.assert_not_called()
    assert p._session_id == "new-sid"
    assert p._session_turns == []
    assert p._last_flushed_turn_count == 0
    assert p._last_queued_flush_count == 0

def test_prefetch_result_cleared_on_switch(provider_with_config):
    """Stale recall text from the old session must not leak into the
    next session's first prefetch read."""
    provider = provider_with_config(recall_sync_on_cache_miss=False)
    provider._prefetch_result = "old-session recall: User likes Rust"
    provider.on_session_switch("new-sid")
    assert provider._prefetch_result == ""
    # And subsequent prefetch() should now report empty, not the leftover.
    assert provider.prefetch("anything") == ""

def test_first_prefetch_after_switch_sync_recalls_new_query(provider):
    provider._prefetch_result = "old-session recall"
    provider.on_session_switch("new-sid")
    result = provider.prefetch("new-session question")
    assert "Memory 1" in result
    assert "old-session recall" not in result
    assert provider._client.arecall.call_args.kwargs["query"] == "new-session question"

def test_modern_api_auto_retain_appends_only_new_turn_after_first_flush(provider, monkeypatch):
    _clear_capability_cache()
    monkeypatch.setattr(
        "plugins.memory.hindsight._fetch_hindsight_api_version",
        lambda *a, **kw: "0.5.6",
    )
    provider.sync_turn("first-user", "first-assistant")
    provider._retain_queue.join()
    provider._client.aretain_batch.reset_mock()

    provider.sync_turn("second-user", "second-assistant")
    provider._retain_queue.join()

    kw = provider._client.aretain_batch.call_args.kwargs
    assert kw["document_id"] == "test-session"
    item = kw["items"][0]
    assert item["update_mode"] == "append"
    assert "second-user" in item["content"]
    assert "first-user" not in item["content"]
