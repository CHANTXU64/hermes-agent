"""Fork-owned SessionDB transcript ordering/timestamp regressions."""

from tests.test_hermes_state import db as db  # re-export pytest fixture


def test_get_messages_as_conversation_can_omit_timestamps(db):
    db.create_session(session_id="s_timestamp_opt", source="cli")

    db.append_message(
        "s_timestamp_opt", role="user", content="hello", timestamp=1710000000.0
    )
    db.append_message(
        "s_timestamp_opt",
        role="assistant",
        content="world",
        timestamp=1710000001.0,
    )

    default_messages = db.get_messages_as_conversation("s_timestamp_opt")
    assert default_messages == [
        {"role": "user", "content": "hello", "timestamp": 1710000000.0},
        {"role": "assistant", "content": "world", "timestamp": 1710000001.0},
    ]

    without_timestamps = db.get_messages_as_conversation(
        "s_timestamp_opt", include_timestamps=False
    )
    assert without_timestamps == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]


def test_get_messages_as_conversation_can_order_by_insert_id(db):
    db.create_session(session_id="s_order_id", source="discord")
    db.append_message("s_order_id", role="user", content="first inserted", timestamp=200.0)
    db.append_message("s_order_id", role="assistant", content="first response", timestamp=201.0)
    db.append_message("s_order_id", role="user", content="late inserted but old event ts", timestamp=100.0)
    db.append_message("s_order_id", role="assistant", content="late response", timestamp=101.0)

    default_messages = db.get_messages_as_conversation("s_order_id")
    id_ordered_messages = db.get_messages_as_conversation("s_order_id", order_by="id")
    timestamp_ordered_messages = db.get_messages_as_conversation(
        "s_order_id", order_by="timestamp"
    )

    assert [m["content"] for m in default_messages] == [
        "first inserted",
        "first response",
        "late inserted but old event ts",
        "late response",
    ]
    assert [m["content"] for m in id_ordered_messages] == [
        "first inserted",
        "first response",
        "late inserted but old event ts",
        "late response",
    ]
    assert [m["content"] for m in timestamp_ordered_messages] == [
        "late inserted but old event ts",
        "late response",
        "first inserted",
        "first response",
    ]
