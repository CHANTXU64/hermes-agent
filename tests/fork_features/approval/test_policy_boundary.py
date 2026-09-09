from __future__ import annotations

import ast
import json
import threading
from pathlib import Path
from types import SimpleNamespace

from fork_features.approval.policy import (
    ApprovalPolicy,
    build_smart_approval_context as build_policy_context,
    get_smart_approval_context,
    reset_smart_approval_context,
    set_smart_approval_context,
)
from fork_features.approval.smart_review import SmartApprovalResult


def _repo() -> Path:
    return Path(__file__).resolve().parents[3]


def _top_level_definitions(source: str) -> set[str]:
    tree = ast.parse(source)
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def test_policy_builds_latest_real_user_and_scoped_clarify_context() -> None:
    messages = [
        {"role": "user", "content": "old", "real": True},
        {"role": "user", "content": "runtime", "real": False},
        {"role": "user", "content": "delete cache", "real": True},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "clarify-1",
                    "function": {
                        "name": "clarify",
                        "arguments": json.dumps({"question": "Delete only cache A?"}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "clarify-1",
            "content": json.dumps({"user_response": "yes"}),
        },
    ]

    context = build_policy_context(
        messages,
        is_real_user_message=lambda message: bool(message.get("real")),
        real_user_message_text=lambda message: str(message.get("content") or ""),
        strip_stale_todo_snapshot=lambda content: content,
    )

    assert context == {
        "latest_user_message": "delete cache",
        "clarifications": [{"question": "Delete only cache A?", "answer": "yes"}],
    }


def test_policy_context_binding_is_request_local() -> None:
    original = get_smart_approval_context()
    token = set_smart_approval_context(
        {"latest_user_message": "one", "clarifications": []}
    )
    try:
        assert get_smart_approval_context()["latest_user_message"] == "one"
        seen: list[dict] = []

        def worker() -> None:
            seen.append(get_smart_approval_context())

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        assert seen == [{}]
    finally:
        reset_smart_approval_context(token)
    assert get_smart_approval_context() == original


def test_concrete_policy_returns_existing_structured_review_contract() -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps(
                        {
                            "decision": "approve",
                            "risk_level": "low",
                            "authorization": "sufficient",
                            "reason": "safe local bookkeeping",
                        }
                    )
                )
            )
        ]
    )
    policy = ApprovalPolicy(
        approval_context={"latest_user_message": "", "clarifications": []},
        interface_language="en",
        operator_policy="",
        strip_shell_comments=lambda command: command,
        call_llm=lambda **_kwargs: response,
        redact_action=lambda action: action,
        retry_key=("session", "turn"),
        lock=threading.RLock(),
        max_retry_entries=16,
    )

    review = policy.review(
        "python safe.py",
        "Direct script execution",
        cwd="/tmp",
        source_kind="shell",
        read_script=lambda _path: None,
        script_evidence=[],
    )

    assert review == SmartApprovalResult(
        "approve", "low", "sufficient", "safe local bookkeeping"
    )


def test_high_churn_hosts_consume_only_public_policy_facade() -> None:
    repo = _repo()
    approval_source = (repo / "tools/approval.py").read_text(encoding="utf-8")
    approval_tree = ast.parse(approval_source)
    forbidden_modules = {
        "fork_features.approval.retry_policy",
        "fork_features.approval.script_evidence",
        "fork_features.approval.smart_review",
    }
    imported_modules = {
        node.module
        for node in ast.walk(approval_tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert forbidden_modules.isdisjoint(imported_modules)
    assert "fork_features.approval.policy" in imported_modules

    forbidden_host_definitions = {
        "set_smart_approval_context",
        "reset_smart_approval_context",
        "get_smart_approval_context",
        "_first_automated_denial_result",
        "_record_user_denial",
        "_latched_user_denial_result",
        "_consume_similar_automated_denial",
        "_generate_repeat_manual_description",
        "_format_smart_review_description",
        "_format_user_denial_message",
        "_parse_smart_approval_result",
        "_enforce_smart_approval_contract",
    }
    assert forbidden_host_definitions.isdisjoint(
        _top_level_definitions(approval_source)
    )

    executor_source = (repo / "agent/tool_executor.py").read_text(encoding="utf-8")
    executor_tree = ast.parse(executor_source)
    builder = next(
        node
        for node in executor_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "build_smart_approval_context"
    )
    assert builder.end_lineno is not None
    assert builder.end_lineno - builder.lineno + 1 <= 20
    assert "build_policy_context" in ast.unparse(builder)

    policy_source = (
        repo / "fork_features/approval/policy.py"
    ).read_text(encoding="utf-8")
    policy_tree = ast.parse(policy_source)
    forbidden_policy_imports = {
        "tools.approval",
        "tools.terminal_tool",
        "tools.code_execution_tool",
        "agent.tool_executor",
    }
    policy_imports = {
        node.module
        for node in ast.walk(policy_tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert forbidden_policy_imports.isdisjoint(policy_imports)

    assert "fork_features.approval.policy" in executor_source
    assert "reset_smart_approval_context" in executor_source
    assert "set_smart_approval_context" in executor_source
