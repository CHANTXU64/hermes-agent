"""Public Fork policy boundary for Smart Approval.

The host approval core owns deterministic floors, allowlists, human transports,
and execution.  This module owns request-scoped authorization context and
composes the Fork evidence, reviewer, and retry policies behind one facade.
Host services are injected; this module never imports the approval core or tool
execution modules.
"""

from __future__ import annotations

import contextvars
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Optional

from . import retry_policy, script_evidence, smart_review

SmartApprovalResult = smart_review.SmartApprovalResult
AutomatedDenialState = retry_policy.AutomatedDenialState
MAX_SCRIPT_BYTES = script_evidence.MAX_SCRIPT_BYTES

_smart_approval_context: contextvars.ContextVar[dict[str, Any]] = (
    contextvars.ContextVar("smart_approval_context", default={})
)


def set_smart_approval_context(
    context: Optional[dict[str, Any]],
) -> contextvars.Token:
    return _smart_approval_context.set(dict(context or {}))


def reset_smart_approval_context(token: contextvars.Token) -> None:
    _smart_approval_context.reset(token)


def get_smart_approval_context() -> dict[str, Any]:
    return dict(_smart_approval_context.get() or {})


_INJECTED_CONTEXT_SPANS = re.compile(
    r'<hermes-runtime-context user-authored="false" [\s\S]*?</hermes-runtime-context>'
    r'|<\s*memory-context\s*>[\s\S]*?</\s*memory-context\s*>',
    re.IGNORECASE,
)


def _tool_call_parts(tool_call: Any) -> tuple[str, str, Any]:
    if isinstance(tool_call, dict):
        function = tool_call.get("function") or {}
        return (
            str(tool_call.get("id") or ""),
            str(function.get("name") or ""),
            function.get("arguments"),
        )
    function = getattr(tool_call, "function", None)
    return (
        str(getattr(tool_call, "id", "") or ""),
        str(getattr(function, "name", "") or ""),
        getattr(function, "arguments", None),
    )


def _answer_text(answer: Any) -> str:
    if isinstance(answer, list):
        return json.dumps(answer, ensure_ascii=False)
    return "" if answer is None else str(answer)


def _clarify_entries(raw_args: Any, content: Any) -> list[dict[str, str]]:
    """Return question/answer pairs from a completed Clarify result."""
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        payload = json.loads(content) if isinstance(content, str) else content
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    args = args if isinstance(args, dict) else {}
    if isinstance(payload.get("responses"), list):
        asked = args.get("questions") if isinstance(args.get("questions"), list) else []
        entries = []
        for index, row in enumerate(payload["responses"]):
            if not isinstance(row, dict):
                continue
            question = row.get("question")
            if not question and index < len(asked) and isinstance(asked[index], dict):
                question = asked[index].get("question")
            if not isinstance(question, str) or not question.strip():
                continue
            entry = {
                "question": question.strip(),
                "answer": _answer_text(row.get("user_response")),
            }
            if row.get("status"):
                entry["status"] = str(row["status"])
            entries.append(entry)
        return entries
    question = args.get("question")
    if "user_response" not in payload or not isinstance(question, str) or not question.strip():
        return []
    return [{"question": question.strip(), "answer": _answer_text(payload.get("user_response"))}]


def build_smart_approval_context(
    messages: list,
    *,
    is_real_user_message: Callable[[Any], bool],
    real_user_message_text: Callable[[dict[str, Any]], str],
    strip_stale_todo_snapshot: Callable[[Any], Any],
) -> dict[str, Any]:
    """Return the session's user messages, Clarify answers and agent tool calls.

    ``history`` keeps them in conversation order. Assistant prose, reasoning and
    tool outputs are never included. ``latest_user_message`` and
    ``clarifications`` (answers after that message) stay for the retry brief.
    """
    if not isinstance(messages, list):
        return {"latest_user_message": "", "clarifications": [], "history": []}

    history: list[dict[str, str]] = []
    clarify_calls: dict[str, Any] = {}
    latest_user_position = -1
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "user":
            if not is_real_user_message(message):
                continue
            normalized = dict(message)
            normalized["content"] = strip_stale_todo_snapshot(message.get("content", ""))
            text = _INJECTED_CONTEXT_SPANS.sub("", real_user_message_text(normalized)).strip()
            if not text:
                continue
            latest_user_position = len(history)
            history.append({"type": "user_message", "text": text})
        elif role == "assistant":
            for tool_call in message.get("tool_calls") or []:
                try:
                    call_id, name, raw_args = _tool_call_parts(tool_call)
                except Exception:
                    continue
                if name == "clarify":
                    if call_id:
                        clarify_calls[call_id] = raw_args
                    continue
                if not name:
                    continue
                arguments = (
                    raw_args
                    if isinstance(raw_args, str)
                    else json.dumps(raw_args, ensure_ascii=False, sort_keys=True)
                )
                history.append(
                    {"type": "agent_tool_call", "name": name, "arguments": arguments}
                )
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id not in clarify_calls:
                continue
            for entry in _clarify_entries(clarify_calls.pop(call_id), message.get("content", "")):
                history.append({"type": "clarify", **entry})

    latest_user_message = (
        history[latest_user_position]["text"] if latest_user_position >= 0 else ""
    )
    clarifications = [
        {key: value for key, value in item.items() if key != "type"}
        for item in history[latest_user_position + 1 :]
        if item["type"] == "clarify"
    ]
    return {
        "latest_user_message": latest_user_message,
        "clarifications": clarifications,
        "history": history,
    }


@dataclass(frozen=True)
class ApprovalPolicy:
    """Concrete facade over the Fork Smart Approval policy modules."""

    approval_context: dict[str, Any]
    interface_language: str
    operator_policy: str
    strip_shell_comments: Callable[[str], str]
    call_llm: Callable[..., Any]
    redact_action: Callable[[str], str]
    retry_key: Optional[tuple[str, str]]
    lock: Any
    max_retry_entries: int

    @property
    def prefers_chinese(self) -> bool:
        return self.interface_language in {"zh", "zh-hant"}

    def with_context(self, context: dict[str, Any]) -> ApprovalPolicy:
        return replace(self, approval_context=dict(context))

    def collect_script_evidence(
        self,
        action: str,
        *,
        cwd: Optional[str],
        source_kind: str,
        read_script: Optional[Callable[[str], Optional[str]]],
    ) -> list[dict[str, str]]:
        return script_evidence.collect_direct_script_evidence(
            action,
            cwd=cwd,
            source_kind=source_kind,
            read_script=read_script,
        )

    def review(
        self,
        action: str,
        description: str,
        *,
        cwd: Optional[str] = None,
        source_kind: str = "shell",
        read_script: Optional[Callable[[str], Optional[str]]] = None,
        script_evidence: Optional[list[dict[str, str]]] = None,
    ) -> SmartApprovalResult:
        return smart_review.review_action(
            action,
            description,
            approval_context=dict(self.approval_context),
            cwd=cwd,
            source_kind=source_kind,
            read_script=read_script,
            interface_language=self.interface_language,
            operator_policy=self.operator_policy,
            strip_shell_comments=self.strip_shell_comments,
            call_llm=self.call_llm,
            script_evidence=script_evidence,
        )

    def parse_review(self, raw: str) -> SmartApprovalResult:
        return smart_review.parse_smart_approval_result(
            raw,
            prefers_chinese=self.prefers_chinese,
        )

    def enforce_review(
        self,
        review: SmartApprovalResult,
        evidence: list[dict[str, str]],
    ) -> SmartApprovalResult:
        return smart_review.enforce_smart_approval_contract(
            review,
            evidence,
            prefers_chinese=self.prefers_chinese,
        )

    def format_review(self, review: SmartApprovalResult) -> str:
        return smart_review.format_smart_review_description(
            review,
            prefers_chinese=self.prefers_chinese,
        )

    def format_repeat_denial(
        self,
        outcome: str,
        deny_reason: Optional[str] = None,
        breaker_addendum: str = "",
    ) -> str:
        return retry_policy.format_user_denial_message(
            outcome,
            deny_reason,
            breaker_addendum,
            prefers_chinese=self.prefers_chinese,
        )

    def first_denial(
        self,
        action: str,
        description: str,
        *,
        source_kind: str,
        breaker_addendum: str = "",
    ) -> dict:
        if self.retry_key is None:
            if self.prefers_chinese:
                message = (
                    f"智能审批已拒绝这项操作。原因：{description}。"
                    "当前执行环境没有可用的一次性人工审批通道，"
                    "因此不能提供重复后转人工的路径。不要重试、改写、拆分或换用其他路径。"
                )
            else:
                message = (
                    f"Smart approval denied this operation: {description}. This execution "
                    "context has no one-shot human approval route, so repeat-to-human "
                    "escalation is unavailable. "
                    "Do not retry, rewrite, split, or use another route."
                )
            return {
                "approved": False,
                "message": message + breaker_addendum,
                "description": description,
                "smart_denied": True,
                "outcome": "auto_denied",
                "retry_escalation_available": False,
                "user_consent": False,
            }
        result = retry_policy.first_automated_denial_result(
            self.retry_key,
            action,
            description,
            source_kind=source_kind,
            prefers_chinese=self.prefers_chinese,
            lock=self.lock,
            max_entries=self.max_retry_entries,
        )
        result["message"] += breaker_addendum
        return result

    def record_user_denial(
        self,
        action: str,
        *,
        source_kind: str,
        reason: Optional[str] = None,
    ) -> None:
        if self.retry_key is None:
            return
        retry_policy.record_user_denial(
            self.retry_key,
            action,
            source_kind=source_kind,
            reason=reason,
            lock=self.lock,
            max_entries=self.max_retry_entries,
        )

    def latched_user_denial(
        self,
        action: str,
        *,
        source_kind: str,
    ) -> Optional[dict]:
        if self.retry_key is None:
            return None
        return retry_policy.latched_user_denial_result(
            self.retry_key,
            action,
            source_kind=source_kind,
            prefers_chinese=self.prefers_chinese,
            lock=self.lock,
        )

    def consume_similar_denial(
        self,
        action: str,
        *,
        source_kind: str,
    ) -> Optional[AutomatedDenialState]:
        if self.retry_key is None:
            return None
        return retry_policy.consume_similar_automated_denial(
            self.retry_key,
            action,
            source_kind=source_kind,
            lock=self.lock,
        )

    def repeat_manual_description(
        self,
        action: str,
        policy_reason: str,
        *,
        source_kind: str,
    ) -> str:
        return retry_policy.generate_repeat_manual_description(
            action,
            policy_reason,
            latest_user_message=str(
                self.approval_context.get("latest_user_message") or ""
            ),
            redact_action=self.redact_action,
            call_llm=self.call_llm,
            source_kind=source_kind,
        )


def clear_retry_session(session_key: str, *, lock: Any) -> None:
    retry_policy.clear_session(session_key, lock=lock)
