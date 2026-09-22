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
                display_order INTEGER
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
        "visible_assistant_event_count": 0,
        "clarify_question_event_count": 0,
        "clarify_response_event_count": 0,
    }


def test_state_reconciliation_drops_compaction_replayed_assistant_copies(tmp_path):
    """A compaction block replays earlier assistant text with fresh timestamps.

    The (body, timestamp) dedupe key cannot see those copies, so each compaction
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
        (1, "user", "第一个问题", 1000, 1),
        (2, "assistant", "第一个回答", 1001, 2),
        (3, "user", "第二个问题", 1002, 3),
        (4, "assistant", "第二个回答", 1003, 4),
        # First compaction: marker, then the replayed prefix at fresh timestamps.
        (5, "assistant", marker, 1004, 5),
        (6, "user", "第一个问题", 1004.1, 6),
        (7, "assistant", "第一个回答", 1004.2, 7),
        (8, "user", "第二个问题", 1004.3, 8),
        (9, "assistant", "第二个回答", 1004.4, 9),
        # Second compaction replays the same prefix again.
        (10, "assistant", marker, 1005, 10),
        (11, "user", "第一个问题", 1005.1, 11),
        (12, "assistant", "第一个回答", 1005.2, 12),
        (13, "user", "第二个问题", 1005.3, 13),
        (14, "assistant", "第二个回答", 1005.4, 14),
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

    # Each distinct reply survives exactly once, despite two replays apiece.
    assert assistant_bodies == ["第一个回答", "第二个回答"]


def test_state_reconciliation_keeps_genuine_repeat_outside_compaction(tmp_path):
    """Deduping must stay scoped to replay blocks, never global text matching.

    A user can legitimately get the same short answer twice in one session; only
    copies inside a compaction replay block are duplicates.
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
        "visible_assistant_event_count": 1,
        "clarify_question_event_count": 0,
        "clarify_response_event_count": 0,
    }


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
        "visible_assistant_event_count": 0,
        "clarify_question_event_count": 1,
        "clarify_response_event_count": 1,
    }


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
