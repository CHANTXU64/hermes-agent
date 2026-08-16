"""Contract tests for context-aware structured smart approval."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.tool_executor import build_smart_approval_context
from tools.todo_tool import TODO_INJECTION_HEADER
from tools.approval import (
    SmartApprovalResult,
    _collect_direct_script_evidence,
    _smart_approve,
    check_all_command_guards,
    clear_session,
    get_smart_approval_context,
    reset_smart_approval_context,
    set_smart_approval_context,
)


def _assistant_tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def test_context_uses_only_latest_user_message_and_following_clarify_pair():
    messages = [
        {"role": "user", "content": "删除 /data/old"},
        {"role": "assistant", "content": "旧轮次"},
        {"role": "user", "content": "先确认后再删除 /data/cache-a"},
        _assistant_tool_call(
            "clarify-1",
            "clarify",
            {"question": "是否删除 /data/cache-a？", "choices": ["删除", "取消"]},
        ),
        {
            "role": "tool",
            "tool_call_id": "clarify-1",
            "content": json.dumps(
                {
                    "user_response": "删除",
                    "response_context": "The user selected: 删除",
                },
                ensure_ascii=False,
            ),
        },
    ]

    context = build_smart_approval_context(messages)

    assert context == {
        "latest_user_message": "先确认后再删除 /data/cache-a",
        "clarifications": [
            {"question": "是否删除 /data/cache-a？", "answer": "删除"}
        ],
    }
    assert "/data/old" not in json.dumps(context, ensure_ascii=False)


def test_context_ignores_synthetic_user_runtime_scaffolding():
    messages = [
        {"role": "user", "content": "检查当前容器状态"},
        {
            "role": "user",
            "content": "[Session Arc Summary (d1, node 11)]\n历史任务摘要",
        },
        {
            "role": "user",
            "content": f"{TODO_INJECTION_HEADER}\n- [>] 继续旧任务",
            "_todo_snapshot_synthetic": True,
        },
        {
            "role": "user",
            "content": (
                "[IMPORTANT: Background process proc-test completed normally "
                "(exit code 0).\nCommand: pytest\nOutput:\n42 passed]"
            ),
        },
    ]

    context = build_smart_approval_context(messages)

    assert context == {
        "latest_user_message": "检查当前容器状态",
        "clarifications": [],
    }


def test_context_strips_todo_snapshot_appended_to_real_user_message():
    messages = [
        {
            "role": "user",
            "content": (
                "检查当前容器状态\n\n"
                f"{TODO_INJECTION_HEADER}\n- [>] 继续旧任务"
            ),
        }
    ]

    context = build_smart_approval_context(messages)

    assert context == {
        "latest_user_message": "检查当前容器状态",
        "clarifications": [],
    }


def test_context_ignores_bare_skill_scaffold_after_real_user_message():
    messages = [
        {"role": "user", "content": "运行一次 Hermes 备份"},
        {
            "role": "user",
            "content": (
                '[IMPORTANT: The user has invoked the "hermes-backup" skill, '
                "indicating they want you to follow its instructions. The full "
                "skill content is loaded below.]\n\n---\nname: hermes-backup\n---"
            ),
        },
    ]

    context = build_smart_approval_context(messages)

    assert context["latest_user_message"] == "运行一次 Hermes 备份"


def test_context_extracts_user_instruction_from_skill_scaffold():
    messages = [
        {
            "role": "user",
            "content": (
                '[IMPORTANT: The user has invoked the "hermes-backup" skill, '
                "indicating they want you to follow its instructions. The full "
                "skill content is loaded below.]\n\n# Skill body\n\n"
                "The user has provided the following instruction alongside the "
                "skill invocation: 立即备份当前配置"
            ),
        }
    ]

    context = build_smart_approval_context(messages)

    assert context["latest_user_message"] == "立即备份当前配置"


def test_context_strips_model_switch_and_thread_reply_runtime_preludes():
    messages = [
        {
            "role": "user",
            "content": (
                "[Note: model was just switched from old-model to new-model via "
                "OpenAI Codex. Adjust your self-identification accordingly.]\n\n"
                '[Replying to: "你好"]\n\n'
                "[Thread context — prior messages in this thread (not yet in "
                "conversation history):]\n[thread parent]\n你好\n"
                "[End of thread context]\n\n/usage"
            ),
        }
    ]

    context = build_smart_approval_context(messages)

    assert context["latest_user_message"] == "/usage"


def test_context_strips_cron_delivery_and_script_output_preludes():
    messages = [
        {
            "role": "user",
            "content": (
                "[IMPORTANT: You are running as a scheduled cron job. DELIVERY: "
                "Your final response will be automatically delivered to the user.]\n\n"
                "## Script Output\nThe following data was collected by a pre-run "
                "script. Use it as context for your analysis.\n\n"
                "```\n{\"dangerous_looking_data\": \"rm -rf /\"}\n```\n\n"
                "只读检查服务状态并报告"
            ),
        }
    ]

    context = build_smart_approval_context(messages)

    assert context["latest_user_message"] == "只读检查服务状态并报告"


def test_clarify_timeout_is_preserved_as_non_authorizing_evidence():
    messages = [
        {"role": "user", "content": "清理缓存"},
        _assistant_tool_call("clarify-2", "clarify", {"question": "是否删除全部缓存？"}),
        {
            "role": "tool",
            "tool_call_id": "clarify-2",
            "content": json.dumps(
                {"user_response": "[user did not respond within 5m]"},
                ensure_ascii=False,
            ),
        },
    ]

    assert build_smart_approval_context(messages)["clarifications"] == [
        {
            "question": "是否删除全部缓存？",
            "answer": "[user did not respond within 5m]",
        }
    ]


def test_direct_shell_script_evidence_reads_python_and_bash_entries(tmp_path: Path):
    py_script = tmp_path / "cleanup.py"
    sh_script = tmp_path / "deploy.sh"
    py_script.write_text("from pathlib import Path\nPath('/tmp/a').unlink()\n")
    sh_script.write_text("#!/bin/sh\nrm -rf /tmp/build-cache\n")

    evidence = _collect_direct_script_evidence(
        "python cleanup.py && bash ./deploy.sh",
        cwd=str(tmp_path),
        source_kind="shell",
    )

    assert [(item["path"], item["status"]) for item in evidence] == [
        (str(py_script), "read"),
        (str(sh_script), "read"),
    ]
    assert "unlink" in evidence[0]["content"]
    assert "rm -rf" in evidence[1]["content"]


def test_virtualenv_console_entrypoint_is_not_custom_script_evidence(tmp_path: Path):
    venv_root = tmp_path / "venv"
    bin_dir = venv_root / "bin"
    bin_dir.mkdir(parents=True)
    (venv_root / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    ruff = bin_dir / "ruff"
    ruff.write_bytes(b"\xcf\xfa\xed\xfe\x00compiled-tool")
    ruff.chmod(0o755)

    evidence = _collect_direct_script_evidence(
        "./venv/bin/ruff check tools/approval.py",
        cwd=str(tmp_path),
        source_kind="shell",
    )

    assert evidence == []


def test_standard_development_commands_bypass_smart_review_when_baseline_safe(
    tmp_path: Path,
):
    venv_root = tmp_path / "venv"
    bin_dir = venv_root / "bin"
    bin_dir.mkdir(parents=True)
    (venv_root / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    ruff = bin_dir / "ruff"
    ruff.write_bytes(b"\xcf\xfa\xed\xfe\x00compiled-tool")
    ruff.chmod(0o755)

    with patch("tools.approval._get_approval_mode", return_value="smart"), patch(
        "tools.tirith_security.check_command_security",
        return_value={"action": "allow", "findings": [], "summary": ""},
    ), patch(
        "tools.approval.detect_dangerous_command",
        return_value=(False, None, None),
    ), patch("tools.approval._invoke_smart_approve") as smart_review:
        pytest_result = check_all_command_guards(
            "python -m pytest -q tests/tools/test_tirith_security.py",
            "local",
            cwd=str(tmp_path),
        )
        ruff_result = check_all_command_guards(
            "./venv/bin/ruff check tools/approval.py",
            "local",
            cwd=str(tmp_path),
        )

    assert pytest_result == {"approved": True, "message": None}
    assert ruff_result == {"approved": True, "message": None}
    smart_review.assert_not_called()


def test_smart_review_display_uses_latest_user_language():
    from tools.approval import _format_smart_review_description

    review = SmartApprovalResult(
        decision="escalate",
        risk_level="medium",
        authorization="unclear",
        reason="需要人工确认此测试脚本的写入范围。",
    )
    chinese_token = set_smart_approval_context(
        {"latest_user_message": "运行测试并验证", "clarifications": []}
    )
    try:
        chinese = _format_smart_review_description(review)
    finally:
        reset_smart_approval_context(chinese_token)

    english_token = set_smart_approval_context(
        {"latest_user_message": "Run the tests", "clarifications": []}
    )
    try:
        english = _format_smart_review_description(review)
    finally:
        reset_smart_approval_context(english_token)

    assert chinese == (
        "智能审批：风险等级=中，授权状态=不明确。"
        "需要人工确认此测试脚本的写入范围。"
    )
    assert english == (
        "Smart review: risk=medium, authorization=unclear. "
        "需要人工确认此测试脚本的写入范围。"
    )


def test_user_denial_message_uses_latest_user_language():
    from tools.approval import _format_user_denial_message

    chinese_token = set_smart_approval_context(
        {"latest_user_message": "请先让我确认", "clarifications": []}
    )
    try:
        chinese = _format_user_denial_message("denied", "范围不对")
    finally:
        reset_smart_approval_context(chinese_token)

    english_token = set_smart_approval_context(
        {"latest_user_message": "Ask me first", "clarifications": []}
    )
    try:
        english = _format_user_denial_message("denied", "wrong scope")
    finally:
        reset_smart_approval_context(english_token)

    assert chinese.startswith("已阻止：用户拒绝了该命令。")
    assert "用户给出的原因：“范围不对”" in chinese
    assert "不要重试、改写命令或通过其他路径实现相同结果" in chinese
    assert english.startswith("BLOCKED: Command denied by user.")


def test_terminal_auto_approval_note_uses_review_language():
    from tools.terminal_tool import _format_approval_note

    chinese = _format_approval_note(
        {
            "smart_approved": True,
            "smart_review": {"reason": "这是常规开发测试。"},
        },
        "test command",
    )
    english = _format_approval_note(
        {
            "smart_approved": True,
            "smart_review": {"reason": "Routine development test."},
        },
        "test command",
    )

    assert chinese == "命令曾触发安全检查，已由智能审批自动批准：这是常规开发测试。"
    assert english == (
        "Command was flagged (test command) and auto-approved by smart approval."
    )


def test_shell_heredoc_is_inline_code_not_external_script_evidence(tmp_path: Path):
    python_evidence = _collect_direct_script_evidence(
        "python - <<'PY'\nprint('ok')\nPY",
        cwd=str(tmp_path),
        source_kind="shell",
    )
    bash_evidence = _collect_direct_script_evidence(
        "bash <<-'SH'\necho ok\nSH",
        cwd=str(tmp_path),
        source_kind="shell",
    )

    assert python_evidence == []
    assert bash_evidence == []


def test_real_script_before_heredoc_is_still_collected(tmp_path: Path):
    script = tmp_path / "reader.py"
    script.write_text("print(input())\n")

    evidence = _collect_direct_script_evidence(
        "python reader.py <<'EOF'\nhello\nEOF",
        cwd=str(tmp_path),
        source_kind="shell",
    )

    assert evidence == [
        {"path": str(script), "status": "read", "content": "print(input())\n"}
    ]


def test_execute_code_direct_subprocess_script_is_read(tmp_path: Path):
    script = tmp_path / "cleanup.py"
    script.write_text("print('cleanup')\n")
    code = """
import subprocess, sys
subprocess.run([sys.executable, "cleanup.py"], check=True)
"""

    evidence = _collect_direct_script_evidence(
        code,
        cwd=str(tmp_path),
        source_kind="python",
    )

    assert evidence == [
        {"path": str(script), "status": "read", "content": "print('cleanup')\n"}
    ]


def test_external_script_reader_wins_over_same_named_local_file(tmp_path: Path):
    script = tmp_path / "deploy.sh"
    script.write_text("echo local\n")

    evidence = _collect_direct_script_evidence(
        "bash deploy.sh",
        cwd=str(tmp_path),
        source_kind="shell",
        read_script=lambda _path: "echo remote\n",
    )

    assert evidence == [
        {"path": str(script), "status": "read", "content": "echo remote\n"}
    ]


def test_unreadable_direct_script_is_explicit_evidence_gap(tmp_path: Path):
    missing = tmp_path / "missing.py"

    evidence = _collect_direct_script_evidence(
        "python missing.py",
        cwd=str(tmp_path),
        source_kind="shell",
    )

    assert evidence == [
        {"path": str(missing), "status": "unreadable", "content": ""}
    ]


def test_direct_script_limit_reports_unreviewed_remainder(tmp_path: Path):
    commands = []
    for index in range(5):
        script = tmp_path / f"task-{index}.py"
        script.write_text(f"print({index})\n")
        commands.append(f"python {script.name}")

    evidence = _collect_direct_script_evidence(
        " && ".join(commands),
        cwd=str(tmp_path),
        source_kind="shell",
    )

    assert evidence[-1] == {
        "path": "<additional-direct-scripts>",
        "status": "unreadable",
        "content": "",
    }


def test_smart_approval_returns_structured_decision_and_receives_context(tmp_path: Path):
    script = tmp_path / "cleanup.py"
    script.write_text("from pathlib import Path\nPath('/data/cache-a').unlink()\n")
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps(
                        {
                            "decision": "approve",
                            "risk_level": "high",
                            "authorization": "exact",
                            "reason": "用户明确授权删除同一目标。",
                        },
                        ensure_ascii=False,
                    )
                )
            )
        ]
    )
    context = {
        "latest_user_message": "删除 /data/cache-a",
        "clarifications": [
            {"question": "是否删除 /data/cache-a？", "answer": "可以删除"}
        ],
    }

    with patch("agent.auxiliary_client.call_llm", return_value=response) as call_llm:
        result = _smart_approve(
            "python cleanup.py",
            "script execution",
            approval_context=context,
            cwd=str(tmp_path),
        )

    assert result.decision == "approve"
    assert result.risk_level == "high"
    assert result.authorization == "exact"
    assert result.reason == "用户明确授权删除同一目标。"
    prompt = call_llm.call_args.kwargs["messages"][1]["content"]
    assert "删除 /data/cache-a" in prompt
    assert "是否删除 /data/cache-a？" in prompt
    assert "可以删除" in prompt
    assert "Path('/data/cache-a').unlink()" in prompt
    assert f'<execution_cwd>"{tmp_path}"</execution_cwd>' in prompt
    assert call_llm.call_args.kwargs["max_tokens"] >= 128


def _approval_response(**payload):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps(payload, ensure_ascii=False)
                )
            )
        ]
    )


def _smart_system_prompt_for(
    command: str,
    latest_user_message: str,
    clarifications: list[dict[str, str]] | None = None,
) -> str:
    response = _approval_response(
        decision="escalate",
        risk_level="low",
        authorization="unclear",
        reason="test capture",
    )
    context = {
        "latest_user_message": latest_user_message,
        "clarifications": clarifications or [],
    }
    with (
        patch("agent.auxiliary_client.call_llm", return_value=response) as call_llm,
        patch("tools.approval._get_smart_policy", return_value=""),
    ):
        _smart_approve(command, "flagged", approval_context=context)
    return call_llm.call_args.kwargs["messages"][0]["content"]


def test_prompt_keeps_baseline_risk_independent_from_task_relevance():
    prompt = _smart_system_prompt_for(
        "printf diagnostic",
        "用户正在处理另一个任务",
    )

    assert (
        "Do not judge whether the action is relevant to, helpful for, or the best "
        "way to complete the user's task."
    ) in prompt
    assert (
        "A baseline-safe action does not require user authorization or task relevance."
        in prompt
    )


def test_prompt_uses_user_context_only_for_explicit_overrides():
    prompt = _smart_system_prompt_for(
        "perform a flagged operation",
        "请用合适的方法处理",
    )

    assert "Use authorization evidence only for explicit overrides" in prompt
    assert "explicit approval of the risky action" in prompt
    assert "explicit prohibition, refusal, or mandatory condition" in prompt
    assert (
        "Ordinary task descriptions, preferences, requested methods, and silence are "
        "not prohibitions or mandatory conditions."
    ) in prompt


def test_prompt_marks_baseline_safe_actions_as_sufficiently_authorized():
    prompt = _smart_system_prompt_for(
        "display local information",
        "未提及这个具体检查",
    )

    assert (
        "For a baseline-safe action, use authorization=sufficient even when the user "
        "message does not mention it."
    ) in prompt


def test_prompt_requires_reason_in_latest_user_language():
    prompt = _smart_system_prompt_for(
        "perform a flagged operation",
        "请检查这个操作是否安全",
    )

    assert (
        "Write reason in the same natural language as the latest real user message."
        in prompt
    )
    assert (
        "If that message is empty or language-neutral, use the dominant natural language "
        "in the authorization evidence; if still unclear, use English."
        in prompt
    )


def test_prompt_treats_read_only_network_retrieval_as_baseline_safe():
    prompt = _smart_system_prompt_for(
        "retrieve public diagnostics into a new temporary file",
        "试一下效果",
    )

    assert (
        "Straightforward read-only network retrieval and fresh temporary diagnostic "
        "outputs are baseline-safe."
    ) in prompt


def test_prompt_requires_approval_for_direct_secret_use_in_external_request():
    prompt = _smart_system_prompt_for(
        "read a stored credential and attach it to an ad hoc external request",
        "检查接口是否正常",
    )

    assert (
        "Reading a stored credential directly and attaching it to an ad hoc external "
        "request is security-sensitive and requires explicit approval."
    ) in prompt
    assert (
        "Ordinary built-in authentication by a trusted tool is not, by itself, a reason "
        "to escalate."
    ) in prompt
    assert (
        "A temporary credential adapter used only to authenticate a standard trusted "
        "tool operation counts as ordinary built-in authentication."
    ) in prompt


def test_prompt_does_not_require_recursive_source_for_visible_read_only_command():
    prompt = _smart_system_prompt_for(
        "use a trusted tool's list operation to display recent records",
        "查看最近记录",
    )

    assert (
        "When visible syntax clearly asks a trusted tool only to display or list "
        "information, treat it as baseline-safe without requiring the tool's implementation "
        "source."
    ) in prompt
    assert (
        "Visible evidence of mutation or interactive behavior still requires normal risk "
        "review."
    ) in prompt


def test_unreadable_direct_script_cannot_be_auto_approved(tmp_path: Path):
    response = _approval_response(
        decision="approve",
        risk_level="medium",
        authorization="sufficient",
        reason="看起来属于正常步骤。",
    )

    with patch("agent.auxiliary_client.call_llm", return_value=response):
        result = _smart_approve(
            "python missing.py",
            "direct script execution",
            approval_context={"latest_user_message": "运行脚本"},
            cwd=str(tmp_path),
        )

    assert result.decision == "escalate"


def test_inconsistent_high_risk_approval_without_exact_authorization_escalates():
    response = _approval_response(
        decision="approve",
        risk_level="high",
        authorization="unclear",
        reason="范围不够明确。",
    )

    with patch("agent.auxiliary_client.call_llm", return_value=response):
        result = _smart_approve(
            "rm -rf /data/cache-a",
            "recursive delete",
            approval_context={"latest_user_message": "清理一下"},
        )

    assert result.decision == "escalate"


def test_critical_risk_is_denied_even_if_model_requested_escalation():
    response = _approval_response(
        decision="escalate",
        risk_level="critical",
        authorization="exact",
        reason="灾难性系统破坏。",
    )

    with patch("agent.auxiliary_client.call_llm", return_value=response):
        result = _smart_approve(
            "rm -rf /",
            "recursive delete in root path",
            approval_context={"latest_user_message": "执行"},
        )

    assert result.decision == "deny"


def test_legacy_string_compatibility_preserves_hash_contract():
    result = SmartApprovalResult("approve", "low", "sufficient", "正常步骤。")

    assert result == "approve"
    assert hash(result) == hash("approve")


def test_approval_context_is_isolated_between_threads():
    def worker(value: str) -> str:
        token = set_smart_approval_context({"latest_user_message": value})
        try:
            return get_smart_approval_context()["latest_user_message"]
        finally:
            reset_smart_approval_context(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ["alpha", "beta"]))

    assert results == ["alpha", "beta"]
    assert get_smart_approval_context() == {}


def test_direct_script_is_smart_reviewed_even_when_shell_text_is_not_flagged(
    tmp_path: Path, monkeypatch
):
    script = tmp_path / "deploy.sh"
    script.write_text("#!/bin/sh\necho safe\n")
    session_key = "direct-script-smart-review"
    clear_session(session_key)
    monkeypatch.setenv("HERMES_SESSION_KEY", session_key)
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.setattr("tools.approval._YOLO_MODE_FROZEN", False)
    monkeypatch.setattr("tools.approval._get_approval_config", lambda: {"mode": "smart"})
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
    )
    review = SmartApprovalResult("approve", "low", "sufficient", "脚本内容与任务一致。")

    with patch("tools.approval._smart_approve", return_value=review) as smart_review:
        result = check_all_command_guards(
            "bash deploy.sh", "local", cwd=str(tmp_path)
        )

    assert result["approved"] is True
    assert result["smart_approved"] is True
    assert smart_review.call_count == 1


def test_model_dispatch_binds_and_resets_request_approval_context():
    import model_tools

    context = {
        "latest_user_message": "删除 /tmp/cache-a",
        "clarifications": [],
    }

    def fake_dispatch(*_args, **_kwargs):
        return json.dumps(get_smart_approval_context(), ensure_ascii=False)

    with patch.object(model_tools.registry, "dispatch", side_effect=fake_dispatch):
        result = model_tools.handle_function_call(
            "terminal",
            {"command": "echo ok"},
            approval_context=context,
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        )

    assert json.loads(result) == context
    assert get_smart_approval_context() == {}
