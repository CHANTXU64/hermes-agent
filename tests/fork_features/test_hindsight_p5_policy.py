from __future__ import annotations

import pytest

from fork_features.hindsight_recall_cache import RecallSnapshot
from plugins.memory import hindsight as hindsight_provider
from plugins.memory.hindsight import recall_preprocessor as p5


def test_provider_uses_p5_policy_without_raw_preprocessor_alias():
    assert hindsight_provider.apply_recall_preprocessor is p5.apply_recall_preprocessor
    assert not hasattr(hindsight_provider, "run_recall_preprocessor")


def test_provider_rejects_nonfallback_outcome_without_snapshot(monkeypatch):
    provider = hindsight_provider.HindsightMemoryProvider()
    provider._memory_mode = "auto"
    provider._auto_recall = True
    provider._recall_cache.seed(
        RecallSnapshot(query="old target", results=("old memory",))
    )
    monkeypatch.setattr(
        hindsight_provider,
        "apply_recall_preprocessor",
        lambda **_kwargs: p5.RecallPreprocessOutcome(
            snapshot=None,
            fall_back_to_current_query=False,
        ),
    )

    with pytest.raises(RuntimeError, match="no snapshot without fallback"):
        provider.prefetch(
            "current target",
            previous_assistant_message="previous answer",
        )


def test_filters_old_refs_and_appends_new_recall_snapshot(monkeypatch):
    monkeypatch.setattr(
        p5,
        "run_recall_preprocessor",
        lambda **_kwargs: p5.RecallPreprocessDecision(
            drop_old_refs=(2,),
            new_query="new target",
        ),
    )
    recall_queries: list[str] = []

    def recall_snapshot(query: str) -> RecallSnapshot:
        recall_queries.append(query)
        return RecallSnapshot(
            query="clipped new target",
            results=("new memory",),
        )

    outcome = p5.apply_recall_preprocessor(
        current_user_message="继续",
        previous_assistant_message="上一轮已经定位到新目标。",
        previous_snapshot=RecallSnapshot(
            query="old target",
            results=("keep old", "drop old"),
        ),
        recall_snapshot_for_query=recall_snapshot,
    )

    assert recall_queries == ["new target"]
    assert not outcome.fall_back_to_current_query
    assert outcome.snapshot == RecallSnapshot(
        query="clipped new target",
        results=("keep old", "new memory"),
    )


def test_preprocessor_failure_restores_complete_old_snapshot(monkeypatch):
    def fail_preprocessor(**_kwargs):
        raise RuntimeError("route failed")

    monkeypatch.setattr(p5, "run_recall_preprocessor", fail_preprocessor)

    outcome = p5.apply_recall_preprocessor(
        current_user_message="继续",
        previous_assistant_message="previous answer",
        previous_snapshot=RecallSnapshot(
            query="old target",
            results=("old one", "old two"),
        ),
        recall_snapshot_for_query=lambda _query: (_ for _ in ()).throw(
            AssertionError("new recall must not run after P5 failure")
        ),
    )

    assert not outcome.fall_back_to_current_query
    assert outcome.snapshot == RecallSnapshot(
        query="old target",
        results=("old one", "old two"),
    )


def test_new_query_recall_failure_restores_complete_old_snapshot(monkeypatch):
    monkeypatch.setattr(
        p5,
        "run_recall_preprocessor",
        lambda **_kwargs: p5.RecallPreprocessDecision(
            drop_old_refs=(2,),
            new_query="new target",
        ),
    )

    def fail_recall(_query: str) -> RecallSnapshot:
        raise TimeoutError("recall timed out")

    outcome = p5.apply_recall_preprocessor(
        current_user_message="继续",
        previous_assistant_message="previous answer",
        previous_snapshot=RecallSnapshot(
            query="old target",
            results=("old one", "old two"),
        ),
        recall_snapshot_for_query=fail_recall,
    )

    assert not outcome.fall_back_to_current_query
    assert outcome.snapshot == RecallSnapshot(
        query="old target",
        results=("old one", "old two"),
    )


def test_preprocessor_failure_without_old_results_requests_current_query_fallback(
    monkeypatch,
):
    def fail_preprocessor(**_kwargs):
        raise RuntimeError("route failed")

    monkeypatch.setattr(p5, "run_recall_preprocessor", fail_preprocessor)

    outcome = p5.apply_recall_preprocessor(
        current_user_message="current query",
        previous_assistant_message="previous answer",
        previous_snapshot=RecallSnapshot(query="", results=()),
        recall_snapshot_for_query=lambda _query: (_ for _ in ()).throw(
            AssertionError("current-query fallback belongs to the Provider")
        ),
    )

    assert outcome.fall_back_to_current_query
    assert outcome.snapshot is None


def test_new_query_recall_failure_without_old_results_requests_current_fallback(
    monkeypatch,
):
    monkeypatch.setattr(
        p5,
        "run_recall_preprocessor",
        lambda **_kwargs: p5.RecallPreprocessDecision(
            drop_old_refs=(),
            new_query="generated query",
        ),
    )

    def fail_recall(_query: str) -> RecallSnapshot:
        raise TimeoutError("recall timed out")

    outcome = p5.apply_recall_preprocessor(
        current_user_message="current query",
        previous_assistant_message="previous answer",
        previous_snapshot=RecallSnapshot(query="", results=()),
        recall_snapshot_for_query=fail_recall,
    )

    assert outcome.fall_back_to_current_query
    assert outcome.snapshot is None


def test_null_query_reuses_only_selected_old_results(monkeypatch):
    monkeypatch.setattr(
        p5,
        "run_recall_preprocessor",
        lambda **_kwargs: p5.RecallPreprocessDecision(
            drop_old_refs=(2,),
            new_query=None,
        ),
    )

    outcome = p5.apply_recall_preprocessor(
        current_user_message="继续",
        previous_assistant_message="previous answer",
        previous_snapshot=RecallSnapshot(
            query="old target",
            results=("keep old", "drop old"),
        ),
        recall_snapshot_for_query=lambda _query: (_ for _ in ()).throw(
            AssertionError("null query must not run Hindsight recall")
        ),
    )

    assert not outcome.fall_back_to_current_query
    assert outcome.snapshot == RecallSnapshot(
        query="old target",
        results=("keep old",),
    )


def test_null_query_that_drops_every_old_ref_clears_the_recall_chain(monkeypatch):
    monkeypatch.setattr(
        p5,
        "run_recall_preprocessor",
        lambda **_kwargs: p5.RecallPreprocessDecision(
            drop_old_refs=(1, 2),
            new_query=None,
        ),
    )

    outcome = p5.apply_recall_preprocessor(
        current_user_message="收尾",
        previous_assistant_message="previous answer",
        previous_snapshot=RecallSnapshot(
            query="old target",
            results=("old one", "old two"),
        ),
        recall_snapshot_for_query=lambda _query: (_ for _ in ()).throw(
            AssertionError("null query must not run Hindsight recall")
        ),
    )

    assert not outcome.fall_back_to_current_query
    assert outcome.snapshot == RecallSnapshot(query="", results=())


def test_successful_new_query_with_zero_results_is_a_real_empty_snapshot(monkeypatch):
    monkeypatch.setattr(
        p5,
        "run_recall_preprocessor",
        lambda **_kwargs: p5.RecallPreprocessDecision(
            drop_old_refs=(),
            new_query="generated target",
        ),
    )

    outcome = p5.apply_recall_preprocessor(
        current_user_message="继续",
        previous_assistant_message="previous answer",
        previous_snapshot=RecallSnapshot(query="", results=()),
        recall_snapshot_for_query=lambda _query: RecallSnapshot(
            query="clipped generated target",
            results=(),
        ),
    )

    assert not outcome.fall_back_to_current_query
    assert outcome.snapshot == RecallSnapshot(
        query="clipped generated target",
        results=(),
    )
