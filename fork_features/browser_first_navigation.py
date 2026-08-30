"""Fork-owned policy for a conversation's first browser navigation.

The upstream Browser tool owns URL safety, backend/session selection, command
execution, navigation results, and snapshots. This module owns only the Fork
preference that a new Hermes conversation gets a fresh active tab before its
first navigation, plus the per-conversation serialization needed to keep that
tab/open sequence ordered.
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable
from typing import Any, Optional


FIRST_NAVIGATION_DESCRIPTION = (
    "On the first call in a new conversation, it automatically opens a new "
    "tab and switches to it before loading the URL."
)

_initialized_task_ids: set[str] = set()
_navigation_locks: dict[str, threading.Lock] = {}
_state_guard = threading.Lock()


def _navigation_lock(task_id: str) -> threading.Lock:
    """Return the process-local lock that serializes one conversation."""
    with _state_guard:
        lock = _navigation_locks.get(task_id)
        if lock is None:
            lock = threading.Lock()
            _navigation_locks[task_id] = lock
        return lock


def serialize_conversation_navigation(func: Callable) -> Callable:
    """Keep tab creation, navigation, and the result snapshot ordered per task."""

    @functools.wraps(func)
    def wrapped(url: str, task_id: Optional[str] = None):
        effective_task_id = task_id or "default"
        with _navigation_lock(effective_task_id):
            return func(url, task_id=task_id)

    return wrapped


def ensure_first_conversation_tab(
    *,
    task_id: str,
    session_key: str,
    run_command: Callable[..., dict[str, Any]],
    timeout: int,
) -> Optional[dict[str, Any]]:
    """Open one fresh active tab per Hermes conversation.

    Returns ``None`` when no further action is needed. A failed tab command is
    normalized to the Browser tool's existing error shape and deliberately does
    not mark the conversation initialized, so the next navigation retries.
    The caller serializes the complete navigation with
    :func:`serialize_conversation_navigation`, which prevents same-task races
    while allowing different conversations to navigate independently.
    """
    with _state_guard:
        if task_id in _initialized_task_ids:
            return None

    tab_result = run_command(
        session_key,
        "tab",
        ["new"],
        timeout=timeout,
    )
    if not tab_result.get("success"):
        return {
            "success": False,
            "error": tab_result.get("error", "Failed to open a new browser tab"),
        }

    with _state_guard:
        _initialized_task_ids.add(task_id)
    return None
