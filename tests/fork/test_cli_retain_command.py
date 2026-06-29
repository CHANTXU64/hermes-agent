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


def test_cli_retain_uses_session_transcript_lineage_when_available(capsys):
    provider = MagicMock()
    provider.retain_conversation_messages.return_value = {"queued": True}
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

    provider.retain_conversation_messages.assert_called_once()
    messages = provider.retain_conversation_messages.call_args.args[0]
    assert [m["_session_id"] for m in messages] == [
        "root-sid", "root-sid", "child-sid", "child-sid", "child-sid"
    ]
    assert session_db.get_messages_as_conversation.call_args_list[0].kwargs == {"include_timestamps": True, "order_by": "id"}
    assert session_db.get_messages_as_conversation.call_args_list[1].kwargs == {"include_timestamps": True, "order_by": "id"}
    assert provider.retain_conversation_messages.call_args.kwargs == {
        "session_id": "child-sid",
        "parent_session_id": "root-sid",
    }
    assert "Buffered session turns queued for retain." in capsys.readouterr().out
