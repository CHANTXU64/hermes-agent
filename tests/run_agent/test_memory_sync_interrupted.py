"""Regression guard for #15218 — external memory sync must skip interrupted turns.

Before this fix, ``run_conversation`` called
``memory_manager.sync_all(original_user_message, final_response)`` at the
end of every turn where both args were present.  That gate didn't check
the ``interrupted`` flag, so an external memory backend received partial
assistant output, aborted tool chains, or mid-stream resets as durable
conversational truth.  Downstream recall then treated that not-yet-real
state as if the user had seen it complete.

The fix is ``AIAgent._sync_external_memory_for_turn`` — a small helper
that replaces the inline block and returns early when ``interrupted``
is True (regardless of whether ``final_response`` and
``original_user_message`` happen to be populated).

These tests exercise the helper directly on a bare ``AIAgent`` built
via ``__new__`` so the full ``run_conversation`` machinery isn't needed
— the method is pure logic and three state arguments.
"""
from unittest.mock import MagicMock

import pytest


def _bare_agent():
    """Build an ``AIAgent`` with only the attributes
    ``_sync_external_memory_for_turn`` touches — matches the bare-agent
    pattern used across ``tests/run_agent/test_interrupt_propagation.py``.
    """
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent._memory_manager = MagicMock()
    # session_id is now propagated into sync_all / queue_prefetch_all so
    # providers that cache per-session state can update it mid-process
    # (see #6672).
    agent.session_id = "test_session_001"
    agent._current_turn_id = "turn-1"
    return agent


class TestSyncExternalMemoryForTurn:
    # --- Interrupt guard (the #15218 fix) -------------------------------

    def test_interrupted_turn_does_not_sync(self):
        """The whole point of #15218: even with a final_response and a
        user message, an interrupted turn must NOT reach the memory
        backend."""
        agent = _bare_agent()
        agent._sync_external_memory_for_turn(
            original_user_message="What time is it?",
            final_response="It is 3pm.",  # looks complete — but partial
            interrupted=True,
        )
        agent._memory_manager.sync_all.assert_not_called()
        agent._memory_manager.queue_prefetch_all.assert_not_called()


    # --- Normal completed turn still syncs ------------------------------

    def test_completed_turn_syncs_and_queues_prefetch(self):
        """Regression guard for the positive path: a normal completed
        turn must still trigger both ``sync_all`` AND
        ``queue_prefetch_all`` — otherwise the external memory backend
        never learns about anything and every user complains.
        """
        agent = _bare_agent()
        agent._sync_external_memory_for_turn(
            original_user_message="What's the weather in Paris?",
            final_response="It's sunny and 22°C.",
            interrupted=False,
        )
        agent._memory_manager.sync_all.assert_called_once_with(
            "What's the weather in Paris?", "It's sunny and 22°C.",
            session_id="test_session_001",
        )
        agent._memory_manager.queue_prefetch_all.assert_called_once_with(
            "What's the weather in Paris?",
            session_id="test_session_001",
            turn_id="turn-1",
        )

    def test_completed_turn_keeps_legacy_queue_prefetch_signature(self):
        class _LegacyMemoryManager:
            def __init__(self):
                self.queued = []

            def sync_all(self, *args, **kwargs):
                return None

            def queue_prefetch_all(self, query, *, session_id=""):
                self.queued.append((query, session_id))

        agent = _bare_agent()
        manager = _LegacyMemoryManager()
        agent._memory_manager = manager

        agent._sync_external_memory_for_turn(
            original_user_message="Continue",
            final_response="Done",
            interrupted=False,
        )

        assert manager.queued == [("Continue", "test_session_001")]

    def test_completed_turn_syncs_messages_when_present(self):
        agent = _bare_agent()
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": "{\"command\":\"pytest\"}",
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "name": "terminal",
                "tool_call_id": "call-1",
                "content": "final Hermes-processed output",
            }
        ]

        agent._sync_external_memory_for_turn(
            original_user_message="run tests",
            final_response="tests passed",
            interrupted=False,
            messages=messages,
        )

        agent._memory_manager.sync_all.assert_called_once_with(
            "run tests",
            "tests passed",
            session_id="test_session_001",
            messages=messages,
        )

    def test_completed_skill_turn_keeps_original_message_for_memory_manager(self):
        """Provider-specific query shaping belongs inside the provider.

        The MemoryManager fan-out contract stays raw so non-OpenViking
        providers can decide for themselves whether slash-skill-expanded
        content is useful.
        """
        agent = _bare_agent()
        skill_message = (
            '[IMPORTANT: The user has invoked the "skill-creator" skill, indicating they want '
            "you to follow its instructions. The full skill content is loaded below.]\n\n"
            "# Skill Creator\n\n"
            "Large skill body that must not be searched or embedded.\n\n"
            "The user has provided the following instruction alongside the skill invocation: "
            "make a skill for release triage"
        )

        agent._sync_external_memory_for_turn(
            original_user_message=skill_message,
            final_response="Done.",
            interrupted=False,
        )

        agent._memory_manager.sync_all.assert_called_once_with(
            skill_message,
            "Done.",
            session_id="test_session_001",
        )
        agent._memory_manager.queue_prefetch_all.assert_called_once_with(
            skill_message,
            session_id="test_session_001",
            turn_id="turn-1",
        )
    # --- Edge cases (pre-existing behaviour preserved) ------------------




    # --- Exception safety ----------------------------------------------



    # --- Multimodal content flattening ----------------------------------

    def test_multimodal_user_message_is_flattened(self):
        """A turn with an attached image carries the user message as a
        list of typed parts.  Providers feed the content to regexes
        (sanitize_context), so a raw list raised ``expected string or
        bytes-like object, got 'list'`` and the turn silently never
        synced.  The boundary must flatten to text first."""
        agent = _bare_agent()
        agent._sync_external_memory_for_turn(
            original_user_message=[
                {"type": "text", "text": "what is in this screenshot?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
            final_response="A terminal window showing a stack trace.",
            interrupted=False,
        )
        agent._memory_manager.sync_all.assert_called_once_with(
            "[1 image] what is in this screenshot?",
            "A terminal window showing a stack trace.",
            session_id="test_session_001",
        )
        agent._memory_manager.queue_prefetch_all.assert_called_once_with(
            "[1 image] what is in this screenshot?",
            session_id="test_session_001",
            turn_id="turn-1",
        )


    # --- The specific matrix the reporter asked about ------------------
