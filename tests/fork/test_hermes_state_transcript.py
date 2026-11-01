"""Keep transcript behavior while retiring the old Fork-only reader switches."""

import pytest

from tests.hermes_state.test_hermes_state import (  # re-export pytest fixtures
    db as db,
    no_foreign_db_holders as no_foreign_db_holders,
)


def test_get_messages_as_conversation_preserves_original_timestamps(db):
    db.create_session(session_id="s_timestamp", source="cli")
    db.append_message("s_timestamp", role="user", content="hello", timestamp=1710000000.0)
    db.append_message("s_timestamp", role="assistant", content="world", timestamp=1710000001.0)

    messages = db.get_messages_as_conversation("s_timestamp")

    assert [(m["role"], m["content"], m["timestamp"]) for m in messages] == [
        ("user", "hello", 1710000000.0),
        ("assistant", "world", 1710000001.0),
    ]
    assert all(m["_db_persisted"] is True for m in messages)


def test_get_messages_as_conversation_keeps_insert_order_for_nonmonotonic_timestamps(db):
    db.create_session(session_id="s_order_id", source="discord")
    db.append_message("s_order_id", role="user", content="first inserted", timestamp=200.0)
    db.append_message("s_order_id", role="assistant", content="first response", timestamp=201.0)
    db.append_message("s_order_id", role="user", content="late inserted but old event ts", timestamp=100.0)
    db.append_message("s_order_id", role="assistant", content="late response", timestamp=101.0)

    messages = db.get_messages_as_conversation("s_order_id")

    assert [m["content"] for m in messages] == [
        "first inserted",
        "first response",
        "late inserted but old event ts",
        "late response",
    ]


def test_get_messages_as_conversation_keeps_tool_pair_order(db):
    db.create_session(session_id="s_tools", source="cli")
    db.append_message("s_tools", role="user", content="check", timestamp=200.0)
    db.append_message(
        "s_tools", role="assistant", content="", timestamp=100.0,
        tool_calls=[{
            "id": "call_order", "type": "function",
            "function": {"name": "probe", "arguments": "{}"},
        }],
    )
    db.append_message(
        "s_tools", role="tool", content="result", timestamp=50.0,
        tool_call_id="call_order", tool_name="probe",
    )
    db.append_message("s_tools", role="assistant", content="done", timestamp=150.0)

    messages = db.get_messages_as_conversation("s_tools")

    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[1]["tool_calls"][0]["id"] == messages[2]["tool_call_id"]


@pytest.mark.parametrize(
    "legacy_kwargs",
    [{"include_timestamps": False}, {"order_by": "timestamp"}],
    ids=["timestamp_switch", "sort_switch"],
)
def test_get_messages_as_conversation_rejects_retired_switches(db, legacy_kwargs):
    db.create_session(session_id="s_retired", source="cli")

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        db.get_messages_as_conversation("s_retired", **legacy_kwargs)
