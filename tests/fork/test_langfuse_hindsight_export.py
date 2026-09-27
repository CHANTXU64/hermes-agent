from __future__ import annotations

import importlib.util

import pytest
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "fork_features"
    / "hindsight_retain"
    / "langfuse_hindsight_export.py"
)


def load_script_module(tmp_path: Path):
    spec = importlib.util.spec_from_file_location(
        "langfuse_hindsight_export", MODULE_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ConnectionProbe:
    def __init__(self, connection, queries: list[str]):
        self._connection = connection
        self._queries = queries

    @property
    def row_factory(self):
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._connection.row_factory = value

    def execute(self, query, parameters=()):
        self._queries.append(str(query).strip())
        return self._connection.execute(query, parameters)

    def close(self):
        self._connection.close()

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._connection.__exit__(exc_type, exc_value, traceback)


def _capture_sqlite_connections(monkeypatch, module):
    original_connect = module.sqlite3.connect
    calls = []

    def connect(database, *args, **kwargs):
        queries: list[str] = []
        calls.append(
            {
                "database": str(database),
                "uri": kwargs.get("uri"),
                "queries": queries,
            }
        )
        return _ConnectionProbe(original_connect(database, *args, **kwargs), queries)

    monkeypatch.setattr(module.sqlite3, "connect", connect)
    return calls


def test_local_retain_summary_opens_legacy_ledger_query_only(tmp_path, monkeypatch):
    module = load_script_module(tmp_path)
    ledger = tmp_path / "retain turns.sqlite3"
    with sqlite3.connect(ledger) as connection:
        connection.executescript(
            """
            CREATE TABLE hindsight_retain_submissions (
                id INTEGER PRIMARY KEY,
                bank_id TEXT,
                document_id TEXT,
                update_mode TEXT,
                content_json TEXT,
                status TEXT,
                queued_at TEXT,
                completed_at TEXT,
                error TEXT
            );
            INSERT INTO hindsight_retain_submissions
                (bank_id, document_id, update_mode, content_json, status)
            VALUES ('Hermes', 'session-read-only', 'replace', '{}', 'completed');
            """
        )

    calls = _capture_sqlite_connections(monkeypatch, module)
    summary = module.local_retain_summary("session-read-only", ledger)

    assert summary["row_count"] == 1
    assert len(calls) == 1
    assert calls[0]["uri"] is True
    assert "mode=ro" in calls[0]["database"]
    assert calls[0]["queries"][0].upper() == "PRAGMA QUERY_ONLY=ON"


def test_undo_evidence_state_db_enforces_query_only(tmp_path, monkeypatch):
    module = load_script_module(tmp_path)
    state_db = tmp_path / "state db.sqlite3"
    with sqlite3.connect(state_db) as connection:
        connection.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, rewind_count INTEGER)"
        )
        connection.execute(
            "INSERT INTO sessions VALUES ('session-read-only', 0)"
        )

    calls = _capture_sqlite_connections(monkeypatch, module)
    result = module.load_undo_filter("session-read-only", state_db)

    assert result["status"] == "not_applicable"
    assert len(calls) == 1
    assert calls[0]["uri"] is True
    assert "mode=ro" in calls[0]["database"]
    assert calls[0]["queries"][0].upper() == "PRAGMA QUERY_ONLY=ON"


def hermes_turn(
    observation_id: str,
    start: str,
    end: str,
    user_content: str,
    assistant_content: str,
) -> dict:
    return {
        "id": observation_id,
        "type": "CHAIN",
        "name": "Hermes turn",
        "startTime": start,
        "endTime": end,
        "input": {"role": "user", "content": user_content},
        "output": {"content": assistant_content, "tool_calls": []},
    }


def test_failed_turn_retried_successfully_is_not_duplicated(tmp_path):
    """A 403 turn and its retry share one user message; only the retry survives.

    Regression for the session where an upstream auth failure was traced as its
    own turn, making the user's message appear twice in the candidate.
    """
    module = load_script_module(tmp_path)
    session_id = "session-failed-retry"
    failed_turn = {
        "id": "turn-failed",
        "type": "CHAIN",
        "name": "Hermes turn",
        "startTime": "2026-09-21T00:19:29Z",
        "endTime": "2026-09-21T00:19:35Z",
        "input": {"role": "user", "content": "你拉下最新的代码"},
        "output": {
            "error": {
                "error": True,
                "error_type": "PermissionDeniedError",
                "status_code": 403,
                "retryable": False,
            }
        },
    }
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id},
                "observations": [
                    failed_turn,
                    hermes_turn(
                        "turn-retry",
                        "2026-09-21T00:19:36Z",
                        "2026-09-21T00:19:50Z",
                        "你拉下最新的代码",
                        "已拉到最新。",
                    ),
                ],
            }
        ],
    }

    candidate = module.build_candidate_document(export, session_id)
    messages = [message for turn in candidate["turns"] for message in turn]

    assert messages == [
        {
            "role": "user",
            "content": "User: 你拉下最新的代码",
            "timestamp": "2026-09-21T00:19:36Z",
        },
        {
            "role": "assistant",
            "content": "Assistant: 已拉到最新。",
            "timestamp": "2026-09-21T00:19:50Z",
        },
    ]


def test_distant_same_text_success_does_not_erase_earlier_failed_event(tmp_path):
    """Equal text weeks later is a new user event, not proof of a retry."""
    module = load_script_module(tmp_path)
    session_id = "session-distant-repeat"
    failed_turn = {
        "id": "turn-failed-old",
        "type": "CHAIN",
        "name": "Hermes turn",
        "startTime": "2026-09-01T00:00:00Z",
        "endTime": "2026-09-01T00:00:05Z",
        "input": {"role": "user", "content": "继续"},
        "output": {
            "error": {
                "error": True,
                "error_type": "APIConnectionError",
                "retryable": True,
            }
        },
    }
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id},
                "observations": [
                    failed_turn,
                    hermes_turn(
                        "turn-success-later",
                        "2026-09-21T00:00:00Z",
                        "2026-09-21T00:00:05Z",
                        "继续",
                        "已继续。",
                    ),
                ],
            }
        ],
    }

    candidate = module.build_candidate_document(export, session_id)
    user_messages = [
        message["content"]
        for turn in candidate["turns"]
        for message in turn
        if message["role"] == "user"
    ]

    assert user_messages == ["User: 继续", "User: 继续"]


def test_failed_turn_without_retry_keeps_the_user_message(tmp_path):
    """An unanswered failure is the only record of that message — keep it.

    Dropping every failed turn would silently lose the user's words whenever a
    request failed and was never re-sent.
    """
    module = load_script_module(tmp_path)
    session_id = "session-failed-final"
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id},
                "observations": [
                    hermes_turn(
                        "turn-ok",
                        "2026-09-21T00:10:00Z",
                        "2026-09-21T00:10:05Z",
                        "第一个问题",
                        "第一个回答",
                    ),
                    {
                        "id": "turn-failed-final",
                        "type": "CHAIN",
                        "name": "Hermes turn",
                        "startTime": "2026-09-21T00:11:00Z",
                        "endTime": "2026-09-21T00:11:06Z",
                        "input": {"role": "user", "content": "这条永远没被回答"},
                        "output": {
                            "error": {
                                "error": True,
                                "error_type": "APIConnectionError",
                                "retryable": True,
                            }
                        },
                    },
                ],
            }
        ],
    }

    candidate = module.build_candidate_document(export, session_id)
    user_messages = [
        message["content"]
        for turn in candidate["turns"]
        for message in turn
        if message["role"] == "user"
    ]

    assert "User: 这条永远没被回答" in user_messages


def test_build_candidate_document_orders_real_turns_and_strips_model_note(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-main"
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id},
                "observations": [
                    hermes_turn(
                        "turn-2",
                        "2026-08-24T02:00:00Z",
                        "2026-08-24T02:01:00Z",
                        "第二条用户消息",
                        "第二条最终回复",
                    ),
                    hermes_turn(
                        "turn-1",
                        "2026-08-24T01:00:00Z",
                        "2026-08-24T01:01:00Z",
                        "[Note: model was just switched from A to B. Adjust your self-identification accordingly.]\n\n第一条用户消息",
                        "第一条最终回复",
                    ),
                ],
            },
            {
                "metadata": {"task_id": "background-task"},
                "observations": [
                    hermes_turn(
                        "background-turn",
                        "2026-08-24T00:00:00Z",
                        "2026-08-24T00:01:00Z",
                        "不属于主会话",
                        "不应出现在候选Document",
                    )
                ],
            },
        ],
    }

    candidate = module.build_candidate_document(export, session_id)

    assert candidate["schema_version"] == "hindsight-conversation-document-v1"
    assert candidate["session_id"] == session_id
    assert candidate["document_id"] == session_id
    assert candidate["turns"] == [
        [
            {
                "role": "user",
                "content": "User: 第一条用户消息",
                "timestamp": "2026-08-24T01:00:00Z",
            },
            {
                "role": "assistant",
                "content": "Assistant: 第一条最终回复",
                "timestamp": "2026-08-24T01:01:00Z",
            },
        ],
        [
            {
                "role": "user",
                "content": "User: 第二条用户消息",
                "timestamp": "2026-08-24T02:00:00Z",
            },
            {
                "role": "assistant",
                "content": "Assistant: 第二条最终回复",
                "timestamp": "2026-08-24T02:01:00Z",
            },
        ],
    ]


def test_candidate_cutoff_excludes_messages_after_retain_request(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-cutoff"
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id},
                "observations": [
                    hermes_turn(
                        "turn-crossing-cutoff",
                        "2026-08-26T01:00:00Z",
                        "2026-08-26T01:02:00Z",
                        "触发前用户消息",
                        "触发后才完成的回复",
                    ),
                    hermes_turn(
                        "turn-after-cutoff",
                        "2026-08-26T01:03:00Z",
                        "2026-08-26T01:04:00Z",
                        "触发后用户消息",
                        "触发后回复",
                    ),
                ],
            }
        ],
    }

    candidate = module.build_candidate_document(
        export,
        session_id,
        cutoff_at=datetime(2026, 8, 26, 1, 1, tzinfo=timezone.utc),
    )

    assert candidate["turns"] == [
        [
            {
                "role": "user",
                "content": "User: 触发前用户消息",
                "timestamp": "2026-08-26T01:00:00Z",
            }
        ]
    ]
    assert candidate["audit"]["cutoff_at"] == "2026-08-26T01:01:00+00:00"
    assert candidate["audit"]["cutoff_filtered_message_count"] == 3


def test_cutoff_excludes_future_state_rows_from_undo_evidence(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-cutoff-undo"
    state_db = tmp_path / "state.db"
    with sqlite3.connect(state_db) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                rewind_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL,
                active INTEGER NOT NULL DEFAULT 1,
                compacted INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.execute("INSERT INTO sessions VALUES (?, 1)", (session_id,))
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, 'user', ?, ?, ?, 0)",
            [
                (1, session_id, "相同消息", 1000.0, 0),
                (2, session_id, "相同消息", 2000.0, 1),
            ],
        )

    undo_filter = module.load_undo_filter(
        session_id,
        state_db,
        cutoff_at=datetime.fromtimestamp(1500, tz=timezone.utc),
    )

    assert [row["message_id"] for row in undo_filter["rewound_users"]] == [1]
    assert undo_filter["active_same_text_users"] == []


def test_build_candidate_document_renders_clarify_roundtrip(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-clarify"
    chain = hermes_turn(
        "turn-1",
        "2026-08-24T01:00:00Z",
        "2026-08-24T01:04:00Z",
        "请帮我修改配置",
        "我根据你的回答，没有修改配置。",
    )
    clarify = {
        "id": "clarify-1",
        "parentObservationId": "turn-1",
        "type": "TOOL",
        "name": "Tool: clarify",
        "startTime": "2026-08-24T01:01:00Z",
        "endTime": "2026-08-24T01:03:00Z",
        "input": {
            "question": "是否执行这个修改？",
            "choices": ["执行", "不执行"],
        },
        "output": {
            "question": "是否执行这个修改？",
            "choices_offered": ["执行", "不执行"],
            "user_response": "先不要执行",
        },
    }
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id},
                "observations": [chain, clarify],
            }
        ],
    }

    candidate = module.build_candidate_document(export, session_id)

    assert candidate["turns"] == [
        [
            {
                "role": "user",
                "content": "User: 请帮我修改配置",
                "timestamp": "2026-08-24T01:00:00Z",
            },
            {
                "role": "assistant",
                "content": (
                    "Assistant: 是否执行这个修改？\n\n"
                    "Choices offered:\n- 执行\n- 不执行"
                ),
                "timestamp": "2026-08-24T01:01:00Z",
            },
            {
                "role": "user",
                "content": "User: 先不要执行",
                "timestamp": "2026-08-24T01:03:00Z",
            },
            {
                "role": "assistant",
                "content": "Assistant: 我根据你的回答，没有修改配置。",
                "timestamp": "2026-08-24T01:04:00Z",
            },
        ]
    ]


def test_build_candidate_document_keeps_user_message_added_during_a_turn(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-oob"
    old_turn = hermes_turn(
        "turn-old",
        "2026-08-24T00:00:00Z",
        "2026-08-24T00:01:00Z",
        "旧用户消息",
        "旧最终回复",
    )
    active_turn = hermes_turn(
        "turn-active",
        "2026-08-24T01:00:00Z",
        "2026-08-24T01:04:00Z",
        "开始调查",
        "按你的插话直接回答",
    )
    generation_before_oob = {
        "id": "generation-1",
        "parentObservationId": "turn-active",
        "type": "GENERATION",
        "name": "LLM call 1",
        "startTime": "2026-08-24T01:01:00Z",
        "input": [
            {"role": "user", "content": "旧用户消息"},
            {"role": "assistant", "content": "旧最终回复"},
            {"role": "user", "content": "开始调查"},
        ],
    }
    generation_after_oob = {
        "id": "generation-2",
        "parentObservationId": "turn-active",
        "type": "GENERATION",
        "name": "LLM call 2",
        "startTime": "2026-08-24T01:02:00Z",
        "input": [
            {"role": "user", "content": "旧用户消息"},
            {"role": "assistant", "content": "旧最终回复"},
            {"role": "user", "content": "开始调查"},
            {"role": "user", "content": "别查了，直接回答"},
        ],
    }
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id},
                "observations": [
                    active_turn,
                    generation_after_oob,
                    old_turn,
                    generation_before_oob,
                ],
            }
        ],
    }

    candidate = module.build_candidate_document(export, session_id)

    assert candidate["turns"][1] == [
        {
            "role": "user",
            "content": "User: 开始调查",
            "timestamp": "2026-08-24T01:00:00Z",
        },
        {
            "role": "user",
            "content": "User: 别查了，直接回答",
            "timestamp": "2026-08-24T01:02:00Z",
        },
        {
            "role": "assistant",
            "content": "Assistant: 按你的插话直接回答",
            "timestamp": "2026-08-24T01:04:00Z",
        },
    ]


def test_build_candidate_document_filters_runtime_user_inputs(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-runtime-filter"
    background_turn = hermes_turn(
        "turn-background",
        "2026-08-24T01:00:00Z",
        "2026-08-24T01:01:00Z",
        "[ASYNC DELEGATION BATCH COMPLETE — batch-1] 后台任务已经结束",
        "这是实际发给用户的后台任务最终报告",
    )
    real_turn = hermes_turn(
        "turn-real",
        "2026-08-24T02:00:00Z",
        "2026-08-24T02:03:00Z",
        (
            "真实用户问题\n\n"
            "[Your active task list was preserved across context compression]\n"
            "- internal task\n\n"
            "## Hermes-LCM Recall Policy\n"
            "internal policy"
        ),
        "真实问题的最终回复",
    )
    generation = {
        "id": "generation-runtime",
        "parentObservationId": "turn-real",
        "type": "GENERATION",
        "name": "LLM call 1",
        "startTime": "2026-08-24T02:01:00Z",
        "input": [
            {"role": "user", "content": "真实用户问题"},
            {
                "role": "user",
                "content": (
                    '<hermes-runtime-context user-authored="false" '
                    'source="long-task-continuity">\n'
                    "绝不能归因为用户原话\n"
                    "</hermes-runtime-context>"
                ),
            },
            {"role": "user", "content": "## Hermes-LCM Recall Policy\ninternal"},
            {
                "role": "user",
                "content": "[IMPORTANT: Background process proc-1 completed normally]",
            },
        ],
    }
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id},
                "observations": [background_turn, real_turn, generation],
            }
        ],
    }

    candidate = module.build_candidate_document(export, session_id)

    assert candidate["turns"] == [
        [
            {
                "role": "assistant",
                "content": "Assistant: 这是实际发给用户的后台任务最终报告",
                "timestamp": "2026-08-24T01:01:00Z",
            }
        ],
        [
            {
                "role": "user",
                "content": "User: 真实用户问题",
                "timestamp": "2026-08-24T02:00:00Z",
            },
            {
                "role": "assistant",
                "content": "Assistant: 真实问题的最终回复",
                "timestamp": "2026-08-24T02:03:00Z",
            },
        ],
    ]
    rendered = str(candidate["turns"])
    assert "ASYNC DELEGATION" not in rendered
    assert "Hermes-LCM Recall Policy" not in rendered
    assert "IMPORTANT: Background process" not in rendered
    assert "active task list" not in rendered
    assert "绝不能归因为用户原话" not in rendered


def test_candidate_document_content_is_deterministic(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-deterministic"
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id},
                "observations": [
                    hermes_turn(
                        "turn-1",
                        "2026-08-24T01:00:00Z",
                        "2026-08-24T01:01:00Z",
                        "用户消息",
                        "最终回复",
                    )
                ],
            }
        ],
    }

    first = module.build_candidate_document(export, session_id)
    second = module.build_candidate_document(export, session_id)

    assert first == second
    assert json.loads(first["document_content"]) == first["turns"]
    assert first["document_content_sha256"] == hashlib.sha256(
        first["document_content"].encode("utf-8")
    ).hexdigest()


def test_exporter_reads_hindsight_bank_from_config(tmp_path: Path) -> None:
    module = load_script_module(tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"bank_id": "ConfiguredBank"}))

    assert module.load_hindsight_bank_id(config_path) == "ConfiguredBank"


def test_cli_writes_candidate_document_without_hindsight_write(tmp_path, monkeypatch):
    module = load_script_module(tmp_path)
    session_id = "session-cli"
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id},
                "observations": [
                    hermes_turn(
                        "turn-1",
                        "2026-08-24T01:00:00Z",
                        "2026-08-24T01:01:00Z",
                        "用户消息",
                        "最终回复",
                    )
                ],
            }
        ],
    }
    output_dir = tmp_path / "output"
    monkeypatch.setattr(module, "export_langfuse", lambda *_: export)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "langfuse_hindsight_export.py",
            "--session-id",
            session_id,
            "--output-dir",
            str(output_dir),
            "--cutoff-at",
            "2026-08-24T01:00:30Z",
            "--skip-hindsight",
            "--sqlite-path",
            str(tmp_path / "missing.sqlite3"),
            "--state-db-path",
            str(tmp_path / "missing-state.db"),
        ],
    )

    assert module.main() == 0

    candidate_path = output_dir / f"candidate_document_{session_id}.json"
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert candidate["document_id"] == session_id
    assert [message["role"] for message in candidate["turns"][0]] == ["user"]
    assert candidate["audit"]["cutoff_at"] == "2026-08-24T01:00:30+00:00"
    assert candidate["audit"]["undo_filter_status"] == "unknown"
    assert candidate["audit"]["undo_filter_reason"] == "state_db_missing"
    assert str(candidate_path) in manifest["files"]
    assert manifest["read_only"] is True


def test_cli_applies_undo_filter_from_state_db(tmp_path, monkeypatch):
    module = load_script_module(tmp_path)
    session_id = "session-cli-undo"
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id},
                "observations": [
                    hermes_turn(
                        "turn-undone",
                        "2026-08-25T02:00:00Z",
                        "2026-08-25T02:01:00Z",
                        "撤销这条",
                        "撤销后的回复",
                    )
                ],
            }
        ],
    }
    state_db = tmp_path / "state.db"
    with sqlite3.connect(state_db) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                rewind_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL,
                active INTEGER NOT NULL DEFAULT 1,
                compacted INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.execute("INSERT INTO sessions(id, rewind_count) VALUES (?, 1)", (session_id,))
        conn.execute(
            """INSERT INTO messages(
                   id, session_id, role, content, timestamp, active, compacted
               ) VALUES (?, ?, 'user', ?, ?, 0, 0)""",
            (1, session_id, "撤销这条", 1787623205.0),
        )

    output_dir = tmp_path / "output-undo"
    monkeypatch.setattr(module, "export_langfuse", lambda *_: export)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "langfuse_hindsight_export.py",
            "--session-id",
            session_id,
            "--output-dir",
            str(output_dir),
            "--skip-hindsight",
            "--sqlite-path",
            str(tmp_path / "missing-retain.sqlite3"),
            "--state-db-path",
            str(state_db),
        ],
    )

    assert module.main() == 0

    candidate = json.loads(
        (output_dir / f"candidate_document_{session_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert candidate["turns"] == []
    assert candidate["audit"]["undo_filter_status"] == "checked"
    assert candidate["audit"]["undo_matched_user_count"] == 1


def test_interrupted_user_correction_is_clean_and_not_duplicated(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-interrupted"
    correction = "别查了，你直接回答"
    chain = hermes_turn(
        "turn-1",
        "2026-08-24T01:00:00Z",
        "2026-08-24T01:02:00Z",
        (
            "[Context from the interrupted assistant response]\n"
            "[This response was interrupted by a user correction.]\n\n"
            f"{correction}"
        ),
        "最终回复",
    )
    generation = {
        "id": "generation-1",
        "parentObservationId": "turn-1",
        "type": "GENERATION",
        "name": "LLM call 1",
        "startTime": "2026-08-24T01:01:00Z",
        "input": [{"role": "user", "content": correction}],
    }
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id},
                "observations": [chain, generation],
            }
        ],
    }

    candidate = module.build_candidate_document(export, session_id)

    user_messages = [
        message["content"]
        for turn in candidate["turns"]
        for message in turn
        if message["role"] == "user"
    ]
    assert user_messages == [f"User: {correction}"]


def test_clean_user_content_unwraps_new_message_and_drops_runtime_only(tmp_path):
    module = load_script_module(tmp_path)
    wrapper = (
        "[System note: A new message has arrived. The conversation history contains "
        "pending tool outputs from an interrupted turn. IGNORE those pending results. "
        "Address the user's NEW message below FIRST. Do NOT re-execute old tool calls "
        "from the history.]\n\n"
    )
    model_note = (
        "[Note: model was just switched from gpt-5.6-sol to gpt-5.6-sol via "
        "openai-api. Adjust your self-identification accordingly.]\n\n"
    )

    assert module._clean_user_content(
        wrapper
        + model_note
        + "继续\n\n## Hermes-LCM Recall Policy\ninternal policy\n\n<memory-context>hidden"
    ) == "继续"
    assert module._clean_user_content(
        wrapper + "[IMPORTANT: 3 background processes completed for this session.]"
    ) == ""
    assert module._clean_user_content(
        "You just executed tool calls but returned an empty response. Please process them."
    ) == ""
    assert module._clean_user_content("[user did not respond within 15m]") == ""


def test_clarify_timeout_is_not_rendered_as_user_response(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-clarify-timeout"
    chain = hermes_turn(
        "turn-1",
        "2026-08-24T01:00:00Z",
        "2026-08-24T01:02:00Z",
        "用户问题",
        "等待用户决定",
    )
    clarify = {
        "id": "clarify-timeout",
        "parentObservationId": "turn-1",
        "type": "TOOL",
        "name": "Tool: clarify",
        "startTime": "2026-08-24T01:00:30Z",
        "endTime": "2026-08-24T01:01:30Z",
        "input": {"question": "请选择", "choices": ["甲", "乙"]},
        "output": {"user_response": "[user did not respond within 15m]"},
    }
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id},
                "observations": [chain, clarify],
            }
        ],
    }

    candidate = module.build_candidate_document(export, session_id)

    contents = [message["content"] for turn in candidate["turns"] for message in turn]
    assert "Assistant: 请选择\n\nChoices offered:\n- 甲\n- 乙" in contents
    assert all("user did not respond" not in content for content in contents)


def test_sanitized_langfuse_source_marks_candidate_as_not_lossless(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-sanitized"
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id, "capture_mode": "sanitized"},
                "observations": [
                    hermes_turn(
                        "turn-1",
                        "2026-08-24T01:00:00Z",
                        "2026-08-24T01:01:00Z",
                        "用户消息",
                        "max_output_tokens: ***",
                    )
                ],
            }
        ],
    }

    candidate = module.build_candidate_document(export, session_id)

    assert candidate["audit"]["source_capture_modes"] == ["sanitized"]
    assert candidate["audit"]["source_is_lossless"] is False
    assert candidate["audit"]["completeness_status"] == "not_guaranteed_sanitized_source"


def test_build_candidate_document_preserves_multimodal_user_context(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-multimodal"
    chain = hermes_turn(
        "turn-image",
        "2026-08-25T01:00:00Z",
        "2026-08-25T01:01:00Z",
        [
            {
                "type": "input_text",
                "text": (
                    "[Note: model was just switched from A to B via Test. "
                    "Adjust your self-identification accordingly.]\n\n"
                    "请分析这张图片\n\n"
                    "[Image attached at: /Users/robot/.hermes/cache/images/sample.jpg]"
                ),
            },
            {"type": "input_image", "image_url": {"url": "data:image/jpeg;base64,hidden"}},
            {
                "type": "input_text",
                "text": "\n\n## Hermes-LCM Recall Policy\ninternal policy",
            },
        ],
        "图片分析结果",
    )
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id},
                "observations": [chain],
            }
        ],
    }

    candidate = module.build_candidate_document(export, session_id)

    assert candidate["turns"] == [
        [
            {
                "role": "user",
                "content": "User: 请分析这张图片\n\n[The user sent an image.]",
                "timestamp": "2026-08-25T01:00:00Z",
            },
            {
                "role": "assistant",
                "content": "Assistant: 图片分析结果",
                "timestamp": "2026-08-25T01:01:00Z",
            },
        ]
    ]
    assert "sample.jpg" not in candidate["document_content"]
    assert "Hermes-LCM Recall Policy" not in candidate["document_content"]
    assert "model was just switched" not in candidate["document_content"]


def test_build_candidate_document_keeps_orphan_clarify_in_time_order(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-orphan-clarify"
    before = hermes_turn(
        "turn-before",
        "2026-08-25T01:00:00Z",
        "2026-08-25T01:00:30Z",
        "先调查",
        "已完成只读调查",
    )
    after = hermes_turn(
        "turn-after",
        "2026-08-25T01:02:00Z",
        "2026-08-25T01:03:00Z",
        "继续执行",
        "执行完成",
    )
    orphan_clarify = {
        "id": "clarify-orphan",
        "parentObservationId": "missing-parent",
        "type": "TOOL",
        "name": "Tool: clarify",
        "startTime": "2026-08-25T01:01:00Z",
        "endTime": "2026-08-25T01:01:30Z",
        "input": {"question": "是否继续？", "choices": ["继续", "停止"]},
        "output": {
            "question": "是否继续？",
            "choices_offered": ["继续", "停止"],
            "user_response": "继续",
        },
    }
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id},
                "observations": [after, orphan_clarify, before],
            }
        ],
    }

    candidate = module.build_candidate_document(export, session_id)

    assert candidate["turns"][1] == [
        {
            "role": "assistant",
            "content": (
                "Assistant: 是否继续？\n\n"
                "Choices offered:\n- 继续\n- 停止"
            ),
            "timestamp": "2026-08-25T01:01:00Z",
        },
        {
            "role": "user",
            "content": "User: 继续",
            "timestamp": "2026-08-25T01:01:30Z",
        },
    ]
    assert [turn[0]["content"] for turn in candidate["turns"]] == [
        "User: 先调查",
        "Assistant: 是否继续？\n\nChoices offered:\n- 继续\n- 停止",
        "User: 继续执行",
    ]


def test_build_candidate_document_recovers_trailing_voice_after_runtime_notice(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-runtime-voice"
    first_voice = (
        "[The user sent a voice message~ Here's what they said: "
        '"脚本必须从干净首页运行。"]'
    )
    second_voice = (
        "[The user sent a voice message~ Here's what they said: "
        '"不要乱改，要把所有情况考虑进去。"]'
    )
    chain = hermes_turn(
        "turn-runtime-voice",
        "2026-08-25T01:00:00Z",
        "2026-08-25T01:02:00Z",
        (
            "[ASYNC DELEGATION BATCH COMPLETE — batch-1]\n"
            "后台子代理的长报告和内部路径。\n"
            "Full live transcript: /tmp/internal.log\n\n"
            f"{first_voice}\n\n{second_voice}"
        ),
        "我会按干净首页和完整场景重新处理。",
    )
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id},
                "observations": [chain],
            }
        ],
    }

    candidate = module.build_candidate_document(export, session_id)

    assert candidate["turns"] == [
        [
            {
                "role": "user",
                "content": f"User: {first_voice}\n\n{second_voice}",
                "timestamp": "2026-08-25T01:00:00Z",
            },
            {
                "role": "assistant",
                "content": "Assistant: 我会按干净首页和完整场景重新处理。",
                "timestamp": "2026-08-25T01:02:00Z",
            },
        ]
    ]
    assert "ASYNC DELEGATION" not in candidate["document_content"]
    assert "internal.log" not in candidate["document_content"]


def test_build_candidate_document_preserves_turns_across_mid_session_model_switch(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-model-switch"
    before_switch = hermes_turn(
        "turn-deepseek",
        "2026-08-25T01:00:00Z",
        "2026-08-25T01:01:00Z",
        "先分析这个问题",
        "DeepSeek阶段的回复",
    )
    after_switch = hermes_turn(
        "turn-gpt",
        "2026-08-25T02:00:00Z",
        "2026-08-25T02:01:00Z",
        (
            "[Note: model was just switched from deepseek-v4-flash to gpt-5.6-sol "
            "via openai-api. Adjust your self-identification accordingly.]\n\n"
            "换模型后重新分析"
        ),
        "GPT阶段的回复",
    )
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {
                    "task_id": session_id,
                    "model": "gpt-5.6-sol",
                    "provider": "openai-api",
                },
                "observations": [after_switch],
            },
            {
                "metadata": {
                    "task_id": session_id,
                    "model": "deepseek-v4-flash",
                    "provider": "opencode-go",
                },
                "observations": [before_switch],
            },
        ],
    }

    candidate = module.build_candidate_document(export, session_id)

    assert candidate["turns"] == [
        [
            {
                "role": "user",
                "content": "User: 先分析这个问题",
                "timestamp": "2026-08-25T01:00:00Z",
            },
            {
                "role": "assistant",
                "content": "Assistant: DeepSeek阶段的回复",
                "timestamp": "2026-08-25T01:01:00Z",
            },
        ],
        [
            {
                "role": "user",
                "content": "User: 换模型后重新分析",
                "timestamp": "2026-08-25T02:00:00Z",
            },
            {
                "role": "assistant",
                "content": "Assistant: GPT阶段的回复",
                "timestamp": "2026-08-25T02:01:00Z",
            },
        ],
    ]
    assert candidate["audit"]["main_trace_count"] == 2
    assert "model was just switched" not in candidate["document_content"]


def test_undo_filter_removes_only_rewound_occurrence_and_keeps_later_resend(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-undo-resend"
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id},
                "observations": [
                    hermes_turn(
                        "turn-keep",
                        "2026-08-25T01:00:00Z",
                        "2026-08-25T01:01:00Z",
                        "保留的请求",
                        "保留的回复",
                    ),
                    hermes_turn(
                        "turn-undone",
                        "2026-08-25T02:00:00Z",
                        "2026-08-25T02:01:00Z",
                        "重复请求",
                        "已经撤销的回复",
                    ),
                    hermes_turn(
                        "turn-resend",
                        "2026-08-25T03:00:00Z",
                        "2026-08-25T03:01:00Z",
                        "重复请求",
                        "合法重发后的回复",
                    ),
                ],
            }
        ],
    }
    state_db = tmp_path / "state.db"
    with sqlite3.connect(state_db) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                rewind_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL,
                active INTEGER NOT NULL DEFAULT 1,
                compacted INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.execute("INSERT INTO sessions(id, rewind_count) VALUES (?, 1)", (session_id,))
        conn.execute(
            """INSERT INTO messages(
                   id, session_id, role, content, timestamp, active, compacted
               ) VALUES (?, ?, 'user', ?, ?, 0, 0)""",
            (1, session_id, "重复请求", 1787623205.0),
        )
        conn.execute(
            """INSERT INTO messages(
                   id, session_id, role, content, timestamp, active, compacted
               ) VALUES (?, ?, 'user', ?, ?, 1, 0)""",
            (2, session_id, "重复请求", 1787626805.0),
        )

    undo_filter = module.load_undo_filter(session_id, state_db)
    candidate = module.build_candidate_document(
        export,
        session_id,
        undo_filter=undo_filter,
    )

    assert candidate["turns"] == [
        [
            {
                "role": "user",
                "content": "User: 保留的请求",
                "timestamp": "2026-08-25T01:00:00Z",
            },
            {
                "role": "assistant",
                "content": "Assistant: 保留的回复",
                "timestamp": "2026-08-25T01:01:00Z",
            },
        ],
        [
            {
                "role": "user",
                "content": "User: 重复请求",
                "timestamp": "2026-08-25T03:00:00Z",
            },
            {
                "role": "assistant",
                "content": "Assistant: 合法重发后的回复",
                "timestamp": "2026-08-25T03:01:00Z",
            },
        ],
    ]
    assert candidate["audit"]["undo_filter_status"] == "checked"
    assert candidate["audit"]["undo_rewind_count"] == 1
    assert candidate["audit"]["undo_rewound_user_count"] == 1
    assert candidate["audit"]["undo_matched_user_count"] == 1
    assert candidate["audit"]["undo_filtered_message_count"] == 2


def test_undo_filter_keeps_active_resend_when_rewound_trace_is_missing(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-undo-missing-trace"
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id},
                "observations": [
                    hermes_turn(
                        "turn-resend",
                        "2026-08-25T03:00:00Z",
                        "2026-08-25T03:01:00Z",
                        "重复请求",
                        "合法重发后的回复",
                    )
                ],
            }
        ],
    }
    state_db = tmp_path / "state.db"
    with sqlite3.connect(state_db) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                rewind_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL,
                active INTEGER NOT NULL DEFAULT 1,
                compacted INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.execute("INSERT INTO sessions(id, rewind_count) VALUES (?, 1)", (session_id,))
        conn.execute(
            """INSERT INTO messages(
                   id, session_id, role, content, timestamp, active, compacted
               ) VALUES (?, ?, 'user', ?, ?, 0, 0)""",
            (1, session_id, "重复请求", 1787623205.0),
        )
        conn.execute(
            """INSERT INTO messages(
                   id, session_id, role, content, timestamp, active, compacted
               ) VALUES (?, ?, 'user', ?, ?, 1, 0)""",
            (2, session_id, "重复请求", 1787626805.0),
        )

    undo_filter = module.load_undo_filter(session_id, state_db)
    candidate = module.build_candidate_document(
        export,
        session_id,
        undo_filter=undo_filter,
    )

    assert candidate["turns"][0][0]["content"] == "User: 重复请求"
    assert candidate["turns"][0][1]["content"] == "Assistant: 合法重发后的回复"
    assert candidate["audit"]["undo_matched_user_count"] == 0
    assert candidate["audit"]["undo_unmatched_user_count"] == 1
    assert candidate["audit"]["undo_filtered_message_count"] == 0


def test_export_langfuse_fetches_v4_session_observations_with_cursor(
    tmp_path, monkeypatch
):
    module = load_script_module(tmp_path)
    session_id = "session-v4"
    observation_calls = []
    shutdown_calls = []

    class Row:
        def __init__(self, **values):
            self.__dict__.update(values)

    class ObservationsApi:
        def get_many(self, **kwargs):
            observation_calls.append(kwargs)
            if kwargs["cursor"] is None:
                return Row(
                    data=[
                        Row(
                            id="turn-1",
                            traceId="trace-1",
                            sessionId=session_id,
                            isRootObservation=True,
                            parentObservationId="session-root",
                            type="CHAIN",
                            name="Hermes turn",
                            traceName="Hermes trace",
                            startTime="2026-09-02T01:00:00Z",
                            endTime="2026-09-02T01:01:00Z",
                            input=json.dumps(
                                {"role": "user", "content": "v4 request"}
                            ),
                            output=json.dumps(
                                {"content": "v4 answer", "tool_calls": []}
                            ),
                            metadata={
                                "task_id": session_id,
                                "capture_mode": "sanitized",
                            },
                        )
                    ],
                    meta=Row(cursor="next-page"),
                )
            assert kwargs["cursor"] == "next-page"
            return Row(
                data=[
                    Row(
                        id="generation-1",
                        traceId="trace-1",
                        sessionId=session_id,
                        isRootObservation=False,
                        parentObservationId="turn-1",
                        type="GENERATION",
                        name="LLM call 1",
                        traceName="Hermes trace",
                        startTime="2026-09-02T01:00:10Z",
                        endTime="2026-09-02T01:00:20Z",
                        input=json.dumps(
                            [{"role": "user", "content": "v4 request"}]
                        ),
                        output=json.dumps({"role": "assistant", "content": "draft"}),
                        metadata={"task_id": session_id},
                    )
                ],
                meta=Row(cursor=None),
            )

    class Client:
        def __init__(self, **kwargs):
            self.api = Row(observations=ObservationsApi())

        def shutdown(self):
            shutdown_calls.append(True)

    monkeypatch.setattr(module, "Langfuse", Client)
    monkeypatch.setattr(
        module,
        "get_langfuse_credentials",
        lambda env_file: ("public", "secret"),
    )

    exported = module.export_langfuse(session_id, tmp_path / "env")

    assert [call["cursor"] for call in observation_calls] == [None, "next-page"]
    assert all(call["limit"] == 100 for call in observation_calls)
    assert all(
        json.loads(call["filter"])
        == [
            {
                "type": "string",
                "column": "sessionId",
                "operator": "=",
                "value": session_id,
            }
        ]
        for call in observation_calls
    )
    assert all("parse_io_as_json" not in call for call in observation_calls)
    assert exported["trace_count"] == 1
    assert exported["traces"][0]["id"] == "trace-1"
    assert exported["traces"][0]["metadata"] == {
        "task_id": session_id,
        "capture_mode": "sanitized",
    }
    assert exported["traces"][0]["observations"][0]["input"] == {
        "role": "user",
        "content": "v4 request",
    }
    assert exported["traces"][0]["observations"][1]["input"] == [
        {"role": "user", "content": "v4 request"}
    ]
    assert shutdown_calls == [True]


def test_v4_traces_keep_observation_task_id_so_background_turns_are_excluded(
    tmp_path,
):
    module = load_script_module(tmp_path)
    session_id = "session-v4"

    def observation(obs_id, trace_id, start, content, answer, metadata):
        row = {
            "id": obs_id,
            "traceId": trace_id,
            "sessionId": session_id,
            "isRootObservation": True,
            "type": "CHAIN",
            "name": "Hermes turn",
            "startTime": start,
            "endTime": start,
            "input": {"role": "user", "content": content},
            "output": {"content": answer, "tool_calls": []},
        }
        if metadata is not None:
            row["metadata"] = metadata
        return row

    traces = module._session_observations_to_traces(
        [
            observation(
                "main-turn",
                "trace-main",
                "2026-09-02T01:00:00Z",
                "真实用户消息",
                "真实回复",
                {"task_id": session_id},
            ),
            observation(
                "review-turn",
                "trace-review",
                "2026-09-02T01:05:00Z",
                "Review the conversation and autonomously maintain built-in memory.",
                "Nothing to save.",
                {"task_id": "1f2e3d4c-review"},
            ),
            observation(
                "legacy-turn",
                "trace-legacy",
                "2026-09-02T01:10:00Z",
                "旧格式轮次",
                "旧格式回复",
                None,
            ),
        ],
        session_id,
    )

    assert {trace["id"]: trace["metadata"]["task_id"] for trace in traces} == {
        "trace-main": session_id,
        "trace-review": "1f2e3d4c-review",
        "trace-legacy": session_id,
    }
    candidate = module.build_candidate_document(
        {"session_id": session_id, "traces": traces}, session_id
    )
    contents = [message["content"] for turn in candidate["turns"] for message in turn]
    assert "User: 真实用户消息" in contents
    assert "User: 旧格式轮次" in contents
    assert not any("Review the conversation" in content for content in contents)
    assert "Assistant: Nothing to save." not in contents


def _create_reconciliation_state_db(path: Path, session_id: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                rewind_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_name TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                finish_reason TEXT,
                timestamp REAL,
                active INTEGER NOT NULL DEFAULT 1,
                compacted INTEGER NOT NULL DEFAULT 0,
                display_kind TEXT,
                platform_message_id TEXT,
                display_order INTEGER,
                display_identity BLOB
            );
            """
        )
        conn.execute("INSERT INTO sessions (id) VALUES (?)", (session_id,))


def test_state_reconciliation_restores_real_user_before_continuity_answer(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-continuity-opening"
    state_db = tmp_path / "state.db"
    _create_reconciliation_state_db(state_db, session_id)
    with sqlite3.connect(state_db) as conn:
        conn.execute(
            """
            INSERT INTO messages (
                id, session_id, role, content, timestamp, platform_message_id,
                display_order
            ) VALUES (1, ?, 'user', '真实第一条用户消息', 1000, 'telegram-1', 1)
            """,
            (session_id,),
        )
    continuity = hermes_turn(
        "turn-1",
        "1970-01-01T00:16:41Z",
        "1970-01-01T00:16:42Z",
        (
            '<hermes-runtime-context user-authored="false" '
            'source="long-task-continuity">内部恢复内容</hermes-runtime-context>'
        ),
        "针对真实问题的回答",
    )
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id, "capture_mode": "sanitized"},
                "observations": [continuity],
            }
        ],
    }

    state_reconciliation = module.load_state_reconciliation(
        session_id,
        state_db,
        cutoff_at=datetime.fromtimestamp(1003, tz=timezone.utc),
    )
    candidate = module.build_candidate_document(
        export,
        session_id,
        state_reconciliation=state_reconciliation,
        cutoff_at=datetime.fromtimestamp(1003, tz=timezone.utc),
    )

    assert candidate["turns"] == [
        [
            {
                "role": "user",
                "content": "User: 真实第一条用户消息",
                "timestamp": "1970-01-01T00:16:40+00:00",
            },
            {
                "role": "assistant",
                "content": "Assistant: 针对真实问题的回答",
                "timestamp": "1970-01-01T00:16:42Z",
            },
        ]
    ]
    assert candidate["audit"]["state_reconciliation"] == {
        "status": "verified",
        "source_event_count": 1,
        "matched_event_count": 0,
        "added_event_count": 1,
        "uncovered_event_count": 0,
        "platform_user_event_count": 1,
        "redirect_user_event_count": 0,
        "visible_assistant_event_count": 0,
        "clarify_question_event_count": 0,
        "clarify_response_event_count": 0,
    }


def test_state_reconciliation_drops_compaction_replayed_assistant_copies(tmp_path):
    """Durable identities collapse compaction copies despite fresh timestamps.

    The fallback (body, timestamp) key cannot see those copies, so each compaction
    used to add another copy of the same reply to the candidate. Regression for the
    FIP session that exported one reply 5 times and another 8 times.
    """
    module = load_script_module(tmp_path)
    session_id = "session-compaction-replay"
    state_db = tmp_path / "state.db"
    _create_reconciliation_state_db(state_db, session_id)

    marker = (
        "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
        "into the summary below."
    )
    rows = [
        # Real conversation.
        (1, "user", "第一个问题", 1000, 1, b"user-1"),
        (2, "assistant", "第一个回答", 1001, 2, b"assistant-1"),
        (3, "user", "第二个问题", 1002, 3, b"user-2"),
        (4, "assistant", "第二个回答", 1003, 4, b"assistant-2"),
        # First compaction: physical copies retain their durable identities.
        (5, "assistant", marker, 1004, 5, b"marker-1"),
        (6, "user", "第一个问题", 1004.1, 6, b"user-1"),
        (7, "assistant", "第一个回答", 1004.2, 7, b"assistant-1"),
        (8, "user", "第二个问题", 1004.3, 8, b"user-2"),
        (9, "assistant", "第二个回答", 1004.4, 9, b"assistant-2"),
        # Second compaction replays the same logical messages again.
        (10, "assistant", marker, 1005, 10, b"marker-2"),
        (11, "user", "第一个问题", 1005.1, 11, b"user-1"),
        (12, "assistant", "第一个回答", 1005.2, 12, b"assistant-1"),
        (13, "user", "第二个问题", 1005.3, 13, b"user-2"),
        (14, "assistant", "第二个回答", 1005.4, 14, b"assistant-2"),
    ]
    with sqlite3.connect(state_db) as conn:
        for row_id, role, content, ts, order, identity in rows:
            conn.execute(
                """
                INSERT INTO messages (
                    id, session_id, role, content, timestamp, display_order,
                    display_identity
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (row_id, session_id, role, content, ts, order, identity),
            )

    reconciliation = module.load_state_reconciliation(session_id, state_db)
    assistant_bodies = [
        event["content"]
        for event in reconciliation["events"]
        if event["source_kind"] == "visible_assistant"
    ]

    # Each distinct reply survives exactly once, despite two replays apiece.
    assert assistant_bodies == ["第一个回答", "第二个回答"]


def test_state_reconciliation_keeps_real_repeat_after_compaction_replay(tmp_path):
    """A new reply after replay survives even when its text matches older replies."""
    module = load_script_module(tmp_path)
    session_id = "session-repeat-after-compaction"
    state_db = tmp_path / "state.db"
    _create_reconciliation_state_db(state_db, session_id)
    marker = (
        "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
        "into the summary below."
    )
    rows = [
        (1, "user", "第一次问", 1000, 1, b"user-1"),
        (2, "assistant", "好的", 1001, 2, b"assistant-1"),
        (3, "assistant", marker, 1002, 3, b"marker-1"),
        (4, "user", "第一次问", 1002.1, 4, b"user-1"),
        (5, "assistant", "好的", 1002.2, 5, b"assistant-1"),
        (6, "user", "第二次问", 1003, 6, b"user-2"),
        (7, "assistant", "好的", 1004, 7, b"assistant-2"),
    ]
    with sqlite3.connect(state_db) as conn:
        conn.executemany(
            """
            INSERT INTO messages (
                id, session_id, role, content, timestamp, display_order,
                display_identity
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (row_id, session_id, role, content, ts, order, identity)
                for row_id, role, content, ts, order, identity in rows
            ],
        )

    reconciliation = module.load_state_reconciliation(session_id, state_db)
    assistant_bodies = [
        event["content"]
        for event in reconciliation["events"]
        if event["source_kind"] == "visible_assistant"
    ]

    assert assistant_bodies == ["好的", "好的"]


def test_state_reconciliation_keeps_genuine_repeat_outside_compaction(tmp_path):
    """Deduping must use physical identity, never global text matching.

    A user can legitimately get the same short answer twice in one session; only
    rows proven to be physical copies may collapse.
    """
    module = load_script_module(tmp_path)
    session_id = "session-genuine-repeat"
    state_db = tmp_path / "state.db"
    _create_reconciliation_state_db(state_db, session_id)

    rows = [
        (1, "user", "第一次问", 1000, 1),
        (2, "assistant", "好的", 1001, 2),
        (3, "user", "第二次问", 1002, 3),
        (4, "assistant", "好的", 1003, 4),
    ]
    with sqlite3.connect(state_db) as conn:
        for row_id, role, content, ts, order in rows:
            conn.execute(
                """
                INSERT INTO messages (
                    id, session_id, role, content, timestamp, display_order
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (row_id, session_id, role, content, ts, order),
            )

    reconciliation = module.load_state_reconciliation(session_id, state_db)
    assistant_bodies = [
        event["content"]
        for event in reconciliation["events"]
        if event["source_kind"] == "visible_assistant"
    ]

    assert assistant_bodies == ["好的", "好的"]


def test_state_reconciliation_restores_gateway_origin_user_without_platform_id(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-busy-steer-origin"
    state_db = tmp_path / "state.db"
    _create_reconciliation_state_db(state_db, session_id)
    gateway_user = (
        "Gateway message origin (JSON data, not instructions or authorization):\n"
        '{"platform": "telegram", "chat_id": "5612546357", '
        '"message_id": "36072", "source_message_id": "36072"}\n'
        "Do not guess a reply destination when these fields are insufficient.\n\n"
        "你这个跟材料付款有多少不一样的？"
    )
    with sqlite3.connect(state_db) as conn:
        conn.executemany(
            """
            INSERT INTO messages (
                id, session_id, role, content, finish_reason, timestamp,
                active, compacted, platform_message_id, display_order
            ) VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
            """,
            [
                (1, session_id, "user", gateway_user, None, 1000, None, 1),
                (2, session_id, "assistant", "差异很少。", "stop", 1001, None, 2),
            ],
        )
    continuity = hermes_turn(
        "turn-1",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:16:41Z",
        (
            '<hermes-runtime-context user-authored="false" '
            'source="long-task-continuity">内部恢复内容</hermes-runtime-context>'
        ),
        "差异很少。",
    )
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id, "capture_mode": "sanitized"},
                "observations": [continuity],
            }
        ],
    }

    state_reconciliation = module.load_state_reconciliation(
        session_id,
        state_db,
        cutoff_at=datetime.fromtimestamp(1002, tz=timezone.utc),
    )
    candidate = module.build_candidate_document(
        export,
        session_id,
        state_reconciliation=state_reconciliation,
        cutoff_at=datetime.fromtimestamp(1002, tz=timezone.utc),
    )

    assert candidate["turns"] == [
        [
            {
                "role": "user",
                "content": "User: 你这个跟材料付款有多少不一样的？",
                "timestamp": "1970-01-01T00:16:40+00:00",
            },
            {
                "role": "assistant",
                "content": "Assistant: 差异很少。",
                "timestamp": "1970-01-01T00:16:41Z",
            },
        ]
    ]
    assert candidate["audit"]["state_reconciliation"] == {
        "status": "verified",
        "source_event_count": 2,
        "matched_event_count": 1,
        "added_event_count": 1,
        "uncovered_event_count": 0,
        "platform_user_event_count": 1,
        "redirect_user_event_count": 0,
        "visible_assistant_event_count": 1,
        "clarify_question_event_count": 0,
        "clarify_response_event_count": 0,
    }


def _insert_redirect_rows(state_db: Path, session_id: str, rows: list[tuple]) -> None:
    with sqlite3.connect(state_db) as conn:
        conn.executemany(
            """
            INSERT INTO messages (
                id, session_id, role, content, finish_reason, timestamp, active,
                compacted, display_kind, platform_message_id, display_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(row[0], session_id, *row[1:]) for row in rows],
        )


def _redirect_export(session_id: str, observations: list[dict]) -> dict:
    return {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id, "capture_mode": "sanitized"},
                "observations": observations,
            }
        ],
    }


def test_active_turn_redirect_writes_the_pair_reconciliation_recognizes():
    """Pins the producer shape the redirect_user rule depends on."""
    from agent import conversation_loop

    class _Agent:
        _current_streamed_assistant_text = ""
        _stream_needs_break = False

        @staticmethod
        def _strip_think_blocks(text):
            return text

    messages = [{"role": "user", "content": "先查一下", "timestamp": 1.0}]
    conversation_loop._apply_active_turn_redirect(_Agent(), messages, "停，换个方向")

    placeholder, correction = messages[-2], messages[-1]
    assert placeholder["role"] == "assistant"
    assert placeholder["content"] == ""
    assert placeholder["display_kind"] == "hidden"
    assert correction["role"] == "user"
    assert correction["content"] == "停，换个方向"
    assert "display_kind" not in correction
    assert "platform_message_id" not in correction
    gap = correction["timestamp"] - placeholder["timestamp"]
    assert 0 <= gap < 0.001


def test_state_reconciliation_restores_active_turn_redirect_user(tmp_path):
    """Regression for session 20260902_001539_d1ce8d22.

    A user correction sent while the model was answering is written by
    _apply_active_turn_redirect as a hidden empty placeholder plus the user's
    own words, with no platform id and absent from Langfuse. Compaction parked
    the original at active=0/compacted=0 and re-inserted a copy after a hidden
    summary carrier; only the original's placeholder proves the redirect, and
    the copy must collapse onto it.
    """
    module = load_script_module(tmp_path)
    session_id = "session-active-turn-redirect"
    state_db = tmp_path / "state.db"
    _create_reconciliation_state_db(state_db, session_id)
    correction = "你抓包之前，就同步了，啥情况"
    _insert_redirect_rows(
        state_db,
        session_id,
        [
            (1, "user", "查同步", None, 1000.0, 0, 1, None, "telegram-1", 1),
            (2, "assistant", "", None, 1005.000001, 0, 0, "hidden", None, 2),
            (3, "user", correction, None, 1005.000003, 0, 0, None, None, 3),
            (4, "assistant", "[PRIOR CONTEXT — summary]", None, 1005.000001, 0, 1,
             "hidden", None, 4),
            (5, "user", correction, None, 1005.000003, 0, 1, None, None, 5),
            (6, "assistant", "是时间差导致的。", "stop", 1010.0, 1, 0, None, None, 6),
        ],
    )
    export = _redirect_export(
        session_id,
        [
            hermes_turn(
                "turn-1",
                "1970-01-01T00:16:40Z",
                "1970-01-01T00:16:50Z",
                "查同步",
                "是时间差导致的。",
            )
        ],
    )
    cutoff = datetime.fromtimestamp(1011, tz=timezone.utc)

    state_reconciliation = module.load_state_reconciliation(
        session_id, state_db, cutoff_at=cutoff
    )
    candidate = module.build_candidate_document(
        export,
        session_id,
        state_reconciliation=state_reconciliation,
        cutoff_at=cutoff,
    )

    contents = [message["content"] for turn in candidate["turns"] for message in turn]
    assert contents == [
        "User: 查同步",
        f"User: {correction}",
        "Assistant: 是时间差导致的。",
    ]
    audit = candidate["audit"]["state_reconciliation"]
    assert audit["redirect_user_event_count"] == 1
    assert audit["added_event_count"] == 1
    assert audit["uncovered_event_count"] == 0


def test_state_reconciliation_does_not_duplicate_redirect_user_present_in_langfuse(
    tmp_path,
):
    module = load_script_module(tmp_path)
    session_id = "session-redirect-present"
    state_db = tmp_path / "state.db"
    _create_reconciliation_state_db(state_db, session_id)
    _insert_redirect_rows(
        state_db,
        session_id,
        [
            (1, "assistant", "", None, 1000.000001, 1, 0, "hidden", None, 1),
            (2, "user", "别审查了", None, 1000.000002, 1, 0, None, None, 2),
            (3, "assistant", "好，停止审查。", "stop", 1001.0, 1, 0, None, None, 3),
        ],
    )
    export = _redirect_export(
        session_id,
        [
            hermes_turn(
                "turn-1",
                "1970-01-01T00:16:40Z",
                "1970-01-01T00:16:41Z",
                "别审查了",
                "好，停止审查。",
            )
        ],
    )
    cutoff = datetime.fromtimestamp(1002, tz=timezone.utc)
    candidate = module.build_candidate_document(
        export,
        session_id,
        state_reconciliation=module.load_state_reconciliation(
            session_id, state_db, cutoff_at=cutoff
        ),
        cutoff_at=cutoff,
    )

    contents = [message["content"] for turn in candidate["turns"] for message in turn]
    assert contents == ["User: 别审查了", "Assistant: 好，停止审查。"]
    audit = candidate["audit"]["state_reconciliation"]
    assert audit["redirect_user_event_count"] == 1
    assert audit["matched_event_count"] == 2
    assert audit["added_event_count"] == 0


def test_state_reconciliation_collapses_redirect_copy_with_fresh_timestamp(tmp_path):
    """Regression for sessions 20260824_205339_b18957b9 / 20260828_082659_6f3805be.

    Compaction re-inserted a whole tool run, redirect pair included, with fresh
    timestamps. The copied pair repeats the tool_call_id of the row before its
    placeholder; a later genuine repeat of the same words follows another call.
    """
    module = load_script_module(tmp_path)
    session_id = "session-redirect-fresh-copy"
    state_db = tmp_path / "state.db"
    _create_reconciliation_state_db(state_db, session_id)
    words = "继续搞，搞完自己验证下"
    rows = [
        (1, "user", "开始", None, None, None, 1000.0, 0, 1, None, "telegram-1", 1),
        (2, "assistant", "", None, None, "[{\"id\": \"call-a\"}]", 1001.0, 0, 1, None, None, 2),
        (3, "tool", "ok-a", "terminal", "call-a", None, 1002.0, 0, 1, None, None, 3),
        (4, "assistant", "", None, None, None, 1002.000001, 0, 1, "hidden", None, 4),
        (5, "user", words, None, None, None, 1002.000002, 0, 1, None, None, 5),
        # compaction copy of rows 2-5 with fresh timestamps
        (6, "assistant", "", None, None, "[{\"id\": \"call-a\"}]", 1600.0, 0, 1, None, None, 6),
        (7, "tool", "ok-a", "terminal", "call-a", None, 1600.000001, 0, 1, None, None, 7),
        (8, "assistant", "", None, None, None, 1600.000002, 0, 1, "hidden", None, 8),
        (9, "user", words, None, None, None, 1600.000003, 0, 1, None, None, 9),
        # a genuine later repeat after a different tool call
        (10, "assistant", "", None, None, "[{\"id\": \"call-b\"}]", 1700.0, 1, 0, None, None, 10),
        (11, "tool", "ok-b", "terminal", "call-b", None, 1701.0, 1, 0, None, None, 11),
        (12, "assistant", "", None, None, None, 1701.000001, 1, 0, "hidden", None, 12),
        (13, "user", words, None, None, None, 1701.000002, 1, 0, None, None, 13),
    ]
    with sqlite3.connect(state_db) as conn:
        conn.executemany(
            """
            INSERT INTO messages (
                id, session_id, role, content, tool_name, tool_call_id,
                tool_calls, timestamp, active, compacted, display_kind,
                platform_message_id, display_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(row[0], session_id, *row[1:]) for row in rows],
        )
    cutoff = datetime.fromtimestamp(1800, tz=timezone.utc)
    state_reconciliation = module.load_state_reconciliation(
        session_id, state_db, cutoff_at=cutoff
    )

    redirect_events = [
        event
        for event in state_reconciliation["events"]
        if event["source_kind"] == "redirect_user"
    ]
    assert [(event["content"], event["source_key"]) for event in redirect_events] == [
        (words, "redirect_user:5"),
        (words, "redirect_user:13"),
    ]


def test_state_reconciliation_needs_the_redirect_pair_for_unidentified_user(tmp_path):
    """No platform id alone proves nothing: every other shape stays unproven."""
    module = load_script_module(tmp_path)
    session_id = "session-redirect-negative"
    state_db = tmp_path / "state.db"
    _create_reconciliation_state_db(state_db, session_id)
    kanban_notice = (
        "[kanban] 任务 t_8e672585 被阻塞，需要处理。\n标题: 复核目录\n\n"
        "这是自动任务状态通知，不是再次分解任务的请求。创建后续任务前请先检查当前看板；"
        "不要重复创建已存在的任务或任务图。"
    )
    _insert_redirect_rows(
        state_db,
        session_id,
        [
            # placeholder too far before the user row: not the same step
            (1, "assistant", "", None, 1000.0, 1, 0, "hidden", None, 1),
            (2, "user", "间隔过长的消息", None, 1000.5, 1, 0, None, None, 2),
            # previous row is visible text, not the hidden empty placeholder
            (3, "assistant", "可见回复", "stop", 1001.0, 1, 0, None, None, 3),
            (4, "user", "紧跟可见回复的消息", None, 1001.0000005, 1, 0, None, None, 4),
            # previous row is hidden but not empty (summary carrier)
            (5, "assistant", "[PRIOR CONTEXT — summary]", None, 1002.0, 1, 0,
             "hidden", None, 5),
            (6, "user", "紧跟摘要的消息", None, 1002.0000005, 1, 0, None, None, 6),
            # redirect shape, but Hermes' automatic kanban wake notice
            (7, "assistant", "", None, 1003.0, 1, 0, "hidden", None, 7),
            (8, "user", kanban_notice, None, 1003.0000005, 1, 0, None, None, 8),
        ],
    )
    cutoff = datetime.fromtimestamp(1004, tz=timezone.utc)
    state_reconciliation = module.load_state_reconciliation(
        session_id, state_db, cutoff_at=cutoff
    )

    user_events = [
        event for event in state_reconciliation["events"] if event["role"] == "user"
    ]
    assert user_events == []


def test_state_reconciliation_restores_visible_assistant_missing_from_langfuse(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-missing-visible-assistant"
    state_db = tmp_path / "state.db"
    _create_reconciliation_state_db(state_db, session_id)
    with sqlite3.connect(state_db) as conn:
        conn.executemany(
            """
            INSERT INTO messages (
                id, session_id, role, content, finish_reason, timestamp,
                active, compacted, platform_message_id, display_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, session_id, "user", "请继续处理", None, 1000, 1, 0, "telegram-1", 1),
                (2, session_id, "assistant", "被 Langfuse 漏掉的正式回复", "stop", 1001, 1, 0, None, 2),
                (3, session_id, "assistant", "压缩前已有的最终回复", "stop", 1002, 0, 1, None, 3),
            ],
        )
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id, "capture_mode": "sanitized"},
                "observations": [
                    hermes_turn(
                        "turn-1",
                        "1970-01-01T00:16:40Z",
                        "1970-01-01T00:16:42Z",
                        "请继续处理",
                        "压缩前已有的最终回复",
                    )
                ],
            }
        ],
    }

    state_reconciliation = module.load_state_reconciliation(
        session_id,
        state_db,
        cutoff_at=datetime.fromtimestamp(1003, tz=timezone.utc),
    )
    candidate = module.build_candidate_document(
        export,
        session_id,
        state_reconciliation=state_reconciliation,
        cutoff_at=datetime.fromtimestamp(1003, tz=timezone.utc),
    )

    assert [
        message["content"]
        for turn in candidate["turns"]
        for message in turn
    ] == [
        "User: 请继续处理",
        "Assistant: 被 Langfuse 漏掉的正式回复",
        "Assistant: 压缩前已有的最终回复",
    ]
    assert candidate["audit"]["state_reconciliation"] == {
        "status": "verified",
        "source_event_count": 3,
        "matched_event_count": 2,
        "added_event_count": 1,
        "uncovered_event_count": 0,
        "platform_user_event_count": 1,
        "redirect_user_event_count": 0,
        "visible_assistant_event_count": 2,
        "clarify_question_event_count": 0,
        "clarify_response_event_count": 0,
    }


def test_state_reconciliation_excludes_nonvisible_assistant_rows(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-assistant-visibility"
    state_db = tmp_path / "state.db"
    _create_reconciliation_state_db(state_db, session_id)
    tool_calls = json.dumps(
        [{"id": "tool-1", "function": {"name": "terminal", "arguments": "{}"}}]
    )
    with sqlite3.connect(state_db) as conn:
        conn.executemany(
            """
            INSERT INTO messages (
                id, session_id, role, content, tool_calls, finish_reason,
                timestamp, active, compacted, display_kind, display_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
            """,
            [
                (1, session_id, "assistant", "真实可见回复", None, "stop", 1000, None, 1),
                (2, session_id, "assistant", "内部通知", None, "stop", 1001, "internal_notification", 2),
                (3, session_id, "assistant", "隐藏内容", None, "stop", 1002, "hidden", 3),
                (4, session_id, "assistant", "工具调用", tool_calls, "tool_calls", 1003, None, 4),
                (5, session_id, "assistant", "[kanban] internal state", None, "stop", 1004, None, 5),
                (6, session_id, "assistant", "[CONTEXT COMPACTION — internal]", None, "stop", 1005, None, 6),
            ],
        )

    reconciliation = module.load_state_reconciliation(
        session_id,
        state_db,
        cutoff_at=datetime.fromtimestamp(1006, tz=timezone.utc),
    )

    assistant_events = [
        event
        for event in reconciliation["events"]
        if event["source_kind"] == "visible_assistant"
    ]
    assert [event["content"] for event in assistant_events] == ["真实可见回复"]


def test_state_reconciliation_deduplicates_physical_assistant_copies(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-assistant-physical-copies"
    state_db = tmp_path / "state.db"
    _create_reconciliation_state_db(state_db, session_id)
    with sqlite3.connect(state_db) as conn:
        conn.executemany(
            """
            INSERT INTO messages (
                id, session_id, role, content, finish_reason, timestamp,
                active, compacted, display_order
            ) VALUES (?, ?, 'assistant', ?, 'stop', ?, ?, ?, ?)
            """,
            [
                (1, session_id, "同一条正式回复", 1000, 1, 0, 1),
                (2, session_id, "同一条正式回复", 1000, 0, 1, 2),
            ],
        )

    reconciliation = module.load_state_reconciliation(
        session_id,
        state_db,
        cutoff_at=datetime.fromtimestamp(1001, tz=timezone.utc),
    )

    assistant_events = [
        event
        for event in reconciliation["events"]
        if event["source_kind"] == "visible_assistant"
    ]
    assert len(assistant_events) == 1
    assert assistant_events[0]["content"] == "同一条正式回复"


def test_state_reconciliation_drops_failed_turn_notice_but_keeps_user(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-failed-turn"
    state_db = tmp_path / "state.db"
    _create_reconciliation_state_db(state_db, session_id)
    notice = (
        "Your request was not processed. Send it again if you still want me to "
        "carry it out."
    )
    with sqlite3.connect(state_db) as conn:
        conn.executemany(
            """
            INSERT INTO messages (
                id, session_id, role, content, finish_reason, timestamp,
                active, compacted, display_kind, platform_message_id, display_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, session_id, "user", "没被处理的请求", None, 1000, 1, 0, None, "p-1", 1),
                (2, session_id, "assistant", notice, "stop", 1001, 1, 0, None, None, 2),
                (3, session_id, "assistant", notice, "stop", 1002, 0, 1, "failed_turn", None, 3),
                (4, session_id, "assistant", f"用户问的是：{notice}", "stop", 1003, 1, 0, None, None, 4),
            ],
        )

    reconciliation = module.load_state_reconciliation(
        session_id,
        state_db,
        cutoff_at=datetime.fromtimestamp(1004, tz=timezone.utc),
    )

    contents = [(event["role"], event["content"]) for event in reconciliation["events"]]
    assert ("user", "没被处理的请求") in contents
    assert [content for role, content in contents if role == "assistant"] == [
        f"用户问的是：{notice}"
    ]


def test_failed_turn_notices_match_agent_constants(tmp_path):
    from agent import turn_failure_copy

    module = load_script_module(tmp_path)

    assert module._FAILED_TURN_DISPLAY_KIND == turn_failure_copy.FAILED_TURN_DISPLAY_KIND
    assert set(module._FAILED_TURN_NOTICES) == {
        turn_failure_copy.FAILED_TURN_NOTICE,
        turn_failure_copy.PARTIAL_FAILED_TURN_NOTICE,
    }


_MERGED_PRIOR_CONTEXT_HEADER = "[PRIOR CONTEXT — for reference only; not a new message]"
_MERGED_SUMMARY_DELIMITER = "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"
_SUMMARY_END_MARKER = (
    "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"
)


def _add_codex_message_items_column(state_db: Path) -> None:
    with sqlite3.connect(state_db) as conn:
        conn.execute("ALTER TABLE messages ADD COLUMN codex_message_items TEXT")


def _codex_message_items(message_id: str, text: str) -> str:
    return json.dumps(
        [
            {
                "type": "message",
                "id": message_id,
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        ensure_ascii=False,
    )


def _merged_prior_context_carrier(prior: str) -> str:
    """Build the row shape written when compaction folds its summary into a tail row."""
    return (
        f"{_MERGED_PRIOR_CONTEXT_HEADER}\n{prior}\n\n{_MERGED_SUMMARY_DELIMITER}\n\n"
        "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into "
        "the summary below.\n## Goal\n压缩摘要正文不应进入 Retain。\n\n"
        f"{_SUMMARY_END_MARKER}"
    )


def test_state_reconciliation_collapses_compaction_copies_by_provider_message_id(
    tmp_path,
):
    """Compaction clones may get fresh timestamps and fresh display identities.

    Regression for session 20260923_160931_4eaf17f7: two compactions one minute
    apart cloned one reply; every clone had a new timestamp and a new
    display_identity, so reconciliation added both clones next to the single
    Langfuse copy. The provider output-message id is shared by all clones and is
    new for a later real reply, even when that reply repeats the same text.
    """
    module = load_script_module(tmp_path)
    session_id = "session-provider-message-copies"
    state_db = tmp_path / "state.db"
    _create_reconciliation_state_db(state_db, session_id)
    _add_codex_message_items_column(state_db)
    reply = "建议分别设置：会话压缩用 xhigh，Hindsight Retain 用 high。"
    rows = [
        (1, "user", "怎么设置？", 1000, 1, 0, "telegram-1", b"user-1", None),
        (2, "assistant", reply, 1001, 0, 1, None, b"assistant-original",
         _codex_message_items("msg_reply", reply)),
        (3, "assistant", reply, 1060, 0, 1, None, b"assistant-clone-1",
         _codex_message_items("msg_reply", reply)),
        (4, "assistant", reply, 1120, 1, 0, None, b"assistant-clone-2",
         _codex_message_items("msg_reply", reply)),
        (5, "user", "再说一遍", 1200, 1, 0, "telegram-2", b"user-2", None),
        (6, "assistant", reply, 1201, 1, 0, None, b"assistant-repeat",
         _codex_message_items("msg_repeat", reply)),
    ]
    with sqlite3.connect(state_db) as conn:
        conn.executemany(
            """
            INSERT INTO messages (
                id, session_id, role, content, finish_reason, timestamp, active,
                compacted, platform_message_id, display_order, display_identity,
                codex_message_items
            ) VALUES (?, ?, ?, ?, 'stop', ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (row_id, session_id, role, content, ts, active, compacted,
                 platform_id, row_id, identity, items)
                for row_id, role, content, ts, active, compacted, platform_id,
                identity, items in rows
            ],
        )
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id, "capture_mode": "sanitized"},
                "observations": [
                    hermes_turn(
                        "turn-1",
                        "1970-01-01T00:16:40Z",
                        "1970-01-01T00:16:41Z",
                        "怎么设置？",
                        reply,
                    ),
                    hermes_turn(
                        "turn-2",
                        "1970-01-01T00:20:00Z",
                        "1970-01-01T00:20:01Z",
                        "再说一遍",
                        reply,
                    ),
                ],
            }
        ],
    }
    cutoff = datetime.fromtimestamp(1300, tz=timezone.utc)

    reconciliation = module.load_state_reconciliation(
        session_id, state_db, cutoff_at=cutoff
    )
    candidate = module.build_candidate_document(
        export,
        session_id,
        state_reconciliation=reconciliation,
        cutoff_at=cutoff,
    )

    assert [
        (event["content"], event["timestamp"])
        for event in reconciliation["events"]
        if event["source_kind"] == "visible_assistant"
    ] == [
        (reply, datetime.fromtimestamp(1001, tz=timezone.utc).isoformat()),
        (reply, datetime.fromtimestamp(1201, tz=timezone.utc).isoformat()),
    ]
    assert [
        message["content"] for turn in candidate["turns"] for message in turn
    ] == [
        "User: 怎么设置？",
        f"Assistant: {reply}",
        "User: 再说一遍",
        f"Assistant: {reply}",
    ]
    audit = candidate["audit"]["state_reconciliation"]
    assert audit["status"] == "verified"
    assert audit["source_event_count"] == 4
    assert audit["matched_event_count"] == 4
    assert audit["added_event_count"] == 0


def test_state_reconciliation_keeps_only_prior_reply_of_merged_summary_carrier(
    tmp_path,
):
    """A merged compaction carrier contributes its prior reply, never the summary.

    Regression for session 20260923_160931_4eaf17f7, whose candidate contained an
    18k-character assistant message: the carried reply followed by the complete
    compaction summary. The carried reply is itself a clone of an earlier row.
    """
    module = load_script_module(tmp_path)
    session_id = "session-merged-summary-carrier"
    state_db = tmp_path / "state.db"
    _create_reconciliation_state_db(state_db, session_id)
    _add_codex_message_items_column(state_db)
    first = "第一条回复"
    last = "压缩前最后一条回复"
    rows = [
        (1, "assistant", first, 1000, 0, 1, b"assistant-1",
         _codex_message_items("msg_first", first)),
        # Carrier cloned from row 1: same provider message, new identity/time.
        (2, "assistant", _merged_prior_context_carrier(first), 1100, 0, 1,
         b"carrier-1", _codex_message_items("msg_first", first)),
        # Carrier whose prior part is empty holds only the summary.
        (3, "assistant", _merged_prior_context_carrier(""), 1200, 0, 1,
         b"carrier-2", None),
        # Carrier for a reply with no earlier row keeps just that reply.
        (4, "assistant", _merged_prior_context_carrier(last), 1300, 1, 0,
         b"carrier-3", _codex_message_items("msg_last", last)),
    ]
    with sqlite3.connect(state_db) as conn:
        conn.executemany(
            """
            INSERT INTO messages (
                id, session_id, role, content, finish_reason, timestamp, active,
                compacted, display_order, display_identity, codex_message_items
            ) VALUES (?, ?, ?, ?, 'stop', ?, ?, ?, ?, ?, ?)
            """,
            [
                (row_id, session_id, role, content, ts, active, compacted, row_id,
                 identity, items)
                for row_id, role, content, ts, active, compacted, identity, items
                in rows
            ],
        )

    reconciliation = module.load_state_reconciliation(session_id, state_db)

    assert [
        event["content"]
        for event in reconciliation["events"]
        if event["source_kind"] == "visible_assistant"
    ] == [first, last]


def test_merged_carrier_markers_match_context_compressor(tmp_path):
    """The exporter keeps literal copies of the compressor's carrier markers."""
    from agent import context_compressor

    module = load_script_module(tmp_path)

    assert module._MERGED_PRIOR_CONTEXT_HEADER == (
        context_compressor._MERGED_PRIOR_CONTEXT_HEADER
    )
    assert module._MERGED_SUMMARY_DELIMITER == (
        context_compressor._MERGED_SUMMARY_DELIMITER
    )
    assert context_compressor.SUMMARY_PREFIX.startswith(
        module._COMPACTION_MARKER_PREFIX
    )
    compressor = context_compressor.ContextCompressor.__new__(
        context_compressor.ContextCompressor
    )
    compressor._summary_has_user_turn = True
    row = {"role": "assistant", "content": "被保留的上一条回复"}
    compressor._merge_summary_into_tail_row(
        row,
        context_compressor.SUMMARY_PREFIX + "\n## Goal\n摘要正文",
        "assistant",
        False,
    )

    assert module._merged_carrier_prior_content(row["content"]) == "被保留的上一条回复"


def test_state_reconciliation_projects_clarify_when_v4_has_only_chain(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-v4-clarify"
    state_db = tmp_path / "state.db"
    _create_reconciliation_state_db(state_db, session_id)
    tool_call_id = "call-clarify-1"
    tool_calls = json.dumps(
        [
            {
                "id": tool_call_id,
                "call_id": tool_call_id,
                "function": {
                    "name": "clarify",
                    "arguments": json.dumps(
                        {
                            "questions": [
                                {
                                    "question": "是否执行远端替换？",
                                    "choices": ["执行替换", "先不执行"],
                                }
                            ]
                        },
                        ensure_ascii=False,
                    ),
                },
            }
        ],
        ensure_ascii=False,
    )
    tool_result = json.dumps(
        {
            "responses": [
                {
                    "question": "是否执行远端替换？",
                    "choices_offered": ["执行替换", "先不执行"],
                    "user_response": "执行替换",
                }
            ]
        },
        ensure_ascii=False,
    )
    with sqlite3.connect(state_db) as conn:
        conn.executemany(
            """
            INSERT INTO messages (
                id, session_id, role, content, tool_name, tool_call_id,
                tool_calls, finish_reason, timestamp, platform_message_id,
                display_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, session_id, "user", "请修复文档", None, None, None, None, 1000, "telegram-1", 1),
                (2, session_id, "assistant", "", None, None, tool_calls, "tool_calls", 1001, None, 2),
                (3, session_id, "tool", tool_result, "clarify", tool_call_id, None, None, 1002, None, 3),
            ],
        )
    chain = hermes_turn(
        "turn-1",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:16:43Z",
        "请修复文档",
        "已按确认执行。",
    )
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id, "capture_mode": "sanitized"},
                "observations": [chain],
            }
        ],
    }

    state_reconciliation = module.load_state_reconciliation(
        session_id,
        state_db,
        cutoff_at=datetime.fromtimestamp(1004, tz=timezone.utc),
    )
    candidate = module.build_candidate_document(
        export,
        session_id,
        state_reconciliation=state_reconciliation,
        cutoff_at=datetime.fromtimestamp(1004, tz=timezone.utc),
    )

    assert candidate["turns"] == [
        [
            {
                "role": "user",
                "content": "User: 请修复文档",
                "timestamp": "1970-01-01T00:16:40Z",
            },
            {
                "role": "assistant",
                "content": (
                    "Assistant: 是否执行远端替换？\n\n"
                    "Choices offered:\n- 执行替换\n- 先不执行"
                ),
                "timestamp": "1970-01-01T00:16:41+00:00",
            },
            {
                "role": "user",
                "content": "User: 执行替换",
                "timestamp": "1970-01-01T00:16:42+00:00",
            },
            {
                "role": "assistant",
                "content": "Assistant: 已按确认执行。",
                "timestamp": "1970-01-01T00:16:43Z",
            },
        ]
    ]
    assert candidate["audit"]["state_reconciliation"] == {
        "status": "verified",
        "source_event_count": 3,
        "matched_event_count": 1,
        "added_event_count": 2,
        "uncovered_event_count": 0,
        "platform_user_event_count": 1,
        "redirect_user_event_count": 0,
        "visible_assistant_event_count": 0,
        "clarify_question_event_count": 1,
        "clarify_response_event_count": 1,
    }


@pytest.mark.parametrize("original_source", ["both", "call", "result"])
def test_state_clarify_uses_same_call_original_after_compaction(tmp_path, original_source):
    module = load_script_module(tmp_path)
    session_id = "clarify-original"
    state_db = tmp_path / "state.db"
    _create_reconciliation_state_db(state_db, session_id)
    question = "完整方案：只改本地；不重启、不提交、不推送。"
    choices = ["同意", "暂不"]

    def call(text):
        return json.dumps([{"id": "call-card", "function": {
            "name": "clarify", "arguments": json.dumps({"questions": [
                {"question": text, "choices": choices}
            ]}, ensure_ascii=False)
        }}], ensure_ascii=False)

    response = json.dumps({"responses": [{"question": question,
        "choices_offered": choices, "user_response": "同意"}]}, ensure_ascii=False)
    rows = [
        (3, session_id, "assistant", "", None, None, call("缩短的方案"), 1001, 0, 1, 30),
        (4, session_id, "tool", '[clarify] user responded: ["同意"]', "clarify", "call-card", None, 1002, 1, 0, 40),
    ]
    if original_source in {"both", "call"}:
        rows.append((1, session_id, "assistant", "", None, None, call(question), 1001, 0, 0, 10))
    if original_source in {"both", "result"}:
        rows.append((2, session_id, "tool", response, "clarify", "call-card", None, 1002, 0, 0, 20))
    with sqlite3.connect(state_db) as conn:
        conn.executemany("""INSERT INTO messages
            (id,session_id,role,content,tool_name,tool_call_id,tool_calls,timestamp,active,compacted,display_order)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""", rows)
    cutoff = datetime.fromtimestamp(1003, tz=timezone.utc)
    state = module.load_state_reconciliation(session_id, state_db, cutoff_at=cutoff)
    candidate = module.build_candidate_document(
        {"session_id": session_id, "traces": []}, session_id,
        state_reconciliation=state, cutoff_at=cutoff,
    )
    assert [message["content"] for turn in candidate["turns"] for message in turn] == [
        f"Assistant: {question}\n\nChoices offered:\n- 同意\n- 暂不",
        "User: 同意",
    ]
    assert candidate["audit"]["state_reconciliation"]["uncovered_event_count"] == 0


@pytest.mark.parametrize("boundary", ["other_session", "other_call", "after_cutoff", "no_live_call"])
def test_state_clarify_original_recovery_requires_retained_identity(tmp_path, boundary):
    module = load_script_module(tmp_path)
    state_db = tmp_path / "state.db"
    session_id = "card-boundary"
    _create_reconciliation_state_db(state_db, session_id)

    def call(call_id, text):
        return json.dumps([{"id": call_id, "function": {
            "name": "clarify", "arguments": {"questions": [{"question": text}]}
        }}])

    original_session = "other-session" if boundary == "other_session" else session_id
    original_id = "other-call" if boundary == "other_call" else "card"
    original_time = 1010 if boundary == "after_cutoff" else 1000
    with sqlite3.connect(state_db) as conn:
        conn.executemany("""INSERT INTO messages
            (id,session_id,role,content,tool_name,tool_call_id,tool_calls,timestamp,active,compacted)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", [
                (1, original_session, "assistant", "", None, None, call(original_id, "不可恢复的历史原文"), original_time, 0, 0),
                (2, original_session, "tool", json.dumps({"responses": [{"question": "不可恢复的历史原文", "user_response": "旧答复"}]}), "clarify", original_id, None, original_time, 0, 0),
                (3, session_id, "assistant", "", None, None, call("card", "保留的确认问题"), 1001, 0, 0 if boundary == "no_live_call" else 1),
                (4, session_id, "tool", '[clarify] user responded: ["当前答复"]', "clarify", "card", None, 1002, 1, 0),
            ])
    events = module.load_state_reconciliation(
        session_id, state_db, cutoff_at=datetime.fromtimestamp(1003, tz=timezone.utc)
    )["events"]
    assert [event["content"] for event in events] == (
        [] if boundary == "no_live_call" else ["保留的确认问题", "当前答复"]
    )


@pytest.mark.parametrize("answer", ["同意", "[user did not respond within 15m]"])
def test_state_clarify_original_recovery_keeps_distinct_calls_and_question_order(tmp_path, answer):
    module = load_script_module(tmp_path)
    state_db = tmp_path / "state.db"
    session_id = "repeated-cards"
    _create_reconciliation_state_db(state_db, session_id)
    with sqlite3.connect(state_db) as conn:
        for i, call_id in enumerate(["card-one", "card-two"]):
            original = json.dumps([{"id": call_id, "function": {"name": "clarify", "arguments": {
                "questions": [{"question": "相同问题一"}, {"question": "相同问题二"}]
            }}}])
            response = json.dumps({"responses": [
                {"question": "相同问题一", "user_response": answer},
                {"question": "相同问题二", "user_response": "第二答复"},
            ]})
            conn.executemany("""INSERT INTO messages
                (id,session_id,role,content,tool_name,tool_call_id,tool_calls,timestamp,active,compacted)
                VALUES (?,?,?,?,?,?,?,?,?,?)""", [
                    (i * 4 + 1, session_id, "assistant", "", None, None, original, 1000 + i * 10, 0, 0),
                    (i * 4 + 2, session_id, "tool", response, "clarify", call_id, None, 1001 + i * 10, 0, 0),
                    (i * 4 + 3, session_id, "assistant", "", None, None, original, 1005 + i * 10, 0, 1),
                    (i * 4 + 4, session_id, "tool", '[clarify] user responded: ["副本"]', "clarify", call_id, None, 1006 + i * 10, 0, 1),
                ])
    events = module.load_state_reconciliation(session_id, state_db)["events"]
    expected = ["相同问题一"] + ([answer] if answer == "同意" else []) + ["相同问题二", "第二答复"]
    assert [event["content"] for event in events] == expected * 2
    assert len({event["source_key"] for event in events}) == len(events)
    questions = [event for event in events if event["source_kind"] == "clarify_question"]
    assert [event["timestamp"] for event in questions] == [
        "1970-01-01T00:16:40+00:00", "1970-01-01T00:16:40+00:00",
        "1970-01-01T00:16:50+00:00", "1970-01-01T00:16:50+00:00",
    ]


def test_state_reconciliation_drops_clarify_timeout_answer(tmp_path):
    """A timed-out clarify keeps its question; Hermes' placeholder is not a user reply."""
    module = load_script_module(tmp_path)
    session_id = "session-clarify-timeout-state"
    state_db = tmp_path / "state.db"
    _create_reconciliation_state_db(state_db, session_id)
    tool_call_id = "call-clarify-timeout"
    tool_calls = json.dumps(
        [
            {
                "id": tool_call_id,
                "call_id": tool_call_id,
                "function": {
                    "name": "clarify",
                    "arguments": json.dumps(
                        {"questions": [{"question": "是否抓包？", "choices": ["允许", "暂不"]}]},
                        ensure_ascii=False,
                    ),
                },
            }
        ],
        ensure_ascii=False,
    )
    tool_result = json.dumps(
        {
            "responses": [
                {
                    "question": "是否抓包？",
                    "choices_offered": ["允许", "暂不"],
                    "user_response": "[user did not respond within 15m]",
                }
            ]
        },
        ensure_ascii=False,
    )
    with sqlite3.connect(state_db) as conn:
        conn.executemany(
            """
            INSERT INTO messages (
                id, session_id, role, content, tool_name, tool_call_id,
                tool_calls, finish_reason, timestamp, platform_message_id,
                display_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, session_id, "user", "继续诊断", None, None, None, None, 1000, "telegram-1", 1),
                (2, session_id, "assistant", "", None, None, tool_calls, "tool_calls", 1001, None, 2),
                (3, session_id, "tool", tool_result, "clarify", tool_call_id, None, None, 1002, None, 3),
            ],
        )
    chain = hermes_turn(
        "turn-1",
        "1970-01-01T00:16:40Z",
        "1970-01-01T00:16:43Z",
        "继续诊断",
        "未执行抓包，等待你的明确授权。",
    )
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id, "capture_mode": "sanitized"},
                "observations": [chain],
            }
        ],
    }
    cutoff = datetime.fromtimestamp(1004, tz=timezone.utc)

    state_reconciliation = module.load_state_reconciliation(
        session_id, state_db, cutoff_at=cutoff
    )
    candidate = module.build_candidate_document(
        export,
        session_id,
        state_reconciliation=state_reconciliation,
        cutoff_at=cutoff,
    )

    contents = [message["content"] for turn in candidate["turns"] for message in turn]
    assert contents == [
        "User: 继续诊断",
        "Assistant: 是否抓包？\n\nChoices offered:\n- 允许\n- 暂不",
        "Assistant: 未执行抓包，等待你的明确授权。",
    ]
    audit = candidate["audit"]["state_reconciliation"]
    assert audit["clarify_question_event_count"] == 1
    assert audit["clarify_response_event_count"] == 0
    assert audit["uncovered_event_count"] == 0


def test_clarify_cli_timeout_sentinel_is_not_rendered_as_user_response(tmp_path):
    module = load_script_module(tmp_path)
    from tools import clarify_tool

    assert module._CLARIFY_CLI_TIMEOUT_RESPONSE == clarify_tool.TIMEOUT_RESPONSE
    session_id = "session-clarify-cli-timeout"
    chain = hermes_turn(
        "turn-1",
        "2026-08-24T01:00:00Z",
        "2026-08-24T01:02:00Z",
        "用户问题",
        "按推荐方案继续",
    )
    clarify = {
        "id": "clarify-cli-timeout",
        "parentObservationId": "turn-1",
        "type": "TOOL",
        "name": "Tool: clarify",
        "startTime": "2026-08-24T01:00:30Z",
        "endTime": "2026-08-24T01:01:30Z",
        "input": {"question": "请选择", "choices": ["甲", "乙"]},
        "output": {"user_response": clarify_tool.TIMEOUT_RESPONSE},
    }
    real = dict(
        clarify,
        id="clarify-real",
        startTime="2026-08-24T01:01:40Z",
        endTime="2026-08-24T01:01:50Z",
        output={"user_response": "The user did not respond within 15m, so I chose 甲"},
    )
    export = {
        "session_id": session_id,
        "traces": [
            {"metadata": {"task_id": session_id}, "observations": [chain, clarify, real]}
        ],
    }

    candidate = module.build_candidate_document(export, session_id)

    contents = [message["content"] for turn in candidate["turns"] for message in turn]
    assert all(clarify_tool.TIMEOUT_RESPONSE not in content for content in contents)
    assert "User: The user did not respond within 15m, so I chose 甲" in contents


def test_kanban_wake_guidance_literals_match_locales():
    import yaml

    locales = Path(__file__).resolve().parents[2] / "locales"
    module = load_script_module(Path("."))
    guidance = set()
    for path in locales.glob("*.yaml"):
        wake = yaml.safe_load(path.read_text(encoding="utf-8"))["gateway"]["kanban"]["wake"]
        assert wake["message"].startswith("[kanban] ")
        assert "{task_id}" in wake["message"].split("\n", 1)[0]
        guidance.add(wake["guidance"])
    assert guidance == set(module._KANBAN_WAKE_GUIDANCE)


def test_kanban_wake_turn_is_excluded_but_user_kanban_text_is_kept(tmp_path):
    module = load_script_module(tmp_path)
    session_id = "session-kanban-wake"
    wake = (
        "[kanban] 任务 t_8e672585 被阻塞，需要处理。\n标题: 复核目录\n执行者: @default\n"
        "看板: default\n\n请检查结果或决定下一步动作。\n\n"
        + module._KANBAN_WAKE_GUIDANCE[1]
    )
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id},
                "observations": [
                    hermes_turn(
                        "turn-wake",
                        "2026-09-02T08:00:00Z",
                        "2026-09-02T08:00:10Z",
                        wake,
                        "任务被阻塞，我先检查看板。",
                    ),
                    hermes_turn(
                        "turn-user",
                        "2026-09-02T08:01:00Z",
                        "2026-09-02T08:01:10Z",
                        "[kanban] 任务 t_8e672585 为什么被阻塞？",
                        "因为目录名不一致。",
                    ),
                ],
            }
        ],
    }

    candidate = module.build_candidate_document(export, session_id)

    contents = [message["content"] for turn in candidate["turns"] for message in turn]
    assert contents == [
        "Assistant: 任务被阻塞，我先检查看板。",
        "User: [kanban] 任务 t_8e672585 为什么被阻塞？",
        "Assistant: 因为目录名不一致。",
    ]


def test_cli_applies_state_reconciliation_to_generated_candidate(tmp_path, monkeypatch):
    module = load_script_module(tmp_path)
    session_id = "session-cli-reconciliation"
    state_db = tmp_path / "state.db"
    _create_reconciliation_state_db(state_db, session_id)
    with sqlite3.connect(state_db) as conn:
        conn.execute(
            """
            INSERT INTO messages (
                id, session_id, role, content, timestamp, platform_message_id,
                display_order
            ) VALUES (1, ?, 'user', 'CLI 真实开场', 1000, 'telegram-cli', 1)
            """,
            (session_id,),
        )
    export = {
        "session_id": session_id,
        "traces": [
            {
                "metadata": {"task_id": session_id, "capture_mode": "sanitized"},
                "observations": [
                    hermes_turn(
                        "turn-1",
                        "1970-01-01T00:16:41Z",
                        "1970-01-01T00:16:42Z",
                        (
                            '<hermes-runtime-context user-authored="false" '
                            'source="long-task-continuity">内部恢复</hermes-runtime-context>'
                        ),
                        "CLI 最终回答",
                    )
                ],
            }
        ],
    }
    output_dir = tmp_path / "output"
    monkeypatch.setattr(module, "export_langfuse", lambda *_: export)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "langfuse_hindsight_export.py",
            "--session-id",
            session_id,
            "--output-dir",
            str(output_dir),
            "--cutoff-at",
            "1970-01-01T00:16:43Z",
            "--skip-hindsight",
            "--sqlite-path",
            str(tmp_path / "missing-retain.sqlite3"),
            "--state-db-path",
            str(state_db),
        ],
    )

    assert module.main() == 0

    candidate = json.loads(
        (output_dir / f"candidate_document_{session_id}.json").read_text()
    )
    assert candidate["turns"][0][0]["content"] == "User: CLI 真实开场"
    assert candidate["audit"]["state_reconciliation"]["status"] == "verified"


def test_state_reconciliation_does_not_treat_quoted_response_as_present(tmp_path):
    module = load_script_module(tmp_path)
    turns = [
        [
            {
                "role": "user",
                "content": "User: 你再漏掉“执行替换”我看看",
                "timestamp": "1970-01-01T00:16:42Z",
            }
        ]
    ]
    reconciliation = {
        "status": "ready",
        "events": [
            {
                "source_kind": "clarify_response",
                "source_key": "clarify_response:call-1:0",
                "role": "user",
                "content": "执行替换",
                "timestamp": "1970-01-01T00:16:41Z",
                "display_order": 1,
            }
        ],
    }

    reconciled, audit = module._apply_state_reconciliation(turns, reconciliation)

    assert [message["content"] for message in reconciled[0]] == [
        "User: 执行替换",
        "User: 你再漏掉“执行替换”我看看",
    ]
    assert audit["matched_event_count"] == 0
    assert audit["added_event_count"] == 1


def test_state_reconciliation_preserves_equal_timestamp_event_order(tmp_path):
    module = load_script_module(tmp_path)
    turns = [
        [
            {
                "role": "assistant",
                "content": "Assistant: 最终回答",
                "timestamp": "1970-01-01T00:16:43Z",
            }
        ]
    ]
    reconciliation = {
        "status": "ready",
        "events": [
            {
                "source_kind": "clarify_question",
                "source_key": "clarify_question:call-1:0",
                "role": "assistant",
                "content": "第一个问题",
                "timestamp": "1970-01-01T00:16:41Z",
                "display_order": 1,
            },
            {
                "source_kind": "clarify_question",
                "source_key": "clarify_question:call-1:1",
                "role": "assistant",
                "content": "第二个问题",
                "timestamp": "1970-01-01T00:16:41Z",
                "display_order": 1,
            },
        ],
    }

    reconciled, _ = module._apply_state_reconciliation(turns, reconciliation)

    assert [message["content"] for message in reconciled[0]] == [
        "Assistant: 第一个问题",
        "Assistant: 第二个问题",
        "Assistant: 最终回答",
    ]


def test_state_reconciliation_requires_one_candidate_occurrence_per_state_event(tmp_path):
    module = load_script_module(tmp_path)
    turns = [
        [
            {
                "role": "user",
                "content": "User: 继续",
                "timestamp": "1970-01-01T00:16:41Z",
            },
            {
                "role": "assistant",
                "content": "Assistant: 已继续一次",
                "timestamp": "1970-01-01T00:16:42Z",
            },
        ]
    ]
    reconciliation = {
        "status": "ready",
        "events": [
            {
                "source_kind": "platform_user",
                "source_key": "platform_user:100",
                "role": "user",
                "content": "继续",
                "timestamp": "1970-01-01T00:16:41Z",
                "display_order": 1,
            },
            {
                "source_kind": "platform_user",
                "source_key": "platform_user:101",
                "role": "user",
                "content": "继续",
                "timestamp": "1970-01-01T00:16:43Z",
                "display_order": 2,
            },
        ],
    }

    reconciled, audit = module._apply_state_reconciliation(turns, reconciliation)
    messages = [message for turn in reconciled for message in turn]

    assert [message["content"] for message in messages].count("User: 继续") == 2
    assert audit["matched_event_count"] == 1
    assert audit["added_event_count"] == 1
