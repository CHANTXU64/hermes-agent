"""Concrete I/O adapters for the Fork approval policy.

The gate supplies retry identity and locking; this module owns bounded script
reads, official auxiliary-client invocation and current policy construction.
No import of tools.approval: terminal and execute_code use these adapters directly.
"""
from __future__ import annotations
import logging
import os
import time
from typing import Any, Optional

from fork_features.approval.policy import MAX_SCRIPT_BYTES

logger = logging.getLogger("tools.approval")


def call_approval_llm(**kwargs: Any) -> Any:
    """Call the structured guardian with an explicit bounded timeout."""
    from agent.auxiliary_client import _get_task_timeout, call_llm

    timeout = _get_task_timeout("approval")
    kwargs.setdefault("timeout", timeout)
    kwargs.setdefault("task", "approval")
    kwargs.setdefault("temperature", 0)
    kwargs.setdefault("max_tokens", 256)
    started = time.monotonic()
    logger.debug("Smart approvals: assessing structured risk (timeout=%ss)", timeout)
    try:
        result = call_llm(**kwargs)
    except Exception as exc:
        logger.warning(
            "Smart approvals: structured LLM call failed after %.1fs (%s: %s)",
            time.monotonic() - started,
            type(exc).__name__,
            exc,
        )
        raise
    logger.debug(
        "Smart approvals: structured LLM call completed in %.1fs",
        time.monotonic() - started,
    )
    return result


def language_prefers_chinese() -> bool:
    from agent.i18n import get_language

    return str(get_language() or "") in {"zh", "zh-hant"}


def read_local_script(path: str) -> Optional[str]:
    """Return a bounded local source prefix for Smart Approval evidence."""
    try:
        real_path = os.path.realpath(path)
        if not os.path.isfile(real_path):
            return None
        with open(real_path, "rb") as handle:
            data = handle.read(MAX_SCRIPT_BYTES + 1)
        return data.decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def read_remote_script(env: Any, path: str) -> Optional[str]:
    """Return a bounded source prefix from the backend that will execute it."""
    if env is None:
        return None
    try:
        import shlex

        result = env.execute(
            f"head -c {MAX_SCRIPT_BYTES + 1} < {shlex.quote(path)}"
        )
        if result.get("returncode", -1) != 0:
            return None
        output = result.get("output", "")
        if not isinstance(output, str) or "\x00" in output:
            return None
        return output
    except Exception:
        return None


def build_policy(*, retry_key, lock, max_retry_entries: int, for_review: bool = False):
    """Bind current Host capabilities to the concrete Fork policy facade."""
    from agent.i18n import get_language
    from agent.redact import redact_sensitive_text
    from fork_features.approval.policy import ApprovalPolicy, get_smart_approval_context
    from tools.approval_smart import _get_smart_policy, _strip_shell_comments

    return ApprovalPolicy(
        approval_context=get_smart_approval_context(),
        interface_language=str(get_language() or ""),
        operator_policy=_get_smart_policy() if for_review else "",
        strip_shell_comments=_strip_shell_comments,
        call_llm=call_approval_llm,
        redact_action=lambda action: redact_sensitive_text(action, force=True),
        retry_key=retry_key,
        lock=lock,
        max_retry_entries=max_retry_entries,
    )
