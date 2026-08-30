"""Fork-owned Hindsight Recall/P5 regressions.

The upstream provider suite owns automatic Retain and the explicit
``hindsight_retain`` tool. This file protects only retained fork Recall/P5
behaviour plus one append-mode compatibility boundary.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fork_features.hindsight_recall_cache import RecallSnapshot
from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
from tests.plugins.memory.test_hindsight_provider import provider, provider_with_config


def _clear_capability_cache():
    with _append_capability_lock:
        _append_capability_cache.clear()


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
    assert provider._recall_cache.snapshot.query == "first turn"
    assert provider._recall_cache.snapshot.results == ("Memory 1", "Memory 2")
    assert provider._recall_cache.result == "- Memory 1\n- Memory 2"


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
    provider._recall_cache.seed(RecallSnapshot(
        query="specific current query",
        results=("actual current recall",),
    ))
    provider._recall_snapshot_for_query = MagicMock(
        side_effect=AssertionError("post-turn raw query must not recall")
    )

    provider.queue_prefetch("继续。", session_id="test-session", turn_id="turn-2")

    provider._recall_snapshot_for_query.assert_not_called()
    assert provider._recall_cache.result == "- actual current recall"
    assert provider._recall_cache.snapshot.query == "specific current query"
    assert provider._recall_cache.snapshot.results == ("actual current recall",)


def test_prefetch_result_cleared_on_switch(provider_with_config):
    """Stale recall text from the old session must not leak into the
    next session's first prefetch read."""
    provider = provider_with_config(recall_sync_on_cache_miss=False)
    provider._recall_cache.seed(RecallSnapshot(
        query="old-session query",
        results=("old-session recall: User likes Rust",),
    ))
    provider.on_session_switch("new-sid")
    assert provider._recall_cache.result == ""
    # And subsequent prefetch() should now report empty, not the leftover.
    assert provider.prefetch("anything") == ""


def test_first_prefetch_after_switch_sync_recalls_new_query(provider):
    provider._recall_cache.seed(RecallSnapshot(
        query="old-session query",
        results=("old-session recall",),
    ))
    provider.on_session_switch("new-sid")
    result = provider.prefetch("new-session question")
    assert "Memory 1" in result
    assert "old-session recall" not in result
    assert provider._client.arecall.call_args.kwargs["query"] == "new-session question"


def test_sync_turn_rebinds_cache_session_without_clearing_carried_recall(provider):
    provider._retain_every_n_turns = 2
    provider._recall_cache.seed(
        RecallSnapshot(query="carried target", results=("carried memory",))
    )

    provider.sync_turn("user", "assistant", session_id="session-b")

    assert provider._session_id == "session-b"
    assert provider._recall_cache.session_id == "session-b"
    assert provider._recall_cache.result == "- carried memory"


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
