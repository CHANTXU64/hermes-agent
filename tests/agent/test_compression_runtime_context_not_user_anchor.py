"""Continuity recovery rows must never be mistaken for the user's own turn.

Failure shape (measured in state.db: three sessions held two ACTIVE recovery rows): the
compression input filters the hidden ``<hermes-runtime-context user-authored="false">`` row
out, so the engine's result keeps no human turn. ``_ensure_compressed_has_user_turn`` then
scans the original transcript for the newest "real" user row, and because
``_is_real_user_message`` did not recognise the recovery envelope it cloned the previous
generation's recovery row to the summary boundary. The commit then appended the fresh
recovery row at the tail: two copies, the older one stale.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from agent.context_compressor import SUMMARY_PREFIX
from agent.conversation_compression import (
    CompressionCommitFence,
    _ensure_compressed_has_user_turn,
    _is_real_user_message,
)

SOURCE_MARK = 'source="long-task-continuity"'
HUMAN = "请排查压缩问题"


def _recovery(text: str, *, parts: bool = False, hidden: bool = False) -> dict:
    body = f'<hermes-runtime-context user-authored="false" {SOURCE_MARK}>\n{text}\n</hermes-runtime-context>'
    row = {"role": "user", "content": [{"type": "text", "text": body}] if parts else body}
    if hidden:
        row["display_kind"] = "hidden"
    return row


def _tool_round(call_id: str) -> list:
    calls = [{"id": call_id, "type": "function", "function": {"name": "f", "arguments": "{}"}}]
    return [
        {"role": "assistant", "content": None, "tool_calls": calls},
        {"role": "tool", "content": "ok", "tool_call_id": call_id},
    ]


@pytest.mark.parametrize("parts", [False, True], ids=["string", "parts_list"])
@pytest.mark.parametrize("hidden", [False, True], ids=["db_reload", "live_hidden"])
def test_recovery_envelope_is_not_a_real_user_message(parts, hidden):
    assert not _is_real_user_message(_recovery("旧版恢复核心", parts=parts, hidden=hidden))


def test_human_text_quoting_the_envelope_is_still_a_real_user_message():
    quoted = {"role": "user", "content": 'Explain <hermes-runtime-context user-authored="false"> please'}
    assert _is_real_user_message(quoted)
    assert _is_real_user_message({"role": "user", "content": HUMAN})


def test_anchor_skips_recovery_row_and_uses_the_human_turn():
    original = [
        {"role": "user", "content": HUMAN},
        *_tool_round("c1"),
        _recovery("旧版恢复核心"),
        *_tool_round("c2"),
    ]
    compressed = [{"role": "user", "content": f"{SUMMARY_PREFIX}\n\nEarlier work."}, *_tool_round("c2")]

    outcome = _ensure_compressed_has_user_turn(original, compressed)

    assert outcome == "inserted"
    assert sum(SOURCE_MARK in str(m.get("content")) for m in compressed) == 0
    assert sum(m.get("content") == HUMAN for m in compressed) == 1


def test_anchor_never_clones_recovery_row_when_no_human_turn_exists():
    original = [_recovery("旧版恢复核心"), *_tool_round("c1")]
    compressed = [{"role": "user", "content": f"{SUMMARY_PREFIX}\n\nEarlier work."}, *_tool_round("c1")]

    _ensure_compressed_has_user_turn(original, compressed)

    assert sum(SOURCE_MARK in str(m.get("content")) for m in compressed) == 0


def _agent_with_stub_engine(tmp_path, fold):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "E2E_RECOVERY_DUP"
    db.create_session(session_id, source="cli")
    rows = [("user", HUMAN), ("assistant", "ack")]
    for i in range(6):
        rows += [("user", f"bulk q {i} " + "q" * 80), ("assistant", f"bulk a {i} " + "a" * 80)]
    rows.append(("user", HUMAN + "（最新）"))
    for role, content in rows:
        db.append_message(session_id, role, content)
    # Production shape: the previous generation's recovery row sits mid-transcript and the turn
    # is still inside a tool chain, so the engine's tail holds no human turn.
    db.append_message(session_id, "user", _recovery("旧版恢复核心")["content"])
    for call_id in ("t1", "t2"):
        calls = [{"id": call_id, "type": "function", "function": {"name": "f", "arguments": "{}"}}]
        db.append_message(session_id, "assistant", None, tool_calls=calls)
        db.append_message(session_id, "tool", "ok", tool_call_id=call_id)
    messages = db.get_messages_as_conversation(session_id, include_row_ids=True)

    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key", base_url="https://openrouter.ai/api/v1", model="test/model",
            platform="telegram", quiet_mode=True, session_db=db, session_id=session_id,
            skip_context_files=True, skip_memory=True,
        )
    engine = MagicMock()
    engine.compress.return_value = fold
    engine.compression_count = 1
    engine.last_prompt_tokens = 0
    engine.last_completion_tokens = 0
    engine._last_summary_error = None
    engine._last_compress_aborted = False
    engine._last_summary_auth_failure = False
    engine._last_aux_model_failure_model = None
    engine._last_aux_model_failure_error = None
    agent.context_compressor = engine
    return db, session_id, messages, agent


def test_compress_context_keeps_exactly_one_recovery_row(tmp_path):
    """Full ``_compress_context``: the plugin hook supplies a fresh recovery row at commit.
    After commit the durable active set must hold that one row, not it plus a stale clone."""
    fold = [{"role": "user", "content": f"{SUMMARY_PREFIX}\n\nEarlier work."}, *_tool_round("t2")]
    db, session_id, messages, agent = _agent_with_stub_engine(tmp_path, fold)

    def fake_has_hook(name):
        return name == "on_compression_prepare_commit"

    def fake_invoke_hook(name, **_kwargs):
        if name == "on_compression_prepare_commit":
            return [{"source": "long-task-continuity", "context": "新版恢复核心"}]
        return []

    agent._persist_user_message_idx = len(messages) - 1
    with patch("hermes_cli.lifecycle.has_hook", fake_has_hook), \
            patch("hermes_cli.lifecycle.invoke_hook", fake_invoke_hook):
        out_messages, _ = agent._compress_context(
            messages, "sys", approx_tokens=120_000, commit_fence=CompressionCommitFence(),
        )

    live = db.get_messages_as_conversation(session_id)
    for view in (out_messages, live):
        recovery_rows = [m for m in view if SOURCE_MARK in str(m.get("content"))]
        assert len(recovery_rows) == 1, f"expected one recovery row, got {len(recovery_rows)}"
        assert "新版恢复核心" in str(recovery_rows[0]["content"])
        assert "旧版恢复核心" not in str(recovery_rows[0]["content"])
    assert any(m.get("content") == HUMAN + "（最新）" for m in live), "human anchor must survive"
