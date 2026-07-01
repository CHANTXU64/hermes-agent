"""Tests for CLI /retain lineage handling."""

from types import SimpleNamespace
from unittest.mock import MagicMock


def _make_cli_stub(provider, session_db):
    from cli import HermesCLI

    memory_manager = SimpleNamespace(get_provider=lambda name: provider if name == "hindsight" else None)
    cli = SimpleNamespace(
        _pending_resume_sessions=None,
        agent=SimpleNamespace(_memory_manager=memory_manager),
        _session_db=session_db,
        session_id="child-sid",
    )
    cli.process_command = HermesCLI.process_command.__get__(cli, type(cli))
    return cli


def test_cli_retain_uses_persisted_turn_store_not_sessiondb_transcript(capsys):
    provider = MagicMock()
    provider.retain_persisted_session_lineage.return_value = {"queued": True}
    sessions = {
        "root-sid": {"parent_session_id": ""},
        "child-sid": {"parent_session_id": "root-sid"},
    }
    transcripts = {
        "root-sid": [
            {"role": "user", "content": "root upload"},
            {"role": "assistant", "content": "root ack"},
        ],
        "child-sid": [
            {"role": "assistant", "content": "[Recent Summary (d0)]\nsummary"},
            {"role": "user", "content": "child new"},
            {"role": "assistant", "content": "child response"},
        ],
    }
    session_db = SimpleNamespace(
        get_session=lambda sid: sessions.get(sid, {"parent_session_id": ""}),
        get_messages_as_conversation=MagicMock(side_effect=lambda sid, **kwargs: transcripts[sid]),
    )
    cli = _make_cli_stub(provider, session_db)

    assert cli.process_command("/retain") is True

    provider.retain_persisted_session_lineage.assert_called_once_with(
        session_id="child-sid",
        parent_session_id="root-sid",
    )
    provider.retain_conversation_messages.assert_not_called()
    session_db.get_messages_as_conversation.assert_not_called()
    assert "Buffered session turns queued for retain." in capsys.readouterr().out


def test_cli_retain_does_not_fallback_to_flush_when_persisted_store_empty(capsys):
    provider = SimpleNamespace(
        retain_persisted_session_lineage=MagicMock(
            return_value={"queued": False, "message": "No persisted turns to retain."}
        ),
        flush_retained_turns=MagicMock(return_value={"queued": True}),
    )
    session_db = SimpleNamespace(
        get_session=lambda sid: {"parent_session_id": "root-sid"},
        get_messages_as_conversation=MagicMock(return_value=[
            {"role": "user", "content": "[Recent Summary (d0)]\nsummary"},
        ]),
    )
    cli = _make_cli_stub(provider, session_db)

    assert cli.process_command("/retain") is True

    provider.retain_persisted_session_lineage.assert_called_once_with(
        session_id="child-sid",
        parent_session_id="root-sid",
    )
    provider.flush_retained_turns.assert_not_called()
    session_db.get_messages_as_conversation.assert_not_called()
    assert "No persisted turns to retain." in capsys.readouterr().out
