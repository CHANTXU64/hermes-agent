"""Fork recovery envelope stays separate through real host normalization entrypoints."""
import copy
import pytest
from agent.context_compressor import is_user_originated_turn
from run_agent import AIAgent


def recovery(multimodal=False, metadata=True):
    body = '<hermes-runtime-context user-authored="false" source="long-task-continuity">\nstate\n</hermes-runtime-context>'
    msg = {'role': 'user', 'content': [{'type': 'text', 'text': body}] if multimodal else body}
    if metadata:
        msg['display_kind'] = 'hidden'
    return msg


@pytest.mark.parametrize('metadata', [False, True])
@pytest.mark.parametrize('multimodal', [False, True])
def test_restored_envelope_is_not_a_new_user_turn(metadata, multimodal):
    row = recovery(multimodal, metadata)
    before = copy.deepcopy(row)
    assert not is_user_originated_turn(row)
    assert row == before


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('metadata', [False, True])
def test_durable_and_request_repair_preserve_both_sides(reverse, metadata):
    human = {'role': 'user', 'content': 'real question'}
    rows = [human, recovery(metadata=metadata)]
    if reverse:
        rows.reverse()
    before = copy.deepcopy(rows)
    agent = AIAgent.__new__(AIAgent)
    assert AIAgent._repair_message_sequence(agent, rows) == 0
    assert rows == before
    assert AIAgent._drop_thinking_only_and_merge_users(rows) == before
    assert rows == before


def test_ordinary_user_text_keeps_existing_authority_and_merge_behavior():
    human = {'role': 'user', 'content': 'Explain <hermes-runtime-context user-authored="false"> please'}
    assert is_user_originated_turn(human)
    rows = [human.copy(), {'role': 'user', 'content': 'follow-up'}]
    assert AIAgent._repair_message_sequence(AIAgent.__new__(AIAgent), rows) == 1
    assert rows[0]['content'] == human['content'] + '\n\nfollow-up'
