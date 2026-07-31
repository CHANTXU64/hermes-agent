"""Fork-owned Hindsight provider regressions.

These tests protect local Hindsight behavior while upstream owns the baseline
provider tests. They are intentionally outside tests/plugins/ to reduce fork
merge conflicts.
"""

import json
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import plugins.memory.hindsight as hindsight_module

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


def test_retain_on_new_config_is_opt_in(provider_with_config):
    default_provider = provider_with_config(auto_retain=False)
    enabled_provider = provider_with_config(
        auto_retain=False,
        retain_on_new=True,
        retain_on_new_timeout_seconds=7,
    )

    assert default_provider.retain_on_new_enabled is False
    assert enabled_provider.retain_on_new_enabled is True
    assert enabled_provider.retain_on_new_timeout_seconds == 7.0


def test_retain_on_new_settings_are_exposed_in_config_schema(provider):
    schema = {item["key"]: item for item in provider.get_config_schema()}

    assert schema["retain_on_new"]["default"] is False
    assert schema["retain_on_new_timeout_seconds"]["default"] == 30


def test_waited_persisted_retain_propagates_api_failure(provider_with_config):
    p = provider_with_config(auto_retain=False)
    p.sync_turn("keep this request", "keep this answer")
    p._client.aretain_batch.side_effect = RuntimeError("retain api unavailable")

    with pytest.raises(RuntimeError, match="retain api unavailable"):
        p.retain_persisted_session_lineage(
            session_id="test-session",
            wait=True,
            timeout=1,
        )

    row = _latest_submission_row(p)
    assert row[2] == "failed"
    assert row[3] is not None
    assert row[4] == "retain api unavailable"


def test_retain_before_session_reset_drains_pending_memory_work(provider_with_config):
    p = provider_with_config(
        auto_retain=False,
        retain_on_new=True,
        retain_on_new_timeout_seconds=7,
    )
    p.sync_turn("keep this request", "keep this answer")
    flush_pending = MagicMock(return_value=True)

    result = p.retain_before_session_reset(
        session_id="test-session",
        flush_pending=flush_pending,
    )

    assert result["queued"] is True
    flush_pending.assert_called_once_with(timeout=7.0)
    assert p._client.aretain_batch.call_count == 1


def test_retain_before_session_reset_uses_one_total_timeout(
    provider_with_config,
    monkeypatch,
):
    p = provider_with_config(
        auto_retain=False,
        retain_on_new=True,
        retain_on_new_timeout_seconds=7,
    )
    p.retain_persisted_session_lineage = MagicMock(
        return_value={"queued": True, "turn_count": 1}
    )
    monotonic = MagicMock(side_effect=[100.0, 103.0])
    monkeypatch.setattr(hindsight_module.time, "monotonic", monotonic)

    p.retain_before_session_reset(
        session_id="test-session",
        flush_pending=MagicMock(return_value=True),
    )

    p.retain_persisted_session_lineage.assert_called_once_with(
        session_id="test-session",
        parent_session_id="",
        wait=True,
        timeout=4.0,
    )


def test_retain_before_session_reset_aborts_when_pending_work_does_not_drain(
    provider_with_config,
):
    p = provider_with_config(
        auto_retain=False,
        retain_on_new=True,
        retain_on_new_timeout_seconds=7,
    )
    p.sync_turn("keep this request", "keep this answer")

    with pytest.raises(
        TimeoutError,
        match="Pending memory work did not finish within 7s",
    ):
        p.retain_before_session_reset(
            session_id="test-session",
            flush_pending=MagicMock(return_value=False),
        )

    p._client.aretain_batch.assert_not_called()


def test_waited_persisted_retain_times_out_while_api_request_is_running(
    provider_with_config,
):
    p = provider_with_config(auto_retain=False)
    p.sync_turn("keep this request", "keep this answer")
    p._run_hindsight_operation = MagicMock(
        side_effect=lambda _operation: threading.Event().wait(0.05)
    )

    with pytest.raises(
        TimeoutError,
        match="Hindsight retain did not finish within 0.01s",
    ):
        p.retain_persisted_session_lineage(
            session_id="test-session",
            wait=True,
            timeout=0.01,
        )

    p._retain_queue.join()


def test_waited_persisted_retain_caps_capability_probe_to_remaining_budget(
    provider_with_config,
    monkeypatch,
):
    p = provider_with_config(auto_retain=False)
    p.sync_turn("keep this request", "keep this answer")
    observed_timeouts = []
    _clear_capability_cache()

    def _fetch_version(_api_url, _api_key=None, timeout=5.0):
        observed_timeouts.append(timeout)
        return "0.5.6"

    monkeypatch.setattr(hindsight_module, "_fetch_hindsight_api_version", _fetch_version)

    try:
        result = p.retain_persisted_session_lineage(
            session_id="test-session",
            wait=True,
            timeout=0.25,
        )

        assert result["queued"] is True
        assert len(observed_timeouts) == 1
        assert 0 < observed_timeouts[0] <= 0.25
    finally:
        _clear_capability_cache()


def test_retain_before_session_reset_counts_payload_preparation_in_total_timeout(
    provider_with_config,
):
    p = provider_with_config(
        auto_retain=False,
        retain_on_new=True,
        retain_on_new_timeout_seconds=0.2,
    )
    p.sync_turn("keep this request", "keep this answer")
    original_resolve = p._resolve_full_retain_target_for_session

    def _slow_resolve(*args, **kwargs):
        threading.Event().wait(0.12)
        return original_resolve(*args, **kwargs)

    p._resolve_full_retain_target_for_session = _slow_resolve
    p._run_hindsight_operation = MagicMock(
        side_effect=lambda _operation: threading.Event().wait(0.12)
    )

    with pytest.raises(
        TimeoutError,
        match="Hindsight retain did not finish within",
    ):
        p.retain_before_session_reset(session_id="test-session")

    p._retain_queue.join()


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

def test_direct_flush_records_exact_successful_submission(provider_with_config):
    p = provider_with_config(auto_retain=False)
    p.sync_turn("ledger user", "ledger assistant")

    p.flush_retained_turns()
    p._retain_queue.join()

    submitted_content = p._client.aretain_batch.call_args.kwargs["items"][0]["content"]
    with sqlite3.connect(p._retain_store_path) as conn:
        row = conn.execute(
            "SELECT document_id, content_json, status, completed_at, error "
            "FROM hindsight_retain_submissions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row[0] == p._client.aretain_batch.call_args.kwargs["document_id"]
    assert row[1] == submitted_content
    assert row[2] == "succeeded"
    assert row[3] is not None
    assert row[4] == ""


def test_session_switch_flush_records_exact_successful_submission(provider_with_config):
    p = provider_with_config(auto_retain=False)
    p.sync_turn("switch ledger user", "switch ledger assistant")
    p._auto_retain = True

    p.on_session_switch("next-session")
    p._retain_queue.join()

    submitted_content = p._client.aretain_batch.call_args.kwargs["items"][0]["content"]
    with sqlite3.connect(p._retain_store_path) as conn:
        row = conn.execute(
            "SELECT document_id, content_json, status, completed_at, error "
            "FROM hindsight_retain_submissions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row[0] == p._client.aretain_batch.call_args.kwargs["document_id"]
    assert row[1] == submitted_content
    assert row[2] == "succeeded"
    assert row[3] is not None
    assert row[4] == ""


def _latest_submission_row(provider):
    with sqlite3.connect(provider._retain_store_path) as conn:
        return conn.execute(
            "SELECT document_id, content_json, status, completed_at, error "
            "FROM hindsight_retain_submissions ORDER BY id DESC LIMIT 1"
        ).fetchone()


def test_direct_flush_records_failed_submission(provider_with_config):
    p = provider_with_config(auto_retain=False)
    p.sync_turn("failed ledger user", "failed ledger assistant")
    p._client.aretain_batch.side_effect = RuntimeError("retain unavailable")

    p.flush_retained_turns()
    p._retain_queue.join()

    row = _latest_submission_row(p)
    assert row[2] == "failed"
    assert row[3] is not None
    assert row[4] == "retain unavailable"


def test_session_switch_flush_records_failed_submission(provider_with_config):
    p = provider_with_config(auto_retain=False)
    p.sync_turn("failed switch user", "failed switch assistant")
    p._client.aretain_batch.side_effect = RuntimeError("switch retain unavailable")
    p._auto_retain = True

    p.on_session_switch("next-session")
    p._retain_queue.join()

    row = _latest_submission_row(p)
    assert row[2] == "failed"
    assert row[3] is not None
    assert row[4] == "switch retain unavailable"


def test_direct_flush_ledger_insert_failure_preserves_retry_state(
    provider_with_config,
    monkeypatch,
):
    p = provider_with_config(auto_retain=False)
    p.sync_turn("ledger insert user", "ledger insert assistant")
    original_begin = p._begin_retain_submission

    def fail_begin(**_kwargs):
        raise sqlite3.OperationalError("ledger locked")

    monkeypatch.setattr(p, "_begin_retain_submission", fail_begin)

    with pytest.raises(sqlite3.OperationalError, match="ledger locked"):
        p.flush_retained_turns()

    assert p._last_queued_flush_count == 0
    assert p._retain_flush_pending is False
    p._client.aretain_batch.assert_not_called()

    monkeypatch.setattr(p, "_begin_retain_submission", original_begin)
    assert p.flush_retained_turns()["queued"] is True
    p._retain_queue.join()
    p._client.aretain_batch.assert_called_once()


def test_direct_flush_enqueue_failure_rolls_back_retry_state(
    provider_with_config,
    monkeypatch,
):
    p = provider_with_config(auto_retain=False)
    p.sync_turn("enqueue user", "enqueue assistant")
    original_put = p._retain_queue.put

    def fail_put(_item):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(p._retain_queue, "put", fail_put)

    with pytest.raises(RuntimeError, match="queue unavailable"):
        p.flush_retained_turns()

    assert p._last_queued_flush_count == 0
    assert p._retain_flush_pending is False
    row = _latest_submission_row(p)
    assert row[2] == "failed"
    assert row[4] == "queue unavailable"

    monkeypatch.setattr(p._retain_queue, "put", original_put)
    assert p.flush_retained_turns()["queued"] is True
    p._retain_queue.join()
    p._client.aretain_batch.assert_called_once()


def test_session_switch_ledger_insert_failure_preserves_old_session_for_retry(
    provider_with_config,
    monkeypatch,
):
    p = provider_with_config(auto_retain=False)
    p.sync_turn("switch insert user", "switch insert assistant")
    p._auto_retain = True
    original_begin = p._begin_retain_submission

    def fail_begin(**_kwargs):
        raise sqlite3.OperationalError("ledger locked")

    monkeypatch.setattr(p, "_begin_retain_submission", fail_begin)

    with pytest.raises(sqlite3.OperationalError, match="ledger locked"):
        p.on_session_switch("next-session")

    assert p._session_id == "test-session"
    assert p._last_queued_flush_count == 0
    assert len(p._session_turns) == 1
    p._client.aretain_batch.assert_not_called()

    monkeypatch.setattr(p, "_begin_retain_submission", original_begin)
    p.on_session_switch("next-session")
    p._retain_queue.join()
    p._client.aretain_batch.assert_called_once()


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


def test_rewind_counts_user_turns_with_trailing_async_assistant_event(provider_with_config, monkeypatch):
    from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
    with _append_capability_lock:
        _append_capability_cache.clear()
    monkeypatch.setattr(
        "plugins.memory.hindsight._fetch_hindsight_api_version",
        lambda *a, **kw: "0.5.6",
    )
    p = provider_with_config(auto_retain=False)
    p.sync_turn("keep user", "keep assistant")
    p.sync_turn("undo user", "undo assistant")
    p.sync_turn(
        "[ASYNC DELEGATION COMPLETE — deleg_rewind]",
        "visible async result after undo user",
    )

    p.on_session_rewind("test-session", turns_undone=1)

    assert len(p._session_turns) == 1
    info = p.retain_persisted_session_lineage(session_id="test-session")
    p._retain_queue.join()
    assert info["queued"] is True
    assert info["turn_count"] == 1
    content = p._client.aretain_batch.call_args.kwargs["items"][0]["content"]
    assert "keep user" in content
    assert "undo user" not in content
    assert "visible async result" not in content


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

def test_prefetch_sync_fallback_carries_actual_recall_for_next_turn(provider):
    result = provider.prefetch("first turn")

    assert "Memory 1" in result
    assert provider._prefetch_snapshot.query == "first turn"
    assert provider._prefetch_snapshot.results == ("Memory 1", "Memory 2")
    assert provider._prefetch_result == "- Memory 1\n- Memory 2"

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

def test_queue_prefetch_does_not_recall_or_replace_carried_snapshot(provider):
    provider._prefetch_result = "- actual current recall"
    provider._prefetch_snapshot = SimpleNamespace(
        query="specific current query",
        results=("actual current recall",),
    )
    provider._recall_snapshot_for_query = MagicMock(
        side_effect=AssertionError("post-turn raw query must not recall")
    )

    provider.queue_prefetch("继续。", session_id="test-session", turn_id="turn-2")

    provider._recall_snapshot_for_query.assert_not_called()
    assert provider._prefetch_result == "- actual current recall"
    assert provider._prefetch_snapshot.query == "specific current query"
    assert provider._prefetch_snapshot.results == ("actual current recall",)


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


def test_sync_turn_drops_async_payload_but_keeps_visible_assistant(provider_with_config):
    p = provider_with_config(auto_retain=False)

    p.sync_turn(
        "[ASYNC DELEGATION BATCH COMPLETE — deleg_deadbeef]\nA background fan-out finished.\nFIP draft saved.",
        "ack async result visible to the user",
    )
    p.sync_turn(
        "[Your active task list was preserved across context compression]\n- [>] keep researching",
        "ack todo noise",
    )
    p.sync_turn(
        "[Externalized payload: kind=raw_payload; role=user; chars=12; ref=abc]",
        "ack externalized noise",
    )
    p.sync_turn(
        (
            "[Current user objective preserved from compacted history]\n"
            "继续\n\n"
            "---\n\n"
            "[Session Arc Summary (d1, node 307)]\n"
            "## Active Current State\n"
            "- keep researching writing-skills\n"
        ),
        "继续执行",
    )
    p.sync_turn("正式用户消息", "正式回复")

    assert len(p._session_turns) == 3
    turns = [json.loads(t) for t in p._session_turns]
    assert [message["role"] for message in turns[0]] == ["assistant"]
    assert turns[0][0]["content"] == "Assistant: ack async result visible to the user"
    assert turns[1][0]["content"] == "User: 继续"
    assert turns[1][1]["content"] == "Assistant: 继续执行"
    assert turns[2][0]["content"] == "User: 正式用户消息"
    assert turns[2][1]["content"] == "Assistant: 正式回复"

    blob = "\n".join(p._session_turns)
    for forbidden in (
        "ASYNC DELEGATION",
        "active task list was preserved",
        "Externalized payload",
        "Session Arc Summary",
        "preserved from compacted history",
        "FIP draft saved",
        "ack todo noise",
        "ack externalized noise",
    ):
        assert forbidden not in blob


def test_transcript_sync_strips_runtime_payload_and_keeps_visible_results(provider_with_config):
    p = provider_with_config(auto_retain=False)
    messages = [
        {
            "role": "user",
            "content": (
                "[ASYNC DELEGATION BATCH COMPLETE — deleg_77e2600e]\n"
                "Context you provided: FIP DP review\n"
                "单号 CKD2007202607001274"
            ),
            "timestamp": 1710001000.0,
        },
        {
            "role": "assistant",
            "content": "async result noted",
            "timestamp": 1710001001.0,
        },
        {
            "role": "user",
            "content": (
                "继续\n\n"
                "[Your active task list was preserved across context compression]\n"
                "- [>] research-projects"
            ),
            "timestamp": 1710001002.0,
        },
        {
            "role": "assistant",
            "content": "已继续",
            "timestamp": 1710001003.0,
        },
        {
            "role": "user",
            "content": (
                "[Current user objective preserved from compacted history]\n"
                "提交你的修改\n\n"
                "---\n\n"
                "[Session Arc Summary (d1, node 307)]\n"
                "huge compression dump"
            ),
            "timestamp": 1710001004.0,
        },
        {
            "role": "assistant",
            "content": "已提交",
            "timestamp": 1710001005.0,
        },
    ]

    p.sync_turn("ignored scalar", "ignored scalar", messages=messages)

    assert len(p._session_turns) == 3
    turns = [json.loads(t) for t in p._session_turns]
    assert [message["role"] for message in turns[0]] == ["assistant"]
    assert turns[0][0]["content"] == "Assistant: async result noted"
    assert turns[1][0]["content"] == "User: 继续"
    assert turns[1][0]["timestamp"] == _local_seconds(1710001002.0)
    assert turns[1][1]["content"] == "Assistant: 已继续"
    assert turns[2][0]["content"] == "User: 提交你的修改"
    assert turns[2][1]["content"] == "Assistant: 已提交"

    info = p.retain_persisted_session_lineage()
    p._retain_queue.join()
    assert info["queued"] is True
    assert info["turn_count"] == 3
    content = p._client.aretain_batch.call_args.kwargs["items"][0]["content"]
    assert "async result noted" in content
    assert "继续" in content
    assert "提交你的修改" in content
    for forbidden in (
        "ASYNC DELEGATION",
        "active task list was preserved",
        "Session Arc Summary",
        "preserved from compacted history",
        "CKD2007202607001274",
        "ignored scalar",
    ):
        assert forbidden not in content


def test_transcript_sync_strips_model_switch_note_but_keeps_user_text(provider_with_config):
    p = provider_with_config(auto_retain=False)
    messages = [
        {
            "role": "user",
            "content": (
                "[Note: model was just switched from gpt-5.6-terra to gpt-5.6-sol "
                "via OpenAI Codex. Adjust your self-identification accordingly.]\n\n"
                "检查这个 Document"
            ),
            "timestamp": 1710001050.0,
        },
        {
            "role": "assistant",
            "content": "已检查",
            "timestamp": 1710001051.0,
        },
    ]

    p.sync_turn("ignored scalar", "ignored scalar", messages=messages)

    assert len(p._session_turns) == 1
    turn = json.loads(p._session_turns[0])
    assert turn[0]["content"] == "User: 检查这个 Document"
    assert turn[1]["content"] == "Assistant: 已检查"
    assert "model was just switched" not in p._session_turns[0]


def test_transcript_sync_strips_model_switch_note_from_multimodal_user_content(provider_with_config):
    p = provider_with_config(auto_retain=False)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
                {
                    "type": "text",
                    "text": (
                        "[Note: model was just switched from gpt-5.6-terra to gpt-5.6-sol "
                        "via OpenAI Codex. Adjust your self-identification accordingly.]\n\n"
                        "检查这张图"
                    ),
                },
            ],
            "timestamp": 1710001050.0,
        },
        {
            "role": "assistant",
            "content": "图片已检查",
            "timestamp": 1710001051.0,
        },
    ]

    p.sync_turn("ignored scalar", "ignored scalar", messages=messages)

    assert len(p._session_turns) == 1
    turn = json.loads(p._session_turns[0])
    assert "model was just switched" not in turn[0]["content"]
    assert "检查这张图" in turn[0]["content"]
    assert "https://example.com/image.png" in turn[0]["content"]
    assert "[Image attached]" in turn[0]["content"]
    assert turn[1]["content"] == "Assistant: 图片已检查"


def test_transcript_sync_preserves_model_switch_text_after_first_multimodal_text_part(provider_with_config):
    p = provider_with_config(auto_retain=False)
    quoted_note = (
        "[Note: model was just switched from old-model to new-model via OpenAI Codex. "
        "Adjust your self-identification accordingly.]"
    )
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
                {"type": "text", "text": "真实第一段"},
                {"type": "text", "text": quoted_note},
            ],
            "timestamp": 1710001052.0,
        },
        {"role": "assistant", "content": "已解释", "timestamp": 1710001053.0},
    ]

    p.sync_turn("ignored scalar", "ignored scalar", messages=messages)

    turn = json.loads(p._session_turns[0])
    assert "真实第一段" in turn[0]["content"]
    assert quoted_note in turn[0]["content"]


def test_historical_multimodal_cleaning_preserves_model_switch_text_after_first_text_part(provider_with_config):
    p = provider_with_config(auto_retain=False)
    quoted_note = (
        "[Note: model was just switched from old-model to new-model via OpenAI Codex. "
        "Adjust your self-identification accordingly.]"
    )
    serialized_parts = json.dumps(
        [
            {"type": "image_url", "image_url": {"url": "https://example.com/historical.png"}},
            {"type": "text", "text": "历史真实第一段"},
            {"type": "text", "text": quoted_note},
        ],
        ensure_ascii=False,
    )
    dirty_turn = json.dumps(
        [
            {"role": "user", "content": f"User: {serialized_parts}", "timestamp": "2024-01-01T00:00:00"},
            {"role": "assistant", "content": "Assistant: 已解释", "timestamp": "2024-01-01T00:00:01"},
        ],
        ensure_ascii=False,
    )

    cleaned_turn = p._sanitize_persisted_turn_json(dirty_turn)

    assert cleaned_turn is not None
    assert "历史真实第一段" in cleaned_turn
    assert quoted_note in cleaned_turn


def test_transcript_sync_drops_async_payload_but_keeps_visible_assistant_in_order(provider_with_config):
    p = provider_with_config(auto_retain=False)
    messages = [
        {"role": "user", "content": "先把方案跑完", "timestamp": 1710001100.0},
        {"role": "assistant", "content": "新版实测已经跑完，等待独立复核。", "timestamp": 1710001101.0},
        {
            "role": "user",
            "content": (
                "[ASYNC DELEGATION BATCH COMPLETE — deleg_visible]\n"
                "INTERNAL ASYNC PAYLOAD THAT MUST NOT BE RETAINED"
            ),
            "timestamp": 1710001102.0,
        },
        {
            "role": "assistant",
            "content": "## 重新跑完了\n这是用户实际看见的正式结论。",
            "timestamp": 1710001103.0,
        },
        {"role": "user", "content": "先说最终结论", "timestamp": 1710001104.0},
        {"role": "assistant", "content": "最终结论保持原样。", "timestamp": 1710001105.0},
    ]

    p.sync_turn("ignored scalar", "ignored scalar", messages=messages)

    assert len(p._session_turns) == 3
    turns = [json.loads(turn) for turn in p._session_turns]
    assert [message["role"] for message in turns[0]] == ["user", "assistant"]
    assert [message["role"] for message in turns[1]] == ["assistant"]
    assert turns[1][0]["content"] == "Assistant: ## 重新跑完了\n这是用户实际看见的正式结论。"
    assert [message["role"] for message in turns[2]] == ["user", "assistant"]

    info = p.retain_persisted_session_lineage()
    p._retain_queue.join()
    assert info["queued"] is True
    content = p._client.aretain_batch.call_args.kwargs["items"][0]["content"]
    assert "ASYNC DELEGATION" not in content
    assert "INTERNAL ASYNC PAYLOAD" not in content
    assert content.index("新版实测已经跑完") < content.index("重新跑完了") < content.index("先说最终结论")


def test_transcript_sync_keeps_last_visible_assistant_after_async_completion(provider_with_config):
    p = provider_with_config(auto_retain=False)
    messages = [
        {
            "role": "user",
            "content": "[ASYNC DELEGATION COMPLETE — deleg_last]",
            "timestamp": 1710001200.0,
        },
        {
            "role": "assistant",
            "content": "intermediate assistant draft",
            "timestamp": 1710001201.0,
        },
        {
            "role": "assistant",
            "content": "final user-visible async result",
            "timestamp": 1710001202.0,
        },
    ]

    p.sync_turn("ignored scalar", "ignored scalar", messages=messages)

    assert len(p._session_turns) == 1
    turn = json.loads(p._session_turns[0])
    assert [message["role"] for message in turn] == ["assistant"]
    assert turn[0]["content"] == "Assistant: final user-visible async result"
    assert "intermediate assistant draft" not in p._session_turns[0]


def test_async_completion_does_not_pair_visible_assistant_with_prior_orphan_user(provider_with_config):
    p = provider_with_config(auto_retain=False)
    messages = [
        {
            "role": "user",
            "content": "earlier interrupted user request",
            "timestamp": 1710001300.0,
        },
        {
            "role": "user",
            "content": "[ASYNC DELEGATION COMPLETE — deleg_orphan]",
            "timestamp": 1710001301.0,
        },
        {
            "role": "assistant",
            "content": "visible result from the async completion",
            "timestamp": 1710001302.0,
        },
        {
            "role": "user",
            "content": "next real user request",
            "timestamp": 1710001303.0,
        },
        {
            "role": "assistant",
            "content": "next real assistant response",
            "timestamp": 1710001304.0,
        },
    ]

    p.sync_turn("ignored scalar", "ignored scalar", messages=messages)

    turns = [json.loads(turn) for turn in p._session_turns]
    assert [[message["role"] for message in turn] for turn in turns] == [
        ["user"],
        ["assistant"],
        ["user", "assistant"],
    ]
    assert turns[0][0]["content"] == "User: earlier interrupted user request"
    assert turns[1][0]["content"] == "Assistant: visible result from the async completion"
    assert turns[2][0]["content"] == "User: next real user request"


def test_partial_replay_anchor_prefers_timestamp_identity_for_repeated_text(provider_with_config):
    p = provider_with_config(auto_retain=False)

    def assistant_event(content, timestamp):
        return json.dumps(
            [{"role": "assistant", "content": f"Assistant: {content}", "timestamp": timestamp}],
            ensure_ascii=False,
        )

    existing = [
        assistant_event("same repeated result", "2026-07-16T01:00:00"),
        assistant_event("existing middle event", "2026-07-16T01:01:00"),
        assistant_event("same repeated result", "2026-07-16T01:02:00"),
        assistant_event("later exact anchor", "2026-07-16T01:03:00"),
    ]
    incoming = [
        assistant_event("new event before second repeat", "2026-07-16T01:01:30"),
        assistant_event("same repeated result", "2026-07-16T01:02:00"),
        assistant_event("later exact anchor", "2026-07-16T01:03:00"),
    ]

    merged = p._merge_overlapping_replayed_turns(existing, incoming)

    assert merged is not None
    assert [json.loads(turn)[0]["content"] for turn in merged] == [
        "Assistant: same repeated result",
        "Assistant: existing middle event",
        "Assistant: new event before second repeat",
        "Assistant: same repeated result",
        "Assistant: later exact anchor",
    ]


def test_partial_replay_anchor_uses_following_exact_anchor_when_repeated_time_drifts(
    provider_with_config,
):
    p = provider_with_config(auto_retain=False)

    def assistant_event(content, timestamp):
        return json.dumps(
            [{"role": "assistant", "content": f"Assistant: {content}", "timestamp": timestamp}],
            ensure_ascii=False,
        )

    existing = [
        assistant_event("same repeated result", "2026-07-16T01:00:00"),
        assistant_event("existing middle event", "2026-07-16T01:01:00"),
        assistant_event("same repeated result", "2026-07-16T01:02:00"),
        assistant_event("later exact anchor", "2026-07-16T01:03:00"),
    ]
    incoming = [
        assistant_event("new event before second repeat", "2026-07-16T01:01:30"),
        assistant_event("same repeated result", "2026-07-16T01:01:59"),
        assistant_event("later exact anchor", "2026-07-16T01:03:00"),
    ]

    merged = p._merge_overlapping_replayed_turns(existing, incoming)

    assert merged is not None
    assert [json.loads(turn)[0]["content"] for turn in merged] == [
        "Assistant: same repeated result",
        "Assistant: existing middle event",
        "Assistant: new event before second repeat",
        "Assistant: same repeated result",
        "Assistant: later exact anchor",
    ]


def test_partial_replay_anchor_handles_timestamp_drift_past_candidate(
    provider_with_config,
):
    p = provider_with_config(auto_retain=False)

    def assistant_event(content, timestamp):
        return json.dumps(
            [{"role": "assistant", "content": f"Assistant: {content}", "timestamp": timestamp}],
            ensure_ascii=False,
        )

    existing = [
        assistant_event("same repeated result", "2026-07-16T01:00:00"),
        assistant_event("existing middle event", "2026-07-16T01:01:00"),
        assistant_event("same repeated result", "2026-07-16T01:02:00"),
        assistant_event("later exact anchor", "2026-07-16T01:03:00"),
    ]
    incoming = [
        assistant_event("new event before second repeat", "2026-07-16T01:01:30"),
        assistant_event("same repeated result", "2026-07-16T01:02:01"),
        assistant_event("later exact anchor", "2026-07-16T01:03:00"),
    ]

    merged = p._merge_overlapping_replayed_turns(existing, incoming)

    assert merged is not None
    contents = [json.loads(turn)[0]["content"] for turn in merged]
    assert contents == [
        "Assistant: same repeated result",
        "Assistant: existing middle event",
        "Assistant: new event before second repeat",
        "Assistant: same repeated result",
        "Assistant: later exact anchor",
    ]
    assert contents.count("Assistant: same repeated result") == 2


def test_partial_replay_does_not_anchor_repeat_after_following_exact_anchor(
    provider_with_config,
):
    p = provider_with_config(auto_retain=False)

    def assistant_event(content, timestamp):
        return json.dumps(
            [{"role": "assistant", "content": f"Assistant: {content}", "timestamp": timestamp}],
            ensure_ascii=False,
        )

    existing = [
        assistant_event("later exact anchor", "2026-07-16T01:03:00"),
        assistant_event("same repeated result", "2026-07-16T01:04:00"),
        assistant_event("tail", "2026-07-16T01:05:00"),
    ]
    incoming = [
        assistant_event("same repeated result", "2026-07-16T01:02:00"),
        assistant_event("later exact anchor", "2026-07-16T01:03:00"),
        assistant_event("tail", "2026-07-16T01:05:00"),
    ]

    merged = p._merge_overlapping_replayed_turns(existing, incoming)

    assert merged is not None
    assert [json.loads(turn)[0]["content"] for turn in merged] == [
        "Assistant: same repeated result",
        "Assistant: later exact anchor",
        "Assistant: same repeated result",
        "Assistant: tail",
    ]


def test_lcm_durable_and_depth_summaries_are_dropped(provider_with_config):
    p = provider_with_config(auto_retain=False)
    p.sync_turn("[Durable Summary (d2, node 9)]\nlong durable dump", "should not keep")
    p.sync_turn("[Depth-3 Summary (d3, node 12)]\ndeep dump", "should not keep")
    p.sync_turn(
        "真实用户\n\n[Durable Summary (d2, node 9)]\ntrailer",
        "ok",
    )
    assert len(p._session_turns) == 1
    turn = json.loads(p._session_turns[0])
    assert turn[0]["content"] == "User: 真实用户"
    assert "Durable Summary" not in p._session_turns[0]
    assert "Depth-3" not in p._session_turns[0]


def test_assistant_summary_markers_are_not_retained(provider_with_config):
    p = provider_with_config(auto_retain=False)
    messages = [
        {"role": "user", "content": "真实问题", "timestamp": 1710002000.0},
        {"role": "assistant", "content": "[Session Arc Summary (d1, node 1)]\nshould drop", "timestamp": 1710002001.0},
        {"role": "assistant", "content": "最终可见回答", "timestamp": 1710002002.0},
    ]
    p.sync_turn("ignored", "ignored", messages=messages)
    assert len(p._session_turns) == 1
    turn = json.loads(p._session_turns[0])
    assert turn[0]["content"] == "User: 真实问题"
    assert turn[1]["content"] == "Assistant: 最终可见回答"
    assert "Session Arc Summary" not in p._session_turns[0]


def test_transcript_sync_keeps_visible_assistant_after_lcm_summary_only_window(provider_with_config):
    p = provider_with_config(auto_retain=False)
    messages = [
        {
            "role": "user",
            "content": "[Recent Summary (d0, node 564)]\ncompressed source conversation",
            "timestamp": 1710002100.0,
        },
        {
            "role": "assistant",
            "content": "最终可见的评估结论",
            "timestamp": 1710002101.0,
        },
    ]

    p.sync_turn(messages[0]["content"], messages[1]["content"], messages=messages)

    assert len(p._session_turns) == 1
    turn = json.loads(p._session_turns[0])
    assert [message["role"] for message in turn] == ["assistant"]
    assert turn[0]["content"] == "Assistant: 最终可见的评估结论"
    assert "Recent Summary" not in p._session_turns[0]


def test_transcript_sync_preserves_real_user_recent_summary_lookalike(provider_with_config):
    p = provider_with_config(auto_retain=False)
    messages = [
        {
            "role": "user",
            "content": "[Recent Summary request]\n这是用户真实输入",
            "timestamp": 1710002200.0,
        },
        {
            "role": "assistant",
            "content": "正常回答",
            "timestamp": 1710002201.0,
        },
    ]

    p.sync_turn(messages[0]["content"], messages[1]["content"], messages=messages)

    assert len(p._session_turns) == 1
    turn = json.loads(p._session_turns[0])
    assert [message["role"] for message in turn] == ["user", "assistant"]
    assert turn[0]["content"] == "User: [Recent Summary request]\n这是用户真实输入"
    assert turn[1]["content"] == "Assistant: 正常回答"


def test_hindsight_preserves_safe_image_marker_url_but_strips_local_path():
    provider = hindsight_module.HindsightMemoryProvider()
    safe_url = "https://example.com/screenshot.png"

    safe = provider._clean_retain_user_content(
        f"check this\n\n[Image attached at: {safe_url}]"
    )
    local = provider._clean_retain_user_content(
        "check this\n\n[Image attached at: /tmp/screenshot.png]"
    )

    assert safe_url in safe
    assert "[Image attached]" in safe
    assert "/tmp/screenshot.png" not in local
    assert "[Image attached]" in local


def test_clean_on_retain_strips_historical_runtime_payload_but_keeps_visible_content(provider_with_config):
    p = provider_with_config(auto_retain=False)
    # Simulate pre-fix dirty rows already in sqlite.
    with p._retain_store_connect() as conn:
        conn.execute(
            """
            INSERT INTO hindsight_retain_turns
            (bank_id, session_id, parent_session_id, retain_document_id, turn_index, turn_json, created_at, active)
            VALUES (?, ?, '', ?, 1, ?, 1710003000.0, 1)
            """,
            (
                p._bank_id,
                p._session_id,
                p._session_id,
                json.dumps(
                    [
                        {
                            "role": "user",
                            "content": "User: [ASYNC DELEGATION BATCH COMPLETE — deleg_x]\nFIP noise",
                            "timestamp": "2024-01-01T00:00:00",
                        },
                        {
                            "role": "assistant",
                            "content": "Assistant: user-visible async result",
                            "timestamp": "2024-01-01T00:00:01",
                        },
                    ],
                    ensure_ascii=False,
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO hindsight_retain_turns
            (bank_id, session_id, parent_session_id, retain_document_id, turn_index, turn_json, created_at, active)
            VALUES (?, ?, '', ?, 2, ?, 1710003001.0, 1)
            """,
            (
                p._bank_id,
                p._session_id,
                p._session_id,
                json.dumps(
                    [
                        {
                            "role": "user",
                            "content": (
                                "User: [Current user objective preserved from compacted history]\n"
                                "继续\n\n---\n\n[Session Arc Summary (d1, node 1)]\ndump"
                            ),
                            "timestamp": "2024-01-01T00:00:02",
                        },
                        {
                            "role": "assistant",
                            "content": "Assistant: 已继续",
                            "timestamp": "2024-01-01T00:00:03",
                        },
                    ],
                    ensure_ascii=False,
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO hindsight_retain_turns
            (bank_id, session_id, parent_session_id, retain_document_id, turn_index, turn_json, created_at, active)
            VALUES (?, ?, '', ?, 3, ?, 1710003002.0, 1)
            """,
            (
                p._bank_id,
                p._session_id,
                p._session_id,
                json.dumps(
                    [
                        {
                            "role": "user",
                            "content": (
                                "User: [Note: model was just switched from gpt-5.6-terra to gpt-5.6-sol "
                                "via OpenAI Codex. Adjust your self-identification accordingly.]\n\n"
                                "检查这个 Document"
                            ),
                            "timestamp": "2024-01-01T00:00:04",
                        },
                        {
                            "role": "assistant",
                            "content": "Assistant: 已检查",
                            "timestamp": "2024-01-01T00:00:05",
                        },
                    ],
                    ensure_ascii=False,
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO hindsight_retain_turns
            (bank_id, session_id, parent_session_id, retain_document_id, turn_index, turn_json, created_at, active)
            VALUES (?, ?, '', ?, 4, ?, 1710003003.0, 1)
            """,
            (
                p._bank_id,
                p._session_id,
                p._session_id,
                json.dumps(
                    [
                        {
                            "role": "user",
                            "content": "User: " + json.dumps(
                                [
                                    {
                                        "type": "image_url",
                                        "image_url": {"url": "https://example.com/historical.png"},
                                    },
                                    {
                                        "type": "text",
                                        "text": (
                                            "[Note: model was just switched from old-model to new-model "
                                            "via OpenAI Codex. Adjust your self-identification accordingly.]\n\n"
                                            "检查历史图片"
                                        ),
                                    },
                                ],
                                ensure_ascii=False,
                            ),
                            "timestamp": "2024-01-01T00:00:06",
                        },
                        {
                            "role": "assistant",
                            "content": "Assistant: 历史图片已检查",
                            "timestamp": "2024-01-01T00:00:07",
                        },
                    ],
                    ensure_ascii=False,
                ),
            ),
        )

    info = p.retain_persisted_session_lineage()
    p._retain_queue.join()
    assert info["queued"] is True
    assert info["turn_count"] == 4
    content = p._client.aretain_batch.call_args.kwargs["items"][0]["content"]
    assert "user-visible async result" in content
    assert "继续" in content
    assert "已继续" in content
    assert "检查这个 Document" in content
    assert "已检查" in content
    assert "检查历史图片" in content
    assert "https://example.com/historical.png" in content
    assert "[Image attached]" in content
    assert content.index("user-visible async result") < content.index("继续")
    for forbidden in (
        "ASYNC DELEGATION",
        "Session Arc Summary",
        "preserved from compacted history",
        "model was just switched",
        "FIP noise",
    ):
        assert forbidden not in content
