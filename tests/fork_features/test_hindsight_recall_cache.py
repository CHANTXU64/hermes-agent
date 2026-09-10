from __future__ import annotations

from fork_features.hindsight_recall_cache import (
    CarryResult,
    HindsightRecallCache,
    RecallSnapshot,
    should_sync_cache_miss,
)


def test_hindsight_provider_uses_fork_cache_without_legacy_state_fields():
    from plugins.memory.hindsight import HindsightMemoryProvider

    provider = HindsightMemoryProvider()

    assert isinstance(provider._recall_cache, HindsightRecallCache)
    legacy_names = {
        "_prefetch_result",
        "_prefetch_snapshot",
        "_prefetch_count",
        "_prefetch_lock",
        "_prefetch_thread",
        "_prefetch_generation",
        "_active_prefetch_turn",
    }
    assert not any(hasattr(provider, name) for name in legacy_names)


def test_matching_session_consumes_cache_but_stale_session_preserves_it():
    cache = HindsightRecallCache(initial_session_id="session-current")
    cache.seed(RecallSnapshot(query="current target", results=("current memory",)))

    stale = cache.begin_turn(
        requested_session_id="session-old",
        turn_id="turn-old",
    )

    assert stale is None
    assert cache.snapshot == RecallSnapshot(
        query="current target",
        results=("current memory",),
    )
    assert cache.result == "- current memory"

    current = cache.begin_turn(
        requested_session_id="session-current",
        turn_id="turn-current",
    )

    assert current is not None
    assert current.generation == 1
    assert current.turn_id == "turn-current"
    assert current.result == "- current memory"
    assert current.snapshot == RecallSnapshot(
        query="current target",
        results=("current memory",),
    )
    assert cache.result == ""
    assert cache.snapshot is None


def test_late_old_generation_cannot_overwrite_current_turn_cache():
    cache = HindsightRecallCache(initial_session_id="session-current")
    first = cache.begin_turn(
        requested_session_id="session-current",
        turn_id="turn-first",
    )
    second = cache.begin_turn(
        requested_session_id="session-current",
        turn_id="turn-second",
    )
    assert first is not None
    assert second is not None

    stale = cache.carry(
        RecallSnapshot(query="old target", results=("old memory",)),
        expected_generation=first.generation,
        expected_session_id="session-current",
    )

    assert stale is CarryResult.STALE_GENERATION
    assert cache.result == ""
    assert cache.snapshot is None

    applied = cache.carry(
        RecallSnapshot(query="current target", results=("current memory",)),
        expected_generation=second.generation,
        expected_session_id="session-current",
    )

    assert applied is CarryResult.APPLIED
    assert cache.result == "- current memory"
    assert cache.snapshot == RecallSnapshot(
        query="current target",
        results=("current memory",),
    )


def test_timeout_invalidates_only_the_matching_active_turn():
    cache = HindsightRecallCache(initial_session_id="session-current")
    active = cache.begin_turn(
        requested_session_id="session-current",
        turn_id="turn-current",
    )
    assert active is not None
    assert cache.carry(
        RecallSnapshot(query="target", results=("memory",)),
        expected_generation=active.generation,
        expected_session_id="session-current",
    ) is CarryResult.APPLIED

    assert cache.invalidate_timeout(
        session_id="session-current",
        turn_id="turn-other",
    ) is False
    assert cache.result == "- memory"

    assert cache.invalidate_timeout(
        session_id="session-current",
        turn_id="turn-current",
    ) is True
    assert cache.result == ""
    assert cache.snapshot is None
    assert cache.carry(
        RecallSnapshot(query="late", results=("late memory",)),
        expected_generation=active.generation,
        expected_session_id="session-current",
    ) is CarryResult.STALE_GENERATION


def test_session_switch_clears_cache_and_rejects_late_old_result():
    cache = HindsightRecallCache(initial_session_id="session-old")
    old_turn = cache.begin_turn(
        requested_session_id="session-old",
        turn_id="turn-old",
    )
    assert old_turn is not None
    assert cache.carry(
        RecallSnapshot(query="old target", results=("old memory",)),
        expected_generation=old_turn.generation,
        expected_session_id="session-old",
    ) is CarryResult.APPLIED

    cache.switch_session("session-new")

    assert cache.result == ""
    assert cache.snapshot is None
    assert cache.carry(
        RecallSnapshot(query="late old", results=("late old memory",)),
        expected_generation=old_turn.generation,
        expected_session_id="session-old",
    ) is CarryResult.STALE_SESSION
    assert cache.begin_turn(
        requested_session_id="session-old",
        turn_id="late-old-turn",
    ) is None

    new_turn = cache.begin_turn(
        requested_session_id="session-new",
        turn_id="turn-new",
    )
    assert new_turn is not None
    assert new_turn.result == ""
    assert new_turn.snapshot is None


def test_rewind_invalidation_keeps_session_but_rejects_pre_rewind_result():
    cache = HindsightRecallCache(initial_session_id="session-current")
    before_rewind = cache.begin_turn(
        requested_session_id="session-current",
        turn_id="turn-before-rewind",
    )
    assert before_rewind is not None
    cache.invalidate()

    assert cache.carry(
        RecallSnapshot(query="late", results=("late memory",)),
        expected_generation=before_rewind.generation,
        expected_session_id="session-current",
    ) is CarryResult.STALE_GENERATION

    after_rewind = cache.begin_turn(
        requested_session_id="session-current",
        turn_id="turn-after-rewind",
    )
    assert after_rewind is not None
    assert after_rewind.result == ""
    assert after_rewind.snapshot is None


def test_session_bind_preserves_cache_without_becoming_a_lifecycle_rotation():
    cache = HindsightRecallCache(initial_session_id="session-a")
    active = cache.begin_turn(
        requested_session_id="session-a",
        turn_id="turn-a",
    )
    assert active is not None
    assert cache.carry(
        RecallSnapshot(query="target", results=("memory",)),
        expected_generation=active.generation,
        expected_session_id="session-a",
    ) is CarryResult.APPLIED

    cache.bind_session("session-b")

    assert cache.session_id == "session-b"
    assert cache.result == "- memory"
    assert cache.begin_turn(
        requested_session_id="session-a",
        turn_id="late-a",
    ) is None
    current = cache.begin_turn(
        requested_session_id="session-b",
        turn_id="turn-b",
    )
    assert current is not None
    assert current.result == "- memory"
    assert current.snapshot == RecallSnapshot(
        query="target",
        results=("memory",),
    )


def test_sync_cache_miss_gate_preserves_all_existing_skip_conditions():
    enabled = {
        "sync_enabled": True,
        "memory_mode": "hybrid",
        "auto_recall": True,
        "shutting_down": False,
        "query": "current question",
    }
    assert should_sync_cache_miss(**enabled) is True

    for override in (
        {"sync_enabled": False},
        {"memory_mode": "tools"},
        {"auto_recall": False},
        {"shutting_down": True},
        {"query": "  "},
    ):
        case = {**enabled, **override}
        assert should_sync_cache_miss(**case) is False
