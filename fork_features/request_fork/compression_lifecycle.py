"""Fork-owned compression observers; the host retains locks, summary and commit.

Own the request snapshot, hook payload and deferred finish together. The host only
reports admission/adoption and commit outcomes; it never inspects our state key.
"""
from __future__ import annotations

import copy
import logging
import sys
from typing import Any, Optional

logger = logging.getLogger("agent.conversation_compression")
_MAX_PERSISTENT_COMPRESSION_CONTEXT_CHARS = 64_000
_PENDING_COMPRESSION_LIFECYCLE_FINISH = "_pending_compression_lifecycle_finish"


def has_pending_finish(agent: Any) -> bool:
    return callable(getattr(agent, _PENDING_COMPRESSION_LIFECYCLE_FINISH, None))


def finalize_pending_finish(agent: Any, *, committed: bool) -> bool:
    """Consume once even when the outer host transaction was rolled back."""
    pending = getattr(agent, _PENDING_COMPRESSION_LIFECYCLE_FINISH, None)
    setattr(agent, _PENDING_COMPRESSION_LIFECYCLE_FINISH, None)
    return bool(pending(committed)) if callable(pending) else False


class CompressionLifecycle:
    """One mutation-safe Fork lifecycle spanning summary, durable commit and outer commit."""

    def __init__(
        self, agent: Any, *, attempt_id: str, in_place: bool, trigger_source: str,
        request_fork: Any, request_fork_rematerializer: Any, commit_fence: Any,
        defer_finish: bool,
    ) -> None:
        self.agent = agent
        self.attempt_id = attempt_id
        self.in_place = in_place
        self.trigger_source = trigger_source
        self.request_fork: Any = request_fork
        self.request_fork_rematerializer = request_fork_rematerializer
        self.commit_fence = commit_fence
        self.defer_finish = defer_finish
        self.start_session_id = agent.session_id or ""
        self.started = False
        self.finished = False
        self.outcome: Optional[str] = None
        self.reason = ""
        self.persistent_context_source = ""

    def rematerialize_after_adoption(self, messages: list) -> None:
        """Replace a stale prepared request with the host-owned adopted-parent rebuild."""
        if not callable(self.request_fork_rematerializer):
            self.request_fork = None
            return
        try:
            self.request_fork = self.request_fork_rematerializer(messages)
        except Exception:
            self.request_fork = None
            logger.warning("request-fork rematerialization after durable-parent adoption failed", exc_info=True)

    def start(self, messages: list) -> None:
        """Publish the admitted request snapshot after adoption and before summary work."""
        if self.started:
            return
        try:
            from hermes_cli.lifecycle import has_hook, invoke_hook

            if not (has_hook("on_compression_start") or has_hook("on_compression_finish")):
                return
            self.started = True
            if not has_hook("on_compression_start"):
                return
            frozen_body = self.request_fork.clone_body() if self.request_fork is not None else {}
            request_messages = frozen_body.get("input")
            tools = frozen_body.get("tools")
            payload = {
                "compression_id": self.attempt_id,
                "session_id": self.start_session_id,
                "in_place": self.in_place,
                "api_mode": str(getattr(self.agent, "api_mode", "") or ""),
                "messages": copy.deepcopy(messages),
                "request_messages": copy.deepcopy(request_messages if isinstance(request_messages, list) else messages),
                "tools": copy.deepcopy(
                    tools if isinstance(tools, list) else (getattr(self.agent, "tools", None) or [])
                ),
                "request_fork_available": self.request_fork is not None,
                "request_fork_fidelity": (
                    getattr(self.request_fork, "fidelity", None) if self.request_fork is not None else None
                ),
                "trigger_source": self.trigger_source,
            }
            if self.request_fork is not None:
                from fork_features.request_fork import current_request_fork_scope

                with current_request_fork_scope(
                    self.agent,
                    frozen_request=self.request_fork,
                    progress_callback=(
                        self.commit_fence.touch_progress if self.commit_fence is not None else None
                    ),
                ):
                    invoke_hook("on_compression_start", **copy.deepcopy(payload))
            else:
                invoke_hook("on_compression_start", **copy.deepcopy(payload))
        except Exception:
            logger.warning("on_compression_start hook failed", exc_info=True)

    def prepare_persistent_context(self) -> Optional[dict[str, Any]]:
        """Collect one bounded hidden context row before entering the commit fence."""
        self.persistent_context_source = ""
        try:
            from hermes_cli.lifecycle import has_hook, invoke_hook

            if not has_hook("on_compression_prepare_commit"):
                return None
            results = invoke_hook(
                "on_compression_prepare_commit",
                compression_id=self.attempt_id,
                session_id=self.start_session_id,
                in_place=self.in_place,
                api_mode=str(getattr(self.agent, "api_mode", "") or ""),
                trigger_source=self.trigger_source,
                max_context_chars=_MAX_PERSISTENT_COMPRESSION_CONTEXT_CHARS,
            )
        except Exception:
            logger.warning("on_compression_prepare_commit hook failed", exc_info=True)
            return None

        from .session_recovery import recovery_context_message

        prepared, self.persistent_context_source = recovery_context_message(
            results, max_context_chars=_MAX_PERSISTENT_COMPRESSION_CONTEXT_CHARS,
        )
        return prepared

    def set_outcome(self, outcome: str, reason: str = "") -> None:
        self.outcome = outcome
        self.reason = reason

    def _publish_finish(self, outcome: str, reason: str) -> bool:
        try:
            from hermes_cli.lifecycle import has_hook, invoke_hook

            if not has_hook("on_compression_finish"):
                return False
            invoke_hook(
                "on_compression_finish",
                compression_id=self.attempt_id,
                session_id=self.agent.session_id or "",
                old_session_id=self.start_session_id,
                in_place=self.in_place,
                outcome=outcome,
                reason=reason,
                trigger_source=self.trigger_source,
                persistent_context_source=(
                    self.persistent_context_source if outcome == "committed" else ""
                ),
            )
            return True
        except Exception:
            logger.warning("on_compression_finish hook failed", exc_info=True)
            return False

    def finish(self) -> None:
        """Publish exactly one terminal outcome for every published start."""
        if not self.started or self.finished:
            return
        self.finished = True
        outcome = self.outcome
        reason = self.reason
        if outcome is None:
            exc_type = sys.exc_info()[0]
            outcome = "failed" if exc_type is not None else "aborted"
            reason = exc_type.__name__ if exc_type is not None else "unfinished"
        if self.defer_finish and outcome == "committed":
            def _finalize_outer_commit(committed: bool) -> bool:
                return self._publish_finish(
                    "committed" if committed else "aborted",
                    reason if committed else "outer_commit_aborted",
                )

            setattr(self.agent, _PENDING_COMPRESSION_LIFECYCLE_FINISH, _finalize_outer_commit)
            return
        self._publish_finish(outcome, reason)
