"""Behavior tests for the Fork-owned Smart Approval retry policy."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from fork_features.approval import retry_policy


@pytest.fixture(autouse=True)
def _clear_retry_state():
    retry_policy.automated_denials.clear()
    retry_policy.user_denials.clear()
    yield
    retry_policy.automated_denials.clear()
    retry_policy.user_denials.clear()


def _record(
    key: tuple[str, str],
    action: str,
    description: str,
    *,
    source_kind: str = "shell",
    lock: threading.Lock,
):
    return retry_policy.first_automated_denial_result(
        key,
        action,
        description,
        source_kind=source_kind,
        prefers_chinese=True,
        lock=lock,
        max_entries=256,
    )


def test_plain_text_similarity_normalizes_whitespace_and_rejects_unrelated_text():
    assert retry_policy.actions_are_similar(
        "python script.py --mode safe",
        "python   script.py\n--mode\tsafe",
        source_kind="shell",
    )
    assert retry_policy.actions_are_similar(
        "python /tmp/project-a.py --payload " + "x" * 1200,
        "python /tmp/project-b.py --payload " + "x" * 1200,
        source_kind="shell",
    )
    assert not retry_policy.actions_are_similar(
        "python /tmp/project-a.py --payload " + "x" * 1200,
        "curl https://example.test/status --fail",
        source_kind="shell",
    )


def test_semantic_reversal_is_only_a_similarity_route():
    previous = (
        "allowed = ready and configured\n"
        "if allowed:\n"
        "    delete_target()\n"
        "notify_user()"
    )
    current = previous.replace("allowed = ready", "allowed = not ready")

    assert retry_policy.actions_are_similar(
        previous,
        current,
        source_kind="python",
    )


def test_similarity_work_is_bounded_to_head_and_tail():
    previous = "a" * 5_000 + "old-center" + "z" * 5_000
    current = "a" * 5_000 + "new-center" + "z" * 5_000

    assert retry_policy.actions_are_similar(
        previous,
        current,
        source_kind="python",
    )


def test_consuming_match_returns_state_and_preserves_unrelated_action():
    lock = threading.Lock()
    key = ("session-a", "turn-1")
    action_a = "python /tmp/action-a.py"
    action_b = "curl https://example.test/status --fail"
    _record(key, action_a, "risk-a", lock=lock)
    _record(key, action_b, "risk-b", lock=lock)

    matched_a = retry_policy.consume_similar_automated_denial(
        key,
        action_a,
        source_kind="shell",
        lock=lock,
    )

    assert matched_a is not None
    assert matched_a.action == action_a
    assert matched_a.description == "risk-a"
    assert [state.action for state in retry_policy.automated_denials[key]] == [action_b]
    assert retry_policy.consume_similar_automated_denial(
        key,
        action_a,
        source_kind="shell",
        lock=lock,
    ) is None


def test_turn_and_tool_kind_isolate_retry_candidates():
    lock = threading.Lock()
    key = ("session-a", "turn-1")
    action = "python /tmp/action.py"
    _record(key, action, "risk", source_kind="shell", lock=lock)

    assert retry_policy.consume_similar_automated_denial(
        ("session-a", "turn-2"),
        action,
        source_kind="shell",
        lock=lock,
    ) is None
    assert retry_policy.consume_similar_automated_denial(
        key,
        action,
        source_kind="python",
        lock=lock,
    ) is None
    assert retry_policy.consume_similar_automated_denial(
        key,
        action,
        source_kind="shell",
        lock=lock,
    ) is not None


def test_repeat_card_user_denial_latches_reason_for_same_turn():
    lock = threading.Lock()
    key = ("session-a", "turn-1")
    action = "rm -rf /tmp/target"
    _record(key, action, "risk", lock=lock)

    retry_policy.record_user_denial(
        key,
        action,
        source_kind="shell",
        reason="暂不允许",
        lock=lock,
        max_entries=256,
    )
    result = retry_policy.latched_user_denial_result(
        key,
        action,
        source_kind="shell",
        prefers_chinese=True,
        lock=lock,
    )

    assert result is not None
    assert result["outcome"] == "user_denied"
    assert result["deny_reason"] == "暂不允许"
    assert key not in retry_policy.automated_denials


def test_clear_session_removes_only_that_sessions_retry_state():
    lock = threading.Lock()
    _record(("session-a", "turn-1"), "python a.py", "risk-a", lock=lock)
    _record(("session-b", "turn-1"), "python b.py", "risk-b", lock=lock)
    retry_policy.record_user_denial(
        ("session-a", "turn-2"),
        "python c.py",
        source_kind="shell",
        reason="deny-c",
        lock=lock,
        max_entries=256,
    )

    retry_policy.clear_session("session-a", lock=lock)

    assert not any(key[0] == "session-a" for key in retry_policy.automated_denials)
    assert not any(key[0] == "session-a" for key in retry_policy.user_denials)
    assert ("session-b", "turn-1") in retry_policy.automated_denials


def test_repeat_description_is_for_current_action_and_one_execution():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=(
                        "目的：完成当前任务。\n"
                        "实际动作：执行 project-b.py 一次。\n"
                        "预期影响：目标文件会更新。\n"
                        "风险：会产生实际写入。\n"
                        "转人工原因：相似操作首次被自动拒绝。"
                    )
                )
            )
        ]
    )

    description = retry_policy.generate_repeat_manual_description(
        "python /tmp/project-b.py",
        "会产生实际写入",
        latest_user_message="继续执行当前任务",
        redact_action=lambda action: action,
        call_llm=lambda **_kwargs: response,
        source_kind="shell",
    )

    assert "project-b.py" in description
    assert "一次" in description
    assert "转人工原因" in description


def test_repeat_description_forwards_complete_latest_user_message():
    captured = {}
    marker = "完整用户消息末尾条件"
    latest_user_message = "前置内容" * 3_000 + marker
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=(
                        "目的：完成当前任务。\n"
                        "实际动作：执行 project-b.py 一次。\n"
                        "预期影响：目标文件会更新。\n"
                        "风险：会产生实际写入。\n"
                        "转人工原因：相似操作首次被自动拒绝。"
                    )
                )
            )
        ]
    )

    def call_llm(**kwargs):
        captured.update(kwargs)
        return response

    retry_policy.generate_repeat_manual_description(
        "python /tmp/project-b.py",
        "会产生实际写入",
        latest_user_message=latest_user_message,
        redact_action=lambda action: action,
        call_llm=call_llm,
        source_kind="shell",
    )

    user_prompt = captured["messages"][1]["content"]
    assert marker in user_prompt
