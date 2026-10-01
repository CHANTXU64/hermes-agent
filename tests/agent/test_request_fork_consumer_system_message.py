"""Request-fork compression consumer must pass the turn's ORIGINAL ``system_message``.

``_run_api_retry_loop`` consumes a deferred request-fork compression. It must hand
``_compress_context`` the caller's raw ``system_message`` (the ephemeral prompt addition), never
the fully assembled ``active_system_prompt``: the commit boundary rebuilds the prompt as
``stable + system_message + volatile``, so feeding the assembled prompt back in nests a complete
copy of the previous prompt inside the new one, one more layer per compression.
"""
import types
import unittest

import agent.conversation_loop as cl
from agent.conversation_compression import _rebuild_system_prompt_at_boundary


class _StopBeforeApiCall(BaseException):
    """Ends the retry loop right after the request-fork consumer branch."""


class _Verdict:
    action = None
    result = None


def _run_consumer_branch(*, system_message, active_system_prompt, compress):
    """Drive the REAL consumer branch of ``_run_api_retry_loop`` up to the API call."""
    def run_phase(fn, agent, state, **extra):
        if fn is cl.perform_api_call:
            raise _StopBeforeApiCall()
        return _Verdict()

    agent = types.SimpleNamespace(_compression_request_fork_pending=True, _compress_context=compress)
    state = types.SimpleNamespace(
        retry_count=0, max_retries=3, api_kwargs={}, messages=[{"role": "user", "content": "hi"}],
        current_turn_user_idx=0, active_system_prompt=active_system_prompt, system_message=system_message,
        request_pressure_tokens=1, effective_task_id="task",
    )
    saved = (cl._run_phase, cl.freeze_request_for_compression, cl.build_adopt_rematerializer)
    cl._run_phase = run_phase
    cl.freeze_request_for_compression = lambda *a, **k: object()
    cl.build_adopt_rematerializer = lambda *a, **k: (lambda messages: None)
    try:
        cl._run_api_retry_loop(agent, state)
    except _StopBeforeApiCall:
        pass
    finally:
        cl._run_phase, cl.freeze_request_for_compression, cl.build_adopt_rematerializer = saved


class RequestForkConsumerSystemMessageTests(unittest.TestCase):
    def _received_system_message(self, *, system_message, active_system_prompt):
        seen = {}

        def compress(messages, passed_system_message, **kwargs):
            seen["system_message"] = passed_system_message
            return messages, "REBUILT"

        _run_consumer_branch(
            system_message=system_message, active_system_prompt=active_system_prompt, compress=compress,
        )
        return seen["system_message"]

    def test_turn_without_ephemeral_prompt_passes_none_not_assembled_prompt(self):
        received = self._received_system_message(
            system_message=None, active_system_prompt="ASSEMBLED PROMPT stable+volatile",
        )
        self.assertIsNone(received)

    def test_turn_ephemeral_prompt_is_passed_through_unchanged(self):
        received = self._received_system_message(
            system_message="EPHEMERAL ADDITION", active_system_prompt="ASSEMBLED PROMPT stable+volatile",
        )
        self.assertEqual(received, "EPHEMERAL ADDITION")

    def test_repeated_compression_boundaries_keep_the_prompt_stable(self):
        """What the consumer hands over is what the commit boundary rebuilds from."""
        marker = "# Hermes Agent Persona"
        rebuilt_prompts = []

        class _Agent:
            _cached_system_prompt = None
            _cached_system_prompt_static = None
            session_id = "session"
            tools = None
            valid_tool_names = frozenset()
            _use_prompt_caching = False

            def _invalidate_system_prompt(self):
                self._cached_system_prompt = None

            def _build_system_prompt(self, system_message=None):
                parts = [marker + " stable"]
                if system_message is not None:
                    parts.append(system_message)
                parts.append("## Skills volatile")
                return "\n\n".join(parts)

        agent = _Agent()
        agent._cached_system_prompt = agent._build_system_prompt(None)
        state_prompt = agent._cached_system_prompt

        for _ in range(3):
            holder = {}

            def commit_boundary(messages, passed_system_message, **kwargs):
                holder["prompt"] = _rebuild_system_prompt_at_boundary(agent, passed_system_message)
                return messages, holder["prompt"]

            _run_consumer_branch(system_message=None, active_system_prompt=state_prompt, compress=commit_boundary)
            state_prompt = holder["prompt"]
            rebuilt_prompts.append(state_prompt)

        for prompt in rebuilt_prompts:
            self.assertEqual(prompt.count(marker), 1, "prompt nested a copy of the previous prompt")
        self.assertEqual(len(set(rebuilt_prompts)), 1, "prompt changed across repeated compressions")


if __name__ == "__main__":
    unittest.main()
