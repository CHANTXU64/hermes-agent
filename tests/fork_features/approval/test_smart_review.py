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


def _review(
    tmp_path,
    *,
    command="python entry.py",
    response,
    context=None,
    call_log=None,
    operator_policy="",
):
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
        operator_policy=operator_policy,
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


def test_review_forwards_complete_latest_user_message_and_all_clarifications(tmp_path):
    entry = tmp_path / "entry.py"
    entry.write_text("print('current entry')\n", encoding="utf-8")
    captured = {}
    marker = "完整用户消息末尾授权"
    latest_user_message = "前置内容" * 4_000 + marker
    clarifications = [
        {"question": f"问题{i}", "answer": f"回答{i}"}
        for i in range(12)
    ]

    _review(
        tmp_path,
        response=_response(
            '{"decision":"approve","risk_level":"low",'
            '"authorization":"exact","reason":"用户已完整授权。"}'
        ),
        context={
            "latest_user_message": latest_user_message,
            "clarifications": clarifications,
        },
        call_log=captured,
    )

    user_prompt = captured["messages"][1]["content"]
    assert marker in user_prompt
    assert "问题11" in user_prompt
    assert "回答11" in user_prompt


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


def test_prompt_infers_risk_from_visible_metadata_when_source_is_incomplete(tmp_path):
    captured = {}

    _review(
        tmp_path,
        command="python missing.py --send-final /tmp/run --recipient fixed@example.test",
        response=_response(
            '{"decision":"approve","risk_level":"low",'
            '"authorization":"sufficient","reason":"可见参数可判断。"}'
        ),
        call_log=captured,
    )

    system_prompt = captured["messages"][0]["content"]
    assert "Do not deny or escalate solely" in system_prompt
    assert "file path" in system_prompt
    assert "filename" in system_prompt
    assert "flags" in system_prompt
    assert "argument names and values" in system_prompt
    assert "working directory" in system_prompt


def test_baseline_safe_local_state_write_does_not_require_authorization(tmp_path):
    required_contract = (
        "Risk first, authorization second.",
        "Local state, cursor/progress, and ordinary output-file writes",
        "must contribute zero risk by itself",
        "must not be combined with absent authorization",
        "not a task-alignment, relevance, or per-tool permission check",
    )

    def call_llm(**kwargs):
        system_prompt = kwargs["messages"][0]["content"]
        if all(clause in system_prompt for clause in required_contract):
            return _response(
                '{"decision":"approve","risk_level":"low",'
                '"authorization":"sufficient","reason":"普通本地状态更新无需逐项授权。"}'
            )
        return _response(
            '{"decision":"escalate","risk_level":"medium",'
            '"authorization":"none","reason":"没有明确授权写入状态。"}'
        )

    result = review_action(
        "python update_cursor.py --state /tmp/state.json --output /tmp/result.json",
        "direct script execution",
        approval_context={"latest_user_message": "", "clarifications": []},
        cwd=str(tmp_path),
        source_kind="shell",
        read_script=None,
        interface_language="zh",
        operator_policy="",
        strip_shell_comments=lambda value: value,
        call_llm=call_llm,
        script_evidence=[
            {
                "path": str(tmp_path / "update_cursor.py"),
                "status": "skipped_git_tracked",
                "content": "",
            }
        ],
    )

    assert result == SmartApprovalResult(
        "approve",
        "low",
        "sufficient",
        "普通本地状态更新无需逐项授权。",
    )


def test_ordinary_api_use_does_not_need_per_request_authorization(tmp_path):
    captured = {}
    _review(
        tmp_path,
        command="python probe.py --url https://api.vendor.example/v1/models --api-key-env VENDOR_API_KEY",
        response=_response(
            '{"decision":"approve","risk_level":"low",'
            '"authorization":"sufficient","reason":"正常 API 使用。"}'
        ),
        call_log=captured,
    )

    system_prompt = captured["messages"][0]["content"]
    assert "Ordinary intended API authentication to its named service" in system_prompt
    assert "straightforward read-only external requests" in system_prompt
    assert "normal model or API usage and its expected cost" in system_prompt
    assert "Direct credential use by a script does not establish a hazard by itself" in system_prompt


def test_operator_trusted_destination_is_not_credential_exposure(tmp_path):
    trusted_policy = "Allow credentials and sensitive data to *.owned.example."
    captured = {}
    _review(
        tmp_path,
        command="python probe.py --url https://model.owned.example/v1 --api-key-env MODEL_API_KEY",
        response=_response(
            '{"decision":"approve","risk_level":"low",'
            '"authorization":"sufficient","reason":"目标主机已被操作者信任。"}'
        ),
        operator_policy=trusted_policy,
        call_log=captured,
    )

    system_prompt = captured["messages"][0]["content"]
    assert trusted_policy in system_prompt
    assert "An operator policy that explicitly trusts a destination is authoritative" in system_prompt
    assert "normal credential authentication" in system_prompt
    assert "matches the credential and intended service" in system_prompt


def test_operator_destination_trust_does_not_cover_mismatched_or_non_auth_transfer(tmp_path):
    captured = {}
    _review(
        tmp_path,
        command=(
            "python upload.py --url https://model.owned.example/upload "
            "--api-key-env UNRELATED_API_KEY"
        ),
        response=_response(
            '{"decision":"escalate","risk_level":"high",'
            '"authorization":"none","reason":"凭据与目标服务不匹配。"}'
        ),
        operator_policy="Trust model.owned.example as the model service.",
        call_log=captured,
    )

    system_prompt = captured["messages"][0]["content"]
    assert "does not automatically authorize a mismatched credential" in system_prompt
    assert "Different provider identities in the credential and destination are visible mismatch evidence" in system_prompt
    assert "even when a flag labels the credential as an API key or authentication input" in system_prompt
    assert "non-authentication transfer" in system_prompt
    assert "unless the operator policy explicitly allows that transfer" in system_prompt


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


def test_safe_assessment_does_not_require_task_authorization(tmp_path):
    import json

    for decision in ("approve", "escalate"):
        for authorization in ("none", "unclear", "sufficient"):
            result = _review(
                tmp_path,
                command="python update_progress.py",
                context={"latest_user_message": "", "clarifications": []},
                response=_response(json.dumps({
                    "decision": decision, "risk_level": "low",
                    "authorization": authorization, "reason": "普通本地进度写入，未见具体危险。",
                    "risk_evidence": "", "prohibition": "",
                })),
            )
            assert result.decision == "approve", (decision, authorization, result)
            assert result.risk_level == "low"
            assert result.authorization == "sufficient"
            assert result.risk_evidence == ""
            assert result.prohibition == ""


def test_risky_actions_and_explicit_prohibitions_keep_their_gate(tmp_path):
    import json

    cases = [
        ("approve", "low", "exact", "", "用户说‘只查，不要修改’，当前命令修改文件。", "deny"),
        ("approve", "critical", "exact", "rm -rf / 删除系统", "", "deny"),
        ("approve", "medium", "none", "send_mail 向外部收件人发送内容", "", "escalate"),
        ("escalate", "medium", "exact", "send_mail 向外部收件人发送内容", "", "approve"),
        ("approve", "medium", "exact", "", "", "escalate"),
        ("approve", "low", "sufficient", "删除重要数据", "", "escalate"),
    ]
    for decision, risk, authorization, evidence, prohibition, expected in cases:
        result = _review(tmp_path, response=_response(json.dumps({
            "decision": decision, "risk_level": risk, "authorization": authorization,
            "reason": prohibition or evidence or "风险结论缺乏证据。",
            "risk_evidence": evidence, "prohibition": prohibition,
        })))
        assert result.decision == expected, (decision, risk, authorization, result)


def test_real_guard_paths_use_separate_safety_findings(tmp_path, monkeypatch):
    import json
    from tools import approval, approval_context
    from fork_features.approval.policy import set_smart_approval_context, reset_smart_approval_context

    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_SINGLE_QUERY_SESSION", raising=False)
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval_context, "_get_approval_config", lambda: {"mode": "smart"})
    monkeypatch.setattr("tools.tirith_security.check_command_security",
                        lambda _: {"action": "allow", "findings": [], "summary": ""})
    calls = []
    response = {}

    def model(**kwargs):
        calls.append(kwargs)
        return _response(json.dumps(response))

    monkeypatch.setattr("fork_features.approval.runtime.call_approval_llm", model)
    token = set_smart_approval_context({"latest_user_message": "", "clarifications": []})
    try:
        for guard, command in [
            (approval.check_all_command_guards, "python -c \"print('hello')\""),
            (approval.check_execute_code_guard, "print('hello')"),
        ]:
            for prohibition in ("", "用户明确禁止执行该操作。"):
                key = f"safety-findings-{guard.__name__}-{bool(prohibition)}"
                approval.clear_session(key)
                monkeypatch.setenv("HERMES_SESSION_KEY", key)
                response.update(decision="approve", risk_level="low", authorization="none",
                                reason="普通操作。", risk_evidence="", prohibition=prohibition)
                before = len(calls)
                result = guard(command, "local")
                assert result["approved"] is (not prohibition)
                assert len(calls) == before + 1
                approval.clear_session(key)
    finally:
        reset_smart_approval_context(token)
