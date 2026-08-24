"""Behavior contracts for the Fork-owned Smart Approval reviewer."""

from __future__ import annotations

from types import SimpleNamespace

from fork_features.approval.smart_review import (
    SmartApprovalResult,
    review_action,
)


def _response(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _review(tmp_path, *, command="python entry.py", response, context=None, call_log=None):
    def call_llm(**kwargs):
        if call_log is not None:
            call_log.update(kwargs)
        return response

    return review_action(
        command,
        "direct script execution",
        approval_context=context or {"latest_user_message": "查看状态", "clarifications": []},
        cwd=str(tmp_path),
        source_kind="shell",
        read_script=None,
        interface_language="zh",
        operator_policy="",
        strip_shell_comments=lambda value: value,
        call_llm=call_llm,
    )


def test_review_returns_structured_result_and_uses_bounded_current_evidence(tmp_path):
    entry = tmp_path / "entry.py"
    entry.write_text("print('current entry')\n", encoding="utf-8")
    captured = {}

    result = _review(
        tmp_path,
        response=_response(
            '{"decision":"approve","risk_level":"low",'
            '"authorization":"sufficient","reason":"安全读取。"}'
        ),
        context={
            "latest_user_message": "查看当前状态",
            "clarifications": [{"question": "只读？", "answer": "是"}],
            "summary": "不得进入审批证据的内部摘要",
        },
        call_log=captured,
    )

    assert result == SmartApprovalResult("approve", "low", "sufficient", "安全读取。")
    user_prompt = captured["messages"][1]["content"]
    assert "查看当前状态" in user_prompt
    assert "只读？" in user_prompt
    assert "print('current entry')" in user_prompt
    assert "不得进入审批证据的内部摘要" not in user_prompt


def test_unreadable_direct_entry_does_not_override_safe_model_decision(tmp_path):
    result = _review(
        tmp_path,
        command="python missing.py",
        response=_response(
            '{"decision":"approve","risk_level":"medium",'
            '"authorization":"sufficient","reason":"可见操作属于正常步骤。"}'
        ),
    )

    assert result.decision == "approve"
    assert result.authorization == "sufficient"


def test_invalid_response_escalates_with_chinese_reason(tmp_path):
    result = _review(tmp_path, response=_response("not-json"))

    assert result.decision == "escalate"
    assert result.risk_level == "high"
    assert "格式无效" in result.reason


def test_critical_model_approval_is_forced_to_deny(tmp_path):
    result = _review(
        tmp_path,
        response=_response(
            '{"decision":"approve","risk_level":"critical",'
            '"authorization":"exact","reason":"严重破坏操作。"}'
        ),
    )

    assert result == SmartApprovalResult("deny", "critical", "exact", "严重破坏操作。")
