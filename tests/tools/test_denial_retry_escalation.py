"""Behavior tests for repeat-to-human escalation after automated denial."""

from __future__ import annotations

import pytest

from fork_features.approval.policy import ApprovalPolicy
from tools import approval as A
from tools import approval_context


def _configure_smart_deny(monkeypatch, *, session_key: str, turn_id: str = "turn-1"):
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.setenv("HERMES_LANGUAGE", "zh")
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(
        A,
        "_smart_approve",
        lambda *_args, **_kwargs: A.SmartApprovalResult(
            "deny", "high", "none", "该操作存在真实风险且当前未获授权。"
        ),
    )
    monkeypatch.setattr(
        ApprovalPolicy,
        "repeat_manual_description",
        lambda _self, action, policy_reason, **_kwargs: (
            "目的：完成当前用户任务。\n"
            f"实际动作：仅执行一次：{action}\n"
            "预期影响：该操作会实际执行一次。\n"
            f"风险：{policy_reason}\n"
            "转人工原因：首次自动拒绝后重复了同一或高度相似操作。"
        ),
        raising=False,
    )
    monkeypatch.setattr(
        A,
        "detect_dangerous_command",
        lambda command: (True, "repeat-test-danger", f"risk:{command}"),
    )
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
        raising=False,
    )
    session_token = approval_context.set_current_session_key(session_key)
    context_tokens = approval_context.set_current_observability_context(turn_id=turn_id)
    return session_token, context_tokens


def _reset_context(session_token, context_tokens):
    approval_context.reset_current_observability_context(context_tokens)
    approval_context.reset_current_session_key(session_token)


def _register_resolver(
    session_key: str,
    result: str,
    captured: list[dict],
    *,
    reason: str | None = None,
):
    def cb(approval_data):
        captured.append(dict(approval_data))
        with A._lock:
            entries = A._gateway_queues.get(session_key, [])
            if entries:
                entries[-1].result = result
                entries[-1].reason = reason
                entries[-1].event.set()

    with A._lock:
        A._gateway_notify_cbs[session_key] = cb


def test_first_smart_deny_returns_single_repeat_path_without_prompt(monkeypatch):
    session_token, context_tokens = _configure_smart_deny(
        monkeypatch, session_key="repeat-first-deny"
    )
    try:
        result = A.check_all_command_guards("hermes gateway restart", "local")
    finally:
        _reset_context(session_token, context_tokens)

    assert result["approved"] is False
    assert result["outcome"] == "auto_denied"
    assert result["retry_escalation_available"] is True
    assert result.get("approval_pending") is not True
    assert result["message"] == (
        "智能审批已拒绝，该操作存在真实风险且当前未获授权，"
        "如该操作确实必要，保持原样重试一次"
        "（也不要额外添加任何注释），第二次将交由用户作决定。"
    )


def test_second_identical_denied_command_goes_directly_to_one_shot_human(monkeypatch):
    session_key = "repeat-second-identical"
    session_token, context_tokens = _configure_smart_deny(
        monkeypatch, session_key=session_key
    )
    captured: list[dict] = []
    try:
        first = A.check_all_command_guards("rm -rf /tmp/repeat-target", "local")
        _register_resolver(session_key, "once", captured)
        second = A.check_all_command_guards("rm -rf /tmp/repeat-target", "local")
    finally:
        with A._lock:
            A._gateway_notify_cbs.pop(session_key, None)
            A._gateway_queues.pop(session_key, None)
        _reset_context(session_token, context_tokens)

    assert first["outcome"] == "auto_denied"
    assert second["approved"] is True
    assert second["user_approved"] is True
    assert len(captured) == 1
    assert captured[0]["allow_session"] is False
    assert captured[0]["allow_permanent"] is False
    assert "该操作存在真实风险且当前未获授权" in captured[0]["description"]


def test_repeat_one_shot_cli_offers_only_once_or_deny(monkeypatch):
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    session_token = approval_context.set_current_session_key("repeat-cli-one-shot")
    captured: dict = {}

    def approval_callback(command, description, **kwargs):
        captured.update(command=command, description=description, **kwargs)
        return "once"

    try:
        result = A._run_approval_gate(
            pattern_key="smart_repeat:test",
            description="当前操作需要一次性人工确认",
            display_target="python current-action.py",
            approval_callback=approval_callback,
            cron_deny_message="blocked",
            single_query_deny_message="blocked",
            autoapprove_log_prefix="repeat-test",
            fail_closed_when_no_human=True,
            no_human_block_message="blocked",
            one_shot_only=True,
            denial_source_kind="shell",
        )
    finally:
        A.clear_session("repeat-cli-one-shot")
        approval_context.reset_current_session_key(session_token)

    assert result["approved"] is True
    assert captured["allow_permanent"] is False
    assert captured["smart_denied"] is True


@pytest.mark.parametrize("bypass", ["yolo", "mode_off", "permanent_allowlist"])
def test_terminal_repeat_respects_existing_legal_bypass(monkeypatch, bypass):
    session_key = f"terminal-repeat-{bypass}"
    session_token, context_tokens = _configure_smart_deny(
        monkeypatch, session_key=session_key
    )
    captured: list[dict] = []
    allowlisted = {"enabled": False}
    monkeypatch.setattr(
        A,
        "_command_matches_permanent_allowlist",
        lambda _command: allowlisted["enabled"],
    )
    command = "rm -rf /tmp/repeat-legal-bypass"
    try:
        first = A.check_all_command_guards(command, "local")
        _register_resolver(session_key, "once", captured)
        if bypass == "yolo":
            monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", True)
        elif bypass == "mode_off":
            monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "off")
        else:
            allowlisted["enabled"] = True
        second = A.check_all_command_guards(command, "local")
    finally:
        with A._lock:
            A._gateway_notify_cbs.pop(session_key, None)
            A._gateway_queues.pop(session_key, None)
        A.clear_session(session_key)
        _reset_context(session_token, context_tokens)

    assert first["outcome"] == "auto_denied"
    assert second["approved"] is True
    assert second.get("one_shot") is not True
    assert captured == []


@pytest.mark.parametrize("bypass", ["yolo", "mode_off", "session_cache"])
def test_execute_code_repeat_respects_existing_legal_bypass(monkeypatch, bypass):
    session_key = f"python-repeat-{bypass}"
    session_token, context_tokens = _configure_smart_deny(
        monkeypatch, session_key=session_key
    )
    captured: list[dict] = []
    code = "delete_target()\nnotify_user()"
    try:
        first = A.check_execute_code_guard(code, "local")
        _register_resolver(session_key, "once", captured)
        if bypass == "yolo":
            monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", True)
        elif bypass == "mode_off":
            monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "off")
        else:
            A.approve_session(session_key, "execute_code")
        second = A.check_execute_code_guard(code, "local")
    finally:
        with A._lock:
            A._gateway_notify_cbs.pop(session_key, None)
            A._gateway_queues.pop(session_key, None)
        A.clear_session(session_key)
        _reset_context(session_token, context_tokens)

    assert first["outcome"] == "auto_denied"
    assert second["approved"] is True
    assert second.get("one_shot") is not True
    assert captured == []


def test_second_highly_similar_long_command_goes_to_human(monkeypatch):
    session_key = "repeat-second-long-similar"
    session_token, context_tokens = _configure_smart_deny(
        monkeypatch, session_key=session_key
    )
    captured: list[dict] = []
    body = "\n".join(f"values[{index}] = {index}" for index in range(600))
    first_command = f"python - <<'PY'\n{body}\nprint('done')\nPY"
    second_command = first_command.replace(
        "values[350] = 350", "values[350]    =    350"
    )
    try:
        first = A.check_all_command_guards(first_command, "local")
        _register_resolver(session_key, "once", captured)
        second = A.check_all_command_guards(second_command, "local")
    finally:
        with A._lock:
            A._gateway_notify_cbs.pop(session_key, None)
            A._gateway_queues.pop(session_key, None)
        _reset_context(session_token, context_tokens)

    assert first["outcome"] == "auto_denied"
    assert second["approved"] is True
    assert len(captured) == 1


def test_long_command_literal_change_still_routes_to_one_shot_human(monkeypatch):
    session_key = "repeat-long-literal-change"
    session_token, context_tokens = _configure_smart_deny(
        monkeypatch, session_key=session_key
    )
    captured: list[dict] = []
    body = "\n".join(f"values[{index}] = {index}" for index in range(600))
    first_command = f"python - <<'PY'\n{body}\nprint('done')\nPY"
    second_command = first_command.replace("values[350] = 350", "values[350] = 351")
    try:
        first = A.check_all_command_guards(first_command, "local")
        _register_resolver(session_key, "once", captured)
        second = A.check_all_command_guards(second_command, "local")
    finally:
        with A._lock:
            A._gateway_notify_cbs.pop(session_key, None)
            A._gateway_queues.pop(session_key, None)
        _reset_context(session_token, context_tokens)

    assert first["outcome"] == "auto_denied"
    assert second["approved"] is True
    assert second["one_shot"] is True
    assert len(captured) == 1
    assert "values[350] = 351" in captured[0]["description"]


def test_changed_direct_script_path_still_routes_to_one_shot_human(monkeypatch):
    session_key = "repeat-changed-script-path"
    session_token, context_tokens = _configure_smart_deny(
        monkeypatch, session_key=session_key
    )
    captured: list[dict] = []
    payload = "x" * 1200
    first_command = f"python /tmp/project-a.py --payload {payload}"
    second_command = f"python /tmp/project-b.py --payload {payload}"
    try:
        first = A.check_all_command_guards(first_command, "local")
        _register_resolver(session_key, "once", captured)
        second = A.check_all_command_guards(second_command, "local")
    finally:
        with A._lock:
            A._gateway_notify_cbs.pop(session_key, None)
            A._gateway_queues.pop(session_key, None)
        _reset_context(session_token, context_tokens)

    assert first["outcome"] == "auto_denied"
    assert second["approved"] is True
    assert second["one_shot"] is True
    assert len(captured) == 1
    assert second_command in captured[0]["description"]


@pytest.mark.parametrize("second_decision", ["approve", "escalate"])
def test_second_similar_command_follows_current_smart_decision(
    monkeypatch,
    second_decision,
):
    session_key = f"repeat-second-model-{second_decision}"
    session_token, context_tokens = _configure_smart_deny(
        monkeypatch, session_key=session_key
    )
    decisions = iter(
        [
            A.SmartApprovalResult("deny", "high", "none", "首次拒绝原因。"),
            A.SmartApprovalResult(
                second_decision,
                "low",
                "sufficient" if second_decision == "approve" else "unclear",
                "第二次模型当前判决。",
            ),
        ]
    )
    monkeypatch.setattr(A, "_smart_approve", lambda *_args, **_kwargs: next(decisions))
    captured: list[dict] = []
    payload = "x" * 1200
    first_command = f"python /tmp/project-a.py --payload {payload}"
    second_command = f"python /tmp/project-b.py --payload {payload}"
    try:
        first = A.check_all_command_guards(first_command, "local")
        _register_resolver(session_key, "once", captured)
        second = A.check_all_command_guards(second_command, "local")
    finally:
        with A._lock:
            A._gateway_notify_cbs.pop(session_key, None)
            A._gateway_queues.pop(session_key, None)
        A.clear_session(session_key)
        _reset_context(session_token, context_tokens)

    assert first["outcome"] == "auto_denied"
    assert second["approved"] is True
    assert second.get("one_shot") is not True
    if second_decision == "approve":
        assert second["smart_approved"] is True
        assert captured == []
    else:
        assert second["user_approved"] is True
        assert len(captured) == 1
        assert captured[0]["allow_session"] is True


def test_second_similar_command_without_warning_uses_normal_approval_result(monkeypatch):
    session_key = "repeat-second-warning-disappears"
    session_token, context_tokens = _configure_smart_deny(
        monkeypatch, session_key=session_key
    )
    detections = iter(
        [
            (True, "repeat-test-danger", "首次 detector 风险。"),
            (False, None, None),
        ]
    )
    monkeypatch.setattr(A, "detect_dangerous_command", lambda _command: next(detections))
    decisions = iter(
        [
            A.SmartApprovalResult("deny", "high", "none", "首次拒绝原因。"),
            A.SmartApprovalResult("approve", "low", "sufficient", "第二次当前审查。"),
        ]
    )
    review_actions: list[str] = []

    def review(action, *_args, **_kwargs):
        review_actions.append(action)
        return next(decisions)

    monkeypatch.setattr(A, "_smart_approve", review)
    captured: list[dict] = []
    payload = "x" * 1200
    first_command = f"custom-op --target /tmp/a --payload {payload}"
    second_command = f"custom-op --target /tmp/b --payload {payload}"
    try:
        first = A.check_all_command_guards(first_command, "local")
        _register_resolver(session_key, "once", captured)
        second = A.check_all_command_guards(second_command, "local")
    finally:
        with A._lock:
            A._gateway_notify_cbs.pop(session_key, None)
            A._gateway_queues.pop(session_key, None)
        _reset_context(session_token, context_tokens)

    assert first["outcome"] == "auto_denied"
    assert second["approved"] is True
    assert second.get("one_shot") is not True
    assert review_actions == [first_command]
    assert captured == []


def test_first_execute_code_deny_uses_same_repeat_contract(monkeypatch):
    session_token, context_tokens = _configure_smart_deny(
        monkeypatch, session_key="repeat-first-execute-code"
    )
    try:
        result = A.check_execute_code_guard("print('danger')", "local")
    finally:
        _reset_context(session_token, context_tokens)

    assert result["approved"] is False
    assert result["outcome"] == "auto_denied"
    assert result["retry_escalation_available"] is True
    assert result.get("approval_pending") is not True
    assert result["message"] == (
        "智能审批已拒绝，该操作存在真实风险且当前未获授权，"
        "如该操作确实必要，保持原样重试一次"
        "（也不要额外添加任何注释），第二次将交由用户作决定。"
    )

def test_second_identical_execute_code_goes_to_one_shot_human(monkeypatch):
    session_key = "repeat-second-execute-code"
    session_token, context_tokens = _configure_smart_deny(
        monkeypatch, session_key=session_key
    )
    captured: list[dict] = []
    code = "from pathlib import Path\nPath('/tmp/repeat-code').unlink()"
    try:
        first = A.check_execute_code_guard(code, "local")
        _register_resolver(session_key, "once", captured)
        second = A.check_execute_code_guard(code, "local")
    finally:
        with A._lock:
            A._gateway_notify_cbs.pop(session_key, None)
            A._gateway_queues.pop(session_key, None)
        _reset_context(session_token, context_tokens)

    assert first["outcome"] == "auto_denied"
    assert second["approved"] is True
    assert second["user_approved"] is True
    assert second["one_shot"] is True
    assert len(captured) == 1
    assert captured[0]["allow_session"] is False
    assert captured[0]["allow_permanent"] is False


@pytest.mark.parametrize("second_decision", ["approve", "escalate"])
def test_execute_code_repeat_follows_current_smart_decision(
    monkeypatch,
    second_decision,
):
    session_key = f"repeat-python-{second_decision}"
    session_token, context_tokens = _configure_smart_deny(
        monkeypatch, session_key=session_key
    )
    decisions = iter(
        [
            A.SmartApprovalResult("deny", "high", "none", "首次 Python 拒绝。"),
            A.SmartApprovalResult(
                second_decision,
                "low",
                "sufficient" if second_decision == "approve" else "unclear",
                "第二次 Python 当前判决。",
            ),
        ]
    )
    monkeypatch.setattr(A, "_smart_approve", lambda *_args, **_kwargs: next(decisions))
    captured: list[dict] = []
    first_code = (
        "allowed = ready and configured\n"
        "if allowed:\n"
        "    delete_target()\n"
        "notify_user()"
    )
    second_code = first_code.replace("allowed = ready", "allowed = not ready")
    try:
        first = A.check_execute_code_guard(first_code, "local")
        _register_resolver(session_key, "once", captured)
        second = A.check_execute_code_guard(second_code, "local")
    finally:
        with A._lock:
            A._gateway_notify_cbs.pop(session_key, None)
            A._gateway_queues.pop(session_key, None)
        A.clear_session(session_key)
        _reset_context(session_token, context_tokens)

    assert first["outcome"] == "auto_denied"
    assert second["approved"] is True
    assert second.get("one_shot") is not True
    if second_decision == "approve":
        assert second["smart_approved"] is True
        assert captured == []
    else:
        assert second["user_approved"] is True
        assert len(captured) == 1
        assert captured[0]["allow_session"] is True


def test_execute_code_ordinary_escalate_deny_does_not_create_repeat_latch(monkeypatch):
    session_key = "python-manual-deny-keeps-baseline"
    session_token, context_tokens = _configure_smart_deny(
        monkeypatch, session_key=session_key
    )
    code = "delete_target()\nnotify_user()"
    monkeypatch.setattr(
        A,
        "_smart_approve",
        lambda *_args, **_kwargs: A.SmartApprovalResult(
            "escalate", "high", "none", "需要用户决定。"
        ),
    )
    captured: list[dict] = []
    try:
        _register_resolver(session_key, "deny", captured, reason="不要执行")
        denied = A.check_execute_code_guard(code, "local")
        monkeypatch.setattr(A, "is_current_session_yolo_enabled", lambda: True)
        retried = A.check_execute_code_guard(code, "local")
    finally:
        with A._lock:
            A._gateway_notify_cbs.pop(session_key, None)
            A._gateway_queues.pop(session_key, None)
        A.clear_session(session_key)
        _reset_context(session_token, context_tokens)

    assert denied["outcome"] == "denied"
    assert retried["approved"] is True
    assert retried.get("outcome") != "user_denied"


def test_user_denial_locks_same_operation_and_forbids_retries(monkeypatch):
    session_key = "repeat-user-denied"
    session_token, context_tokens = _configure_smart_deny(
        monkeypatch, session_key=session_key
    )
    captured: list[dict] = []
    command = "hermes gateway restart"
    try:
        first = A.check_all_command_guards(command, "local")
        _register_resolver(
            session_key,
            "deny",
            captured,
            reason="现在不允许重启 Hermes",
        )
        second = A.check_all_command_guards(command, "local")
        third = A.check_all_command_guards(command, "local")
    finally:
        with A._lock:
            A._gateway_notify_cbs.pop(session_key, None)
            A._gateway_queues.pop(session_key, None)
        _reset_context(session_token, context_tokens)

    assert first["outcome"] == "auto_denied"
    assert second["outcome"] == "denied"
    assert "用户不允许执行这项操作" in second["message"]
    assert "现在不允许重启 Hermes" in second["message"]
    assert third["outcome"] == "user_denied"
    assert "停止当前流程，或者在聊天中直接询问用户" in third["message"]
    assert "不要重试、改写、拆分或换用其他路径" in third["message"]
    assert len(captured) == 1


def test_second_retry_uses_approval_ai_generated_chinese_description(monkeypatch):
    session_key = "repeat-ai-description"
    session_token, context_tokens = _configure_smart_deny(
        monkeypatch, session_key=session_key
    )
    captured: list[dict] = []
    generated = (
        "目的：完成必要维护。\n"
        "实际动作：执行指定命令一次。\n"
        "预期影响：目标服务短暂重启。\n"
        "风险：当前会话可能中断。\n"
        "转人工原因：自动策略首次拒绝后，AI 判断仍有必要由用户决定。"
    )
    monkeypatch.setattr(
        ApprovalPolicy,
        "repeat_manual_description",
        lambda *_args, **_kwargs: generated,
    )
    try:
        A.check_all_command_guards("hermes gateway restart", "local")
        _register_resolver(session_key, "once", captured)
        result = A.check_all_command_guards("hermes gateway restart", "local")
    finally:
        with A._lock:
            A._gateway_notify_cbs.pop(session_key, None)
            A._gateway_queues.pop(session_key, None)
        _reset_context(session_token, context_tokens)

    assert result["approved"] is True
    assert captured[0]["description"] == generated


def test_user_timeout_locks_same_operation_without_second_card(monkeypatch):
    session_token, context_tokens = _configure_smart_deny(
        monkeypatch, session_key="repeat-user-timeout"
    )
    captured: list[dict] = []

    def timeout_decision(_session_key, _notify_cb, approval_data, **_kwargs):
        captured.append(dict(approval_data))
        return {"resolved": False, "choice": None, "reason": None}

    try:
        A.check_all_command_guards("rm -rf /tmp/timeout-target", "local")
        monkeypatch.setattr(A, "_await_gateway_decision", timeout_decision)
        A.register_gateway_notify("repeat-user-timeout", lambda _data: None)
        second = A.check_all_command_guards("rm -rf /tmp/timeout-target", "local")
        third = A.check_all_command_guards("rm -rf /tmp/timeout-target", "local")
    finally:
        with A._lock:
            A._gateway_notify_cbs.pop("repeat-user-timeout", None)
            A._gateway_queues.pop("repeat-user-timeout", None)
        _reset_context(session_token, context_tokens)

    assert second["outcome"] == "timeout"
    assert "用户不允许在未明确回复时执行这项操作" in second["message"]
    assert third["outcome"] == "user_denied"
    assert len(captured) == 1


def test_new_user_turn_does_not_inherit_previous_user_denial(monkeypatch):
    session_key = "repeat-new-turn"
    session_token, context_tokens = _configure_smart_deny(
        monkeypatch, session_key=session_key, turn_id="turn-old"
    )
    captured: list[dict] = []
    new_turn_tokens = None
    command = "rm -rf /tmp/new-turn-target"
    try:
        A.check_all_command_guards(command, "local")
        _register_resolver(session_key, "deny", captured)
        denied = A.check_all_command_guards(command, "local")
        approval_context.reset_current_observability_context(context_tokens)
        context_tokens = None
        new_turn_tokens = approval_context.set_current_observability_context(turn_id="turn-new")
        new_turn = A.check_all_command_guards(command, "local")
    finally:
        with A._lock:
            A._gateway_notify_cbs.pop(session_key, None)
            A._gateway_queues.pop(session_key, None)
        if new_turn_tokens is not None:
            approval_context.reset_current_observability_context(new_turn_tokens)
        if context_tokens is not None:
            approval_context.reset_current_observability_context(context_tokens)
        approval_context.reset_current_session_key(session_token)

    assert denied["outcome"] == "denied"
    assert new_turn["outcome"] == "auto_denied"
    assert new_turn["retry_escalation_available"] is True
    assert len(captured) == 1


def test_human_approval_is_one_shot_even_if_client_returns_session(monkeypatch):
    session_key = "repeat-one-shot"
    session_token, context_tokens = _configure_smart_deny(
        monkeypatch, session_key=session_key
    )
    captured: list[dict] = []
    command = "rm -rf /tmp/one-shot-target"
    try:
        A.check_all_command_guards(command, "local")
        _register_resolver(session_key, "session", captured)
        approved = A.check_all_command_guards(command, "local")
        third = A.check_all_command_guards(command, "local")
    finally:
        with A._lock:
            A._gateway_notify_cbs.pop(session_key, None)
            A._gateway_queues.pop(session_key, None)
        _reset_context(session_token, context_tokens)

    assert approved["approved"] is True
    assert approved["one_shot"] is True
    assert third["outcome"] == "auto_denied"
    assert len(captured) == 1


def test_repeat_without_live_callback_fails_closed_without_pending(monkeypatch):
    session_key = "repeat-no-live-callback"
    session_token, context_tokens = _configure_smart_deny(
        monkeypatch, session_key=session_key
    )
    command = "rm -rf /tmp/no-live-callback-target"
    captured: list[dict] = []
    try:
        first = A.check_all_command_guards(command, "local")
        second = A.check_all_command_guards(command, "local")
        with A._lock:
            pending = A._pending.get(session_key)
        _register_resolver(session_key, "once", captured)
        third = A.check_all_command_guards(command, "local")
    finally:
        A.clear_session(session_key)
        _reset_context(session_token, context_tokens)

    assert first["outcome"] == "auto_denied"
    assert second["approved"] is False
    assert second["outcome"] == "approval_unavailable"
    assert second["status"] == "blocked"
    assert second["one_shot"] is True
    assert pending is None
    assert third["approved"] is True
    assert third["one_shot"] is True
    assert len(captured) == 1


def test_no_turn_id_disables_same_turn_denial_state(monkeypatch):
    session_token = approval_context.set_current_session_key("missing-turn-id")
    try:
        policy = A._fork_approval_policy()
        first = policy.first_denial(
            "rm -rf /tmp/no-turn",
            "test denial",
            source_kind="shell",
        )
        policy.record_user_denial(
            "rm -rf /tmp/no-turn",
            source_kind="shell",
        )

        assert first["retry_escalation_available"] is False
        assert policy.consume_similar_denial(
            "rm -rf /tmp/no-turn", source_kind="shell"
        ) is None
        assert policy.latched_user_denial(
            "rm -rf /tmp/no-turn", source_kind="shell"
        ) is None
    finally:
        approval_context.reset_current_session_key(session_token)


def test_cron_policy_block_never_enters_repeat_escalation(monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(
        A.approval_context, "_get_cron_approval_mode", lambda: "deny"
    )
    monkeypatch.setattr(
        A,
        "detect_dangerous_command",
        lambda command: (True, "cron-danger", f"risk:{command}"),
    )
    session_token = approval_context.set_current_session_key("repeat-cron")
    context_tokens = approval_context.set_current_observability_context(turn_id="turn-cron")
    try:
        first = A.check_all_command_guards("rm -rf /tmp/cron-target", "local")
        second = A.check_all_command_guards("rm -rf /tmp/cron-target", "local")
        retry_state = A._fork_approval_policy().consume_similar_denial(
            "rm -rf /tmp/cron-target",
            source_kind="shell",
        )
    finally:
        _reset_context(session_token, context_tokens)

    assert first["approved"] is False
    assert second["approved"] is False
    assert first.get("retry_escalation_available") is not True
    assert second.get("retry_escalation_available") is not True
    assert retry_state is None


def test_ordinary_smart_escalate_deny_does_not_create_repeat_latch(monkeypatch):
    session_key = "manual-deny-keeps-baseline"
    session_token, context_tokens = _configure_smart_deny(
        monkeypatch, session_key=session_key
    )
    command = "rm -rf /tmp/manual-deny-target"
    monkeypatch.setattr(
        A,
        "_smart_approve",
        lambda *_args, **_kwargs: A.SmartApprovalResult(
            "escalate", "high", "none", "需要用户决定。"
        ),
    )
    captured = []
    try:
        _register_resolver(session_key, "deny", captured, reason="不要执行")
        denied = A.check_all_command_guards(command, "local")
        monkeypatch.setattr(A, "is_current_session_yolo_enabled", lambda: True)
        retried = A.check_all_command_guards(command, "local")
    finally:
        with A._lock:
            A._gateway_notify_cbs.pop(session_key, None)
            A._gateway_queues.pop(session_key, None)
        _reset_context(session_token, context_tokens)

    assert denied["outcome"] == "denied"
    assert retried["approved"] is True
    assert retried.get("outcome") != "user_denied"


def test_selected_transport_deny_does_not_create_repeat_latch(monkeypatch):
    session_key = "transport-deny-keeps-baseline"
    session_token, context_tokens = _configure_smart_deny(
        monkeypatch, session_key=session_key
    )
    command = "rm -rf /tmp/transport-deny-target"
    monkeypatch.setattr(
        A,
        "_smart_approve",
        lambda *_args, **_kwargs: A.SmartApprovalResult(
            "escalate", "high", "none", "需要用户决定。"
        ),
    )
    monkeypatch.setattr(
        A,
        "_present_with_selected_transport",
        lambda **_kwargs: {"selected": True, "choice": "deny"},
    )
    try:
        denied = A.check_all_command_guards(command, "local")
        monkeypatch.setattr(A, "is_current_session_yolo_enabled", lambda: True)
        retried = A.check_all_command_guards(command, "local")
    finally:
        _reset_context(session_token, context_tokens)

    assert denied["outcome"] == "denied"
    assert retried["approved"] is True
    assert retried.get("outcome") != "user_denied"
