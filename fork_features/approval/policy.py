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


def build_smart_approval_context(
    messages: list,
    *,
    is_real_user_message: Callable[[Any], bool],
    real_user_message_text: Callable[[dict[str, Any]], str],
    strip_stale_todo_snapshot: Callable[[Any], Any],
) -> dict[str, Any]:
    """Return the latest real user message and following completed clarifies."""
    if not isinstance(messages, list):
        return {"latest_user_message": "", "clarifications": []}

    latest_user_index = -1
    latest_user_message = ""
    for index, message in enumerate(messages):
        if not is_real_user_message(message):
            continue
        normalized = dict(message)
        normalized["content"] = strip_stale_todo_snapshot(
            message.get("content", "")
        )
        candidate = real_user_message_text(normalized).strip()
        if (
            candidate.startswith('<hermes-runtime-context user-authored="false" ')
            and candidate.endswith("</hermes-runtime-context>")
        ):
            continue
        latest_user_index = index
        latest_user_message = candidate

    clarify_questions: dict[str, str] = {}
    clarifications: list[dict[str, str]] = []
    for message in messages[latest_user_index + 1 :]:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            for tool_call in message.get("tool_calls") or []:
                try:
                    if isinstance(tool_call, dict):
                        call_id = str(tool_call.get("id") or "")
                        function = tool_call.get("function") or {}
                        name = function.get("name")
                        raw_args = function.get("arguments")
                    else:
                        call_id = str(getattr(tool_call, "id", "") or "")
                        function = getattr(tool_call, "function", None)
                        name = getattr(function, "name", None)
                        raw_args = getattr(function, "arguments", None)
                    if name != "clarify" or not call_id:
                        continue
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if not isinstance(args, dict):
                        continue
                    question = args.get("question")
                    if isinstance(question, str) and question.strip():
                        clarify_questions[call_id] = question.strip()
                except Exception:
                    continue
        elif message.get("role") == "tool":
            call_id = str(message.get("tool_call_id") or "")
            question = clarify_questions.get(call_id)
            if not question:
                continue
            try:
                payload = message.get("content", "")
                if isinstance(payload, str):
                    payload = json.loads(payload)
                if not isinstance(payload, dict) or "user_response" not in payload:
                    continue
                answer = payload.get("user_response")
                if isinstance(answer, list):
                    answer_text = json.dumps(answer, ensure_ascii=False)
                else:
                    answer_text = "" if answer is None else str(answer)
                clarifications.append(
                    {"question": question, "answer": answer_text}
                )
            except Exception:
                continue

    return {
        "latest_user_message": latest_user_message,
        "clarifications": clarifications,
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

    def with_context(self, context: dict[str, Any]) -> "ApprovalPolicy":
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
                    f"智能审批已拒绝这项操作。原因：{description}。当前没有可验证的用户回合，"
                    "因此不能提供重复后转人工的路径。不要重试、改写、拆分或换用其他路径。"
                )
            else:
                message = (
                    f"Smart approval denied this operation: {description}. No verified "
                    "user turn is available, so repeat-to-human escalation is disabled. "
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
