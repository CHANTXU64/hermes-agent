"""Nonpersistent review forks preserve the Fork's two-context return contract."""
from types import SimpleNamespace
from unittest.mock import patch
from agent.turn_context import _collect_pre_llm_call_context


def test_nonpersistent_review_skips_hooks_and_returns_both_empty_contexts():
    with patch('hermes_cli.lifecycle.invoke_hook') as hook:
        value = _collect_pre_llm_call_context(
            SimpleNamespace(_persist_disabled=True), effective_task_id='review',
            turn_id='turn', original_user_message='review', messages=[],
            conversation_history=[],
        )
    assert value == ('', '')
    hook.assert_not_called()
