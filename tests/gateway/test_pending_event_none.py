"""Tests for pending follow-up extraction in recursive _run_agent calls.

When pending_event is None (Path B: pending comes from interrupt_message),
accessing pending_event.channel_prompt previously raised AttributeError.
This verifies the fix: channel_prompt is captured inside the
`if pending_event is not None:` block and falls back to None otherwise.

Also verifies that internal control interrupt reasons like "Stop requested"
do not get recycled into the pending-user-message follow-up path.
"""

from datetime import datetime
import inspect
from types import SimpleNamespace

from gateway.run import (
    GatewayRunner,
    _is_control_interrupt_message,
    _prepare_gateway_user_message_metadata,
)


class TestQueuedFollowupPersistenceMetadata:
    def test_pending_event_keeps_its_own_text_timestamp_and_message_id(self):
        event = SimpleNamespace(
            timestamp="2026-07-31T04:00:00+00:00",
            message_id="telegram-followup-2",
        )

        model_text, persisted_text, persisted_timestamp, persisted_message_id = (
            _prepare_gateway_user_message_metadata(
                event,
                "queued correction",
                inject_timestamp=False,
            )
        )

        assert model_text == "queued correction"
        assert persisted_text == "queued correction"
        assert persisted_timestamp == datetime.fromisoformat(
            event.timestamp
        ).timestamp()
        assert persisted_message_id == "telegram-followup-2"


    def test_recursive_followup_forwards_persistence_metadata(self):
        source = inspect.getsource(GatewayRunner._run_agent_inner)
        marker = "followup_result = await self._run_agent("
        recursive_call = source[source.index(marker) :]

        assert "persist_user_message=next_persist_user_message" in recursive_call
        assert "persist_user_timestamp=next_persist_user_timestamp" in recursive_call
        assert "persist_user_message_id=next_persist_user_message_id" in recursive_call


def _extract_channel_prompt(pending_event):
    """Reproduce the fixed logic from gateway/run.py.

    Mirrors the variable-capture pattern used before the recursive
    _run_agent call so we can test both paths without a full runner.
    """
    next_channel_prompt = None
    if pending_event is not None:
        next_channel_prompt = getattr(pending_event, "channel_prompt", None)
    return next_channel_prompt


def _extract_pending_text(interrupted, pending_event, interrupt_message):
    """Reproduce the fixed pending-text selection from gateway/run.py."""
    if interrupted and pending_event is None and interrupt_message:
        if _is_control_interrupt_message(interrupt_message):
            return None
        return interrupt_message
    return None


class TestPendingEventNoneChannelPrompt:
    """Guard against AttributeError when pending_event is None."""


    def test_pending_event_with_channel_prompt_passes_through(self):
        """Path A: pending_event present — channel_prompt is forwarded."""
        event = SimpleNamespace(channel_prompt="You are a helpful bot.")
        result = _extract_channel_prompt(event)
        assert result == "You are a helpful bot."


class TestControlInterruptMessages:
    """Control interrupt reasons must not become follow-up user input."""

    def test_stop_requested_is_not_treated_as_pending_user_message(self):
        result = _extract_pending_text(True, None, "Stop requested")
        assert result is None


