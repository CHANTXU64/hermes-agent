from __future__ import annotations

import importlib.util
import io
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "fork_features"
    / "hindsight_retain"
    / "retain_integrity.py"
)
CHECKER_PATH = Path.home() / ".hermes" / "scripts" / "check-hermes-hindsight.py"
HTML_MONITOR_PATH = Path.home() / ".hermes" / "scripts" / "hindsight_monitor_html.py"


def load_path_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_module():
    return load_path_module("retain_integrity", MODULE_PATH)


def create_state_db(path: Path, session_id: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT,
                session_key TEXT,
                profile_name TEXT,
                started_at REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT,
                role TEXT,
                content TEXT,
                timestamp REAL,
                active INTEGER,
                compacted INTEGER,
                display_kind TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO sessions VALUES (?, 'gateway', 'agent:main:telegram:dm:1', 'default', ?)",
            (session_id, 1000.0),
        )
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, 1, 0, NULL)",
            [
                (1, session_id, "user", "first request", 1001.0),
                (2, session_id, "assistant", "first answer", 1002.0),
            ],
        )


def write_valid_exporter(path: Path, *, reconciliation_status: str = "verified") -> None:
    script = """
import argparse
import hashlib
import json
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("--session-id", required=True)
parser.add_argument("--skip-hindsight", action="store_true")
parser.add_argument("--cutoff-at")
parser.add_argument("--state-db-path")
parser.add_argument("--output-dir", type=Path, required=True)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
turns = [[{"role": "user", "content": "User: persist this"}, {"role": "assistant", "content": "Assistant: persisted"}]]
content = json.dumps(turns, separators=(",", ":"))
digest = hashlib.sha256(content.encode()).hexdigest()
candidate = args.output_dir / f"candidate_document_{args.session_id}.json"
reconciliation = {"status": __RECONCILIATION_STATUS__, "source_event_count": 2, "matched_event_count": 2, "added_event_count": 0, "uncovered_event_count": 0}
candidate.write_text(json.dumps({"schema_version": "hindsight-conversation-document-v1", "session_id": args.session_id, "document_id": args.session_id, "turns": turns, "document_content": content, "document_content_sha256": digest, "audit": {"candidate_turn_count": 1, "candidate_message_count": 2, "state_reconciliation": reconciliation}}))
(args.output_dir / "manifest.json").write_text(json.dumps({"session_id": args.session_id, "read_only": True, "received_cutoff_at": args.cutoff_at, "received_state_db_path": args.state_db_path, "files": [str(candidate)]}))
print(json.dumps({"session_id": args.session_id}))
""".lstrip().replace("__RECONCILIATION_STATUS__", repr(reconciliation_status))
    path.write_text(script, encoding="utf-8")


def create_accepted_attempt(
    module,
    tmp_path: Path,
    *,
    session_id: str,
    attempt_id: str,
    started_at: datetime,
):
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    exporter = tmp_path / "fake_exporter.py"
    create_state_db(state_db, session_id)
    write_valid_exporter(exporter)
    result = module.run_export(
        session_id=session_id,
        output_root=tmp_path / "runs",
        journal_path=journal,
        state_db_path=state_db,
        export_script=exporter,
        python_executable=sys.executable,
        remote_expectation="expected",
        attempt_id=attempt_id,
        now=started_at,
        remote_writer=lambda payload: {
            "success": True,
            "bank_id": "Hermes",
            "items_count": 1,
            "async": True,
            "operation_id": payload["operation_id"],
        },
    )
    return state_db, journal, result


def test_unverified_state_reconciliation_blocks_remote_write(tmp_path: Path) -> None:
    module = load_module()
    session_id = "20260914_115000_a0b1c2d3"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    exporter = tmp_path / "unverified_exporter.py"
    create_state_db(state_db, session_id)
    write_valid_exporter(exporter, reconciliation_status="not_requested")
    remote_payloads: list[dict] = []

    result = module.run_export(
        session_id=session_id,
        output_root=tmp_path / "runs",
        journal_path=journal,
        state_db_path=state_db,
        export_script=exporter,
        python_executable=sys.executable,
        remote_expectation="expected",
        attempt_id="c1d2e3f4-5678-4abc-8def-0123456789ab",
        now=datetime(2026, 9, 14, 3, 50, tzinfo=timezone.utc),
        remote_writer=lambda payload: remote_payloads.append(payload),
    )

    assert remote_payloads == []
    assert result["status"] == "blocked_unverified_candidate"
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert events[-1]["event"] == "remote_write_blocked_unverified_candidate"
    assert events[-1]["state_reconciliation_status"] == "not_requested"

    scan = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=datetime(2026, 9, 14, 4, 0, tzinfo=timezone.utc),
        operation_fetcher=lambda _operation_id: (_ for _ in ()).throw(
            AssertionError("blocked candidate queried remote operation")
        ),
        document_fetcher=lambda _document_id: (_ for _ in ()).throw(
            AssertionError("blocked candidate queried remote document")
        ),
    )
    assert scan["alerts"] == [
        {
            "alert_key": (
                "retain:c1d2e3f4-5678-4abc-8def-0123456789ab:"
                "candidate_reconciliation_unverified"
            ),
            "type": "retain_candidate_reconciliation_unverified",
            "severity": "high",
            "attempt_id": "c1d2e3f4-5678-4abc-8def-0123456789ab",
            "session_id": session_id,
            "document_id": session_id,
            "started_at": "2026-09-14T03:50:00+00:00",
            "state_session_found": True,
            "state_reconciliation_status": "not_requested",
            "remote_write_status": "blocked_before_submit",
            "message": "Retain 候选未完成 StateDB 可见事件对账，已在提交远端前拦截",
        }
    ]


def test_severely_incomplete_candidate_blocks_remote_write_before_submit(
    tmp_path: Path,
) -> None:
    module = load_module()
    session_id = "20260914_120000_a1b2c3d4"
    attempt_id = "b0f9bf22-9e09-455b-b867-d8cb0fc22775"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    exporter = tmp_path / "fake_exporter.py"
    config = tmp_path / "config.json"
    create_state_db(state_db, session_id)
    with sqlite3.connect(state_db) as conn:
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, 1, 0, NULL)",
            [
                (
                    message_id,
                    session_id,
                    "user" if message_id % 2 else "assistant",
                    f"message {message_id}",
                    1000.0 + message_id,
                )
                for message_id in range(3, 11)
            ],
        )
    write_valid_exporter(exporter)
    config.write_text(json.dumps({"bank_id": "Hermes"}), encoding="utf-8")
    remote_payloads: list[dict] = []

    result = module.run_export(
        session_id=session_id,
        output_root=tmp_path / "runs",
        journal_path=journal,
        state_db_path=state_db,
        export_script=exporter,
        python_executable=sys.executable,
        remote_expectation="expected",
        attempt_id=attempt_id,
        now=datetime(2026, 9, 14, 4, 0, tzinfo=timezone.utc),
        hindsight_config_path=config,
        remote_writer=lambda payload: remote_payloads.append(payload),
    )

    assert remote_payloads == []
    assert result["status"] == "blocked_incomplete_candidate"
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [event["event"] for event in events] == [
        "started",
        "export_succeeded",
        "remote_write_blocked_incomplete_candidate",
    ]
    assert events[-1]["state_active_message_count"] == 10
    assert events[-1]["candidate_message_count"] == 2
    assert events[-1]["missing_message_count"] == 8


def test_single_missing_visible_assistant_blocks_remote_write_before_submit(
    tmp_path: Path,
) -> None:
    module = load_module()
    session_id = "20260914_120500_a1b2c3d4"
    attempt_id = "a6bdd87b-61b0-497b-863b-02942ee78661"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    exporter = tmp_path / "fake_exporter.py"
    config = tmp_path / "config.json"
    create_state_db(state_db, session_id)
    with sqlite3.connect(state_db) as conn:
        conn.execute(
            "INSERT INTO messages VALUES (3, ?, 'assistant', 'second answer', 1003, 1, 0, NULL)",
            (session_id,),
        )
    write_valid_exporter(exporter)
    config.write_text(json.dumps({"bank_id": "Hermes"}), encoding="utf-8")
    remote_payloads: list[dict] = []

    def remote_writer(payload: dict) -> dict:
        remote_payloads.append(payload)
        return {
            "success": True,
            "bank_id": "Hermes",
            "items_count": 1,
            "async": True,
            "operation_id": payload["operation_id"],
        }

    result = module.run_export(
        session_id=session_id,
        output_root=tmp_path / "runs",
        journal_path=journal,
        state_db_path=state_db,
        export_script=exporter,
        python_executable=sys.executable,
        remote_expectation="expected",
        attempt_id=attempt_id,
        now=datetime(2026, 9, 14, 4, 5, tzinfo=timezone.utc),
        hindsight_config_path=config,
        remote_writer=remote_writer,
    )

    assert remote_payloads == []
    assert result["status"] == "blocked_visible_event_gap"
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert events[-1]["event"] == "remote_write_blocked_visible_event_gap"
    assert events[-1]["missing_user_count"] == 0
    assert events[-1]["missing_assistant_count"] == 1

    scan = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=datetime(2026, 9, 14, 4, 15, tzinfo=timezone.utc),
        operation_fetcher=lambda _operation_id: (_ for _ in ()).throw(
            AssertionError("visible event gap queried remote operation")
        ),
        document_fetcher=lambda _document_id: (_ for _ in ()).throw(
            AssertionError("visible event gap queried remote document")
        ),
    )
    assert scan["alerts"] == [
        {
            "alert_key": f"retain:{attempt_id}:candidate_visible_event_gap",
            "type": "retain_candidate_visible_event_gap",
            "severity": "high",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "document_id": session_id,
            "started_at": "2026-09-14T04:05:00+00:00",
            "state_session_found": True,
            "required_user_count": 1,
            "required_assistant_count": 2,
            "candidate_user_count": 1,
            "candidate_assistant_count": 1,
            "missing_user_count": 0,
            "missing_assistant_count": 1,
            "remote_write_status": "blocked_before_submit",
            "message": "Retain 候选少了可见用户或 AI 对话，已在提交远端前拦截",
        }
    ]


def test_state_snapshot_counts_compacted_visible_assistant(tmp_path: Path) -> None:
    module = load_module()
    session_id = "20260914_120700_a1b2c3d4"
    state_db = tmp_path / "state.db"
    create_state_db(state_db, session_id)
    with sqlite3.connect(state_db) as conn:
        conn.execute(
            "INSERT INTO messages VALUES (3, ?, 'assistant', 'compacted answer', 1003, 0, 1, NULL)",
            (session_id,),
        )

    snapshot = module._state_snapshot(
        state_db,
        session_id,
        cutoff_at=datetime(2026, 9, 14, 4, 6, tzinfo=timezone.utc),
    )

    assert snapshot["active_user_count"] == 1
    assert snapshot["active_assistant_count"] == 2
    assert snapshot["active_message_count"] == 3


def test_blocked_incomplete_candidate_scans_as_one_explicit_alert(
    tmp_path: Path,
) -> None:
    module = load_module()
    session_id = "20260914_121000_b1c2d3e4"
    attempt_id = "c4c8657f-346d-4b5d-83e0-8467b7da6b58"
    started_at = datetime(2026, 9, 14, 4, 10, tzinfo=timezone.utc)
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    exporter = tmp_path / "fake_exporter.py"
    config = tmp_path / "config.json"
    create_state_db(state_db, session_id)
    with sqlite3.connect(state_db) as conn:
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, 1, 0, NULL)",
            [
                (
                    message_id,
                    session_id,
                    "user" if message_id % 2 else "assistant",
                    f"message {message_id}",
                    1000.0 + message_id,
                )
                for message_id in range(3, 11)
            ],
        )
    write_valid_exporter(exporter)
    config.write_text(json.dumps({"bank_id": "Hermes"}), encoding="utf-8")
    module.run_export(
        session_id=session_id,
        output_root=tmp_path / "runs",
        journal_path=journal,
        state_db_path=state_db,
        export_script=exporter,
        python_executable=sys.executable,
        remote_expectation="expected",
        attempt_id=attempt_id,
        now=started_at,
        hindsight_config_path=config,
        remote_writer=lambda _payload: (_ for _ in ()).throw(
            AssertionError("incomplete candidate reached remote writer")
        ),
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=started_at + timedelta(minutes=10),
        operation_fetcher=lambda _operation_id: (_ for _ in ()).throw(
            AssertionError("blocked candidate queried remote operation")
        ),
        document_fetcher=lambda _document_id: (_ for _ in ()).throw(
            AssertionError("blocked candidate queried remote document")
        ),
    )

    assert result["alerts"] == [
        {
            "alert_key": f"retain:{attempt_id}:candidate_severely_incomplete",
            "type": "retain_candidate_severely_incomplete",
            "severity": "high",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "document_id": session_id,
            "started_at": started_at.isoformat(),
            "state_session_found": True,
            "state_active_message_count": 10,
            "candidate_message_count": 2,
            "missing_message_count": 8,
            "remote_write_status": "blocked_before_submit",
            "message": "Retain 候选相对开始时的 StateDB 会话少了一大块有效用户或 AI 消息",
        }
    ]


def test_torn_journal_tail_preserves_prior_attempts_and_alerts(tmp_path: Path) -> None:
    module = load_module()
    now = datetime(2026, 8, 25, 7, 59, tzinfo=timezone.utc)
    session_id = "20260825_155900_a0b0c0d0"
    journal = tmp_path / "retain-attempts.jsonl"
    state_db = tmp_path / "state.db"
    create_state_db(state_db, session_id)
    complete = {
        "schema_version": 1,
        "attempt_id": "attempt-before-torn-tail",
        "event": "started",
        "recorded_at": (now - timedelta(minutes=10)).isoformat(),
        "session_id": session_id,
        "document_id": session_id,
        "remote_expectation": "not_expected_export_only",
        "output_dir": str(tmp_path / "runs"),
    }
    complete_line = json.dumps(complete, separators=(",", ":")) + "\n"
    torn = b'{"schema_version":1,"attempt_id":"partial'
    journal.write_bytes(complete_line.encode() + torn)

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=now,
        grace_seconds=300,
    )

    assert result["journal_torn_tail"] is True
    assert result["attempt_count"] == 1
    assert [alert["type"] for alert in result["alerts"]] == [
        "retain_journal_torn_tail",
        "retain_attempt_incomplete",
    ]
    assert result["alerts"][0]["line_number"] == 2
    assert result["alerts"][0]["discarded_bytes"] == len(torn)


def test_scheduled_worker_missing_is_reported_after_due_grace(tmp_path: Path) -> None:
    module = load_module()
    now = datetime(2026, 8, 26, 5, 0, tzinfo=timezone.utc)
    session_id = "20260826_043000_ff001122"
    attempt_id = "2cc7f90f-6d5c-42f6-b857-f00f8ffc96bc"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    create_state_db(state_db, session_id)
    journal.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "attempt_id": attempt_id,
                "event": "scheduled",
                "recorded_at": (now - timedelta(minutes=30)).isoformat(),
                "requested_at": (now - timedelta(minutes=30)).isoformat(),
                "cutoff_at": (now - timedelta(minutes=30)).isoformat(),
                "due_at": (now - timedelta(minutes=10)).isoformat(),
                "delay_seconds": 1200,
                "session_id": session_id,
                "document_id": session_id,
                "remote_expectation": "expected",
                "non_durable_worker": True,
            }
        )
        + "\n"
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=now,
        grace_seconds=300,
    )

    assert result["alerts"] == [
        {
            "alert_key": f"retain:{attempt_id}:scheduled_worker_missing",
            "type": "retain_scheduled_worker_missing",
            "severity": "high",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "document_id": session_id,
            "scheduled_at": (now - timedelta(minutes=30)).isoformat(),
            "due_at": (now - timedelta(minutes=10)).isoformat(),
            "state_session_found": True,
            "message": "Retain 已到期，但非持久延迟子进程没有开始提取",
        }
    ]


def test_started_only_attempt_is_reported_after_grace(tmp_path: Path) -> None:
    module = load_module()
    now = datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc)
    started_at = now - timedelta(minutes=10)
    session_id = "20260825_155000_deadbeef"
    journal = tmp_path / "retain-attempts.jsonl"
    state_db = tmp_path / "state.db"
    create_state_db(state_db, session_id)
    journal.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "attempt_id": "attempt-crash",
                "event": "started",
                "recorded_at": started_at.isoformat(),
                "session_id": session_id,
                "document_id": session_id,
                "remote_expectation": "not_expected_export_only",
                "output_dir": str(tmp_path / "runs" / session_id / "attempt-crash"),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=now,
        grace_seconds=300,
    )

    assert result["status"] == "ok"
    assert result["attempt_count"] == 1
    assert result["alerts"] == [
        {
            "alert_key": "retain:attempt-crash:attempt_incomplete",
            "type": "retain_attempt_incomplete",
            "severity": "high",
            "attempt_id": "attempt-crash",
            "session_id": session_id,
            "document_id": session_id,
            "started_at": started_at.isoformat(),
            "state_session_found": True,
            "message": "Retain 已开始，但宽限期后仍没有本地完成或失败记录",
        }
    ]


def test_successful_export_writes_started_and_terminal_receipts(tmp_path: Path) -> None:
    module = load_module()
    observed_at = datetime(2026, 8, 25, 8, 30, tzinfo=timezone.utc)
    session_id = "20260825_163000_feedface"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    output_root = tmp_path / "runs"
    create_state_db(state_db, session_id)
    exporter = tmp_path / "fake_exporter.py"
    exporter.write_text(
        """
import argparse
import hashlib
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--session-id", required=True)
parser.add_argument("--skip-hindsight", action="store_true")
parser.add_argument("--cutoff-at")
parser.add_argument("--state-db-path")
parser.add_argument("--output-dir", type=Path, required=True)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
candidate = args.output_dir / f"candidate_document_{args.session_id}.json"
turns = [[
    {"role": "user", "content": "User: first request", "timestamp": "2026-08-25T08:00:01Z"},
    {"role": "assistant", "content": "Assistant: first answer", "timestamp": "2026-08-25T08:00:02Z"},
]]
content = json.dumps(turns, separators=(",", ":"))
candidate_payload = {
    "schema_version": "hindsight-conversation-document-v1",
    "session_id": args.session_id,
    "document_id": args.session_id,
    "turns": turns,
    "document_content": content,
    "document_content_sha256": hashlib.sha256(content.encode()).hexdigest(),
    "audit": {"candidate_turn_count": 1, "candidate_message_count": 2, "state_reconciliation": {"status": "verified", "source_event_count": 2, "matched_event_count": 2, "added_event_count": 0, "uncovered_event_count": 0}},
}
candidate.write_text(json.dumps(candidate_payload), encoding="utf-8")
manifest = {
    "session_id": args.session_id,
    "read_only": True,
    "files": [str(candidate)],
    "summary": {"candidate_document": candidate_payload["audit"]},
}
manifest_path = args.output_dir / "manifest.json"
manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
print(json.dumps(manifest))
""".lstrip(),
        encoding="utf-8",
    )

    result = module.run_export(
        session_id=session_id,
        output_root=output_root,
        journal_path=journal,
        state_db_path=state_db,
        export_script=exporter,
        python_executable=sys.executable,
        remote_expectation="not_expected_export_only",
        attempt_id="attempt-ok",
        now=observed_at,
    )

    assert result["attempt_id"] == "attempt-ok"
    assert result["remote_expectation"] == "not_expected_export_only"
    assert result["output_dir"] == str(output_root / session_id / "attempt-ok")
    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["started", "export_succeeded"]
    assert {event["attempt_id"] for event in events} == {"attempt-ok"}
    assert events[0]["state_snapshot"] == {
        "session_found": True,
        "active_user_count": 1,
        "active_assistant_count": 1,
        "active_message_count": 2,
        "max_message_id": 2,
    }
    assert events[1]["candidate_message_count"] == 2
    assert Path(events[1]["manifest_path"]).is_file()


def test_expected_remote_document_missing_after_grace_is_high_alert(tmp_path: Path) -> None:
    module = load_module()
    now = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
    started_at = now - timedelta(minutes=15)
    session_id = "20260825_164500_aabbccdd"
    attempt_id = "attempt-remote-missing"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    output_dir = tmp_path / "runs" / session_id / attempt_id
    output_dir.mkdir(parents=True)
    create_state_db(state_db, session_id)
    turns = [[
        {"role": "user", "content": "User: first request"},
        {"role": "assistant", "content": "Assistant: first answer"},
    ]]
    candidate_content = json.dumps(turns, separators=(",", ":"))
    candidate_sha = hashlib.sha256(candidate_content.encode()).hexdigest()
    candidate_path = output_dir / f"candidate_document_{session_id}.json"
    candidate_path.write_text(
        json.dumps(
            {
                "schema_version": "hindsight-conversation-document-v1",
                "session_id": session_id,
                "document_id": session_id,
                "turns": turns,
                "document_content": candidate_content,
                "document_content_sha256": candidate_sha,
                "audit": {"candidate_turn_count": 1, "candidate_message_count": 2, "state_reconciliation": {"status": "verified", "source_event_count": 2, "matched_event_count": 2, "added_event_count": 0, "uncovered_event_count": 0}},
            }
        ),
        encoding="utf-8",
    )
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps({"session_id": session_id, "read_only": False, "files": [str(candidate_path)]}),
        encoding="utf-8",
    )
    events = [
        {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "event": "started",
            "recorded_at": started_at.isoformat(),
            "session_id": session_id,
            "document_id": session_id,
            "remote_expectation": "expected",
            "output_dir": str(output_dir),
        },
        {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "event": "export_succeeded",
            "recorded_at": (started_at + timedelta(seconds=5)).isoformat(),
            "session_id": session_id,
            "document_id": session_id,
            "manifest_path": str(manifest_path),
            "candidate_path": str(candidate_path),
            "candidate_sha256": candidate_sha,
            "candidate_turn_count": 1,
            "candidate_message_count": 2,
        },
        {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "event": "remote_write_started",
            "recorded_at": (started_at + timedelta(seconds=6)).isoformat(),
            "session_id": session_id,
            "document_id": session_id,
            "operation_id": attempt_id,
            "bank_id": "Hermes",
            "update_mode": "replace",
            "candidate_sha256": candidate_sha,
        },
    ]
    journal.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=now,
        grace_seconds=300,
        operation_fetcher=lambda operation_id: {
            "status": "found",
            "operation_id": operation_id,
            "operation": {
                "id": operation_id,
                "task_type": "batch_retain",
                "status": "completed",
                "document_id": session_id,
                "items_count": 1,
                "extraction_errors_count": 0,
            },
        },
        document_fetcher=lambda document_id: {"status": "missing", "document_id": document_id},
    )

    assert result["alerts"] == [
        {
            "alert_key": f"retain:{attempt_id}:remote_document_missing",
            "type": "retain_expected_remote_document_missing",
            "severity": "high",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "document_id": session_id,
            "started_at": started_at.isoformat(),
            "state_session_found": True,
            "message": "Retain 本地候选已完成，但宽限期后 Hindsight 中没有对应 Document",
        }
    ]


def test_candidate_with_large_state_db_gap_is_high_alert(tmp_path: Path) -> None:
    module = load_module()
    now = datetime(2026, 8, 25, 9, 30, tzinfo=timezone.utc)
    started_at = now - timedelta(minutes=10)
    session_id = "20260825_170000_11223344"
    attempt_id = "attempt-severe-candidate-gap"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    output_dir = tmp_path / "runs" / session_id / attempt_id
    output_dir.mkdir(parents=True)
    create_state_db(state_db, session_id)
    with sqlite3.connect(state_db) as conn:
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, 1, 0, NULL)",
            [
                (message_id, session_id, "user" if message_id % 2 else "assistant", f"message {message_id}", 1000.0 + message_id)
                for message_id in range(3, 11)
            ],
        )
    turns = [[
        {"role": "user", "content": "User: first request"},
        {"role": "assistant", "content": "Assistant: first answer"},
    ]]
    candidate_content = json.dumps(turns, separators=(",", ":"))
    candidate_sha = hashlib.sha256(candidate_content.encode()).hexdigest()
    candidate_path = output_dir / f"candidate_document_{session_id}.json"
    candidate_path.write_text(
        json.dumps(
            {
                "schema_version": "hindsight-conversation-document-v1",
                "session_id": session_id,
                "document_id": session_id,
                "turns": turns,
                "document_content": candidate_content,
                "document_content_sha256": candidate_sha,
                "audit": {"candidate_turn_count": 1, "candidate_message_count": 2, "state_reconciliation": {"status": "verified", "source_event_count": 2, "matched_event_count": 2, "added_event_count": 0, "uncovered_event_count": 0}},
            }
        ),
        encoding="utf-8",
    )
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"session_id": session_id}), encoding="utf-8")
    journal.write_text(
        "".join(
            json.dumps(event) + "\n"
            for event in [
                {
                    "schema_version": 1,
                    "attempt_id": attempt_id,
                    "event": "started",
                    "recorded_at": started_at.isoformat(),
                    "session_id": session_id,
                    "document_id": session_id,
                    "remote_expectation": "not_expected_export_only",
                    "output_dir": str(output_dir),
                    "state_snapshot": {
                        "session_found": True,
                        "active_user_count": 5,
                        "active_assistant_count": 5,
                        "active_message_count": 10,
                        "max_message_id": 10,
                    },
                },
                {
                    "schema_version": 1,
                    "attempt_id": attempt_id,
                    "event": "export_succeeded",
                    "recorded_at": (started_at + timedelta(seconds=5)).isoformat(),
                    "session_id": session_id,
                    "document_id": session_id,
                    "manifest_path": str(manifest_path),
                    "candidate_path": str(candidate_path),
                    "candidate_sha256": candidate_sha,
                    "candidate_turn_count": 1,
                    "candidate_message_count": 2,
                },
            ]
        ),
        encoding="utf-8",
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=now,
        grace_seconds=300,
    )

    assert result["alerts"] == [
        {
            "alert_key": f"retain:{attempt_id}:candidate_severely_incomplete",
            "type": "retain_candidate_severely_incomplete",
            "severity": "high",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "document_id": session_id,
            "started_at": started_at.isoformat(),
            "state_session_found": True,
            "state_active_message_count": 10,
            "candidate_message_count": 2,
            "missing_message_count": 8,
            "message": "Retain 候选相对开始时的 StateDB 会话少了一大块有效用户或 AI 消息",
        }
    ]


def test_verified_confirmed_only_repair_uses_repair_scope_instead_of_live_session_gap(
    tmp_path: Path,
) -> None:
    module = load_module()
    now = datetime(2026, 9, 14, 5, 0, tzinfo=timezone.utc)
    started_at = now - timedelta(minutes=10)
    session_id = "20260824_160102_e39c34be"
    attempt_id = "42d44e79-ecfd-4b36-a553-93df85e532dd"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    output_dir = tmp_path / "runs" / session_id / attempt_id
    output_dir.mkdir(parents=True)
    create_state_db(state_db, session_id)
    with sqlite3.connect(state_db) as conn:
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, 1, 0, NULL)",
            [
                (
                    message_id,
                    session_id,
                    "user" if message_id % 2 else "assistant",
                    f"message {message_id}",
                    1000.0 + message_id,
                )
                for message_id in range(3, 11)
            ],
        )
    turns = [
        [
            {"role": "user", "content": "User: retained request"},
            {"role": "assistant", "content": "Assistant: retained answer"},
        ]
    ]
    candidate_content = json.dumps(turns, separators=(",", ":"))
    candidate_sha = hashlib.sha256(candidate_content.encode()).hexdigest()
    candidate_path = output_dir / f"candidate_document_{session_id}.json"
    audit = {
        "candidate_turn_count": 1,
        "candidate_message_count": 2,
        "user_count": 1,
        "assistant_count": 1,
        "first_role": "user",
        "repair_scope": {
            "status": "confirmed_missing_only_verified",
            "base_remote_sha256": "a" * 64,
            "old_message_count": 1,
            "inserted_message_count": 1,
            "selected_occurrence_ids": ["message_id:1"],
            "excluded_review_candidate_count": 8,
            "old_messages_preserved_as_ordered_subsequence": True,
        },
    }
    candidate_path.write_text(
        json.dumps(
            {
                "schema_version": "hindsight-conversation-document-v1",
                "session_id": session_id,
                "document_id": session_id,
                "turns": turns,
                "document_content": candidate_content,
                "document_content_sha256": candidate_sha,
                "audit": audit,
            }
        ),
        encoding="utf-8",
    )
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"session_id": session_id}), encoding="utf-8")
    events = [
        {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "event": "started",
            "recorded_at": started_at.isoformat(),
            "session_id": session_id,
            "document_id": session_id,
            "remote_expectation": "expected",
            "output_dir": str(output_dir),
            "repair_scope": "confirmed_missing_only",
            "base_remote_sha256": "a" * 64,
            "state_snapshot": {
                "session_found": True,
                "active_user_count": 5,
                "active_assistant_count": 5,
                "active_message_count": 10,
                "max_message_id": 10,
            },
        },
        {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "event": "export_succeeded",
            "recorded_at": (started_at + timedelta(seconds=1)).isoformat(),
            "session_id": session_id,
            "document_id": session_id,
            "manifest_path": str(manifest_path),
            "candidate_path": str(candidate_path),
            "candidate_sha256": candidate_sha,
            "candidate_turn_count": 1,
            "candidate_message_count": 2,
            "audit": audit,
            "repair_scope": "confirmed_missing_only",
        },
        {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "event": "remote_write_started",
            "recorded_at": (started_at + timedelta(seconds=2)).isoformat(),
            "session_id": session_id,
            "document_id": session_id,
            "operation_id": attempt_id,
            "bank_id": "Hermes",
            "update_mode": "replace",
            "candidate_sha256": candidate_sha,
        },
        {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "event": "remote_write_accepted",
            "recorded_at": (started_at + timedelta(seconds=3)).isoformat(),
            "session_id": session_id,
            "document_id": session_id,
            "operation_id": attempt_id,
            "bank_id": "Hermes",
            "candidate_sha256": candidate_sha,
        },
    ]
    journal.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=now,
        operation_fetcher=lambda _operation_id: {
            "status": "found",
            "operation_id": attempt_id,
            "operation": {
                "id": attempt_id,
                "task_type": "batch_retain",
                "status": "completed",
                "document_id": session_id,
                "items_count": 1,
                "unit_ids_count": 1,
                "extraction_errors_count": 0,
            },
        },
        document_fetcher=lambda _document_id: {
            "status": "found",
            "document_id": session_id,
            "document": {"id": session_id, "original_text": candidate_content},
        },
    )

    assert result["alerts"] == []


def test_exact_remote_copy_enriches_severe_candidate_alert(tmp_path: Path) -> None:
    module = load_module()
    now = datetime(2026, 9, 14, 5, 0, tzinfo=timezone.utc)
    started_at = now - timedelta(minutes=10)
    session_id = "20260914_125000_c1d2e3f4"
    attempt_id = "e6d22f41-64e4-4acc-a9a5-672d017945ae"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    output_dir = tmp_path / "runs" / session_id / attempt_id
    output_dir.mkdir(parents=True)
    create_state_db(state_db, session_id)
    with sqlite3.connect(state_db) as conn:
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, 1, 0, NULL)",
            [
                (
                    message_id,
                    session_id,
                    "user" if message_id % 2 else "assistant",
                    f"message {message_id}",
                    1000.0 + message_id,
                )
                for message_id in range(3, 11)
            ],
        )
    turns = [
        [
            {"role": "user", "content": "User: first request"},
            {"role": "assistant", "content": "Assistant: first answer"},
        ]
    ]
    candidate_content = json.dumps(turns, separators=(",", ":"))
    candidate_sha = hashlib.sha256(candidate_content.encode()).hexdigest()
    candidate_path = output_dir / f"candidate_document_{session_id}.json"
    candidate_path.write_text(
        json.dumps(
            {
                "schema_version": "hindsight-conversation-document-v1",
                "session_id": session_id,
                "document_id": session_id,
                "turns": turns,
                "document_content": candidate_content,
                "document_content_sha256": candidate_sha,
                "audit": {"candidate_turn_count": 1, "candidate_message_count": 2, "state_reconciliation": {"status": "verified", "source_event_count": 2, "matched_event_count": 2, "added_event_count": 0, "uncovered_event_count": 0}},
            }
        ),
        encoding="utf-8",
    )
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"session_id": session_id}), encoding="utf-8")
    journal.write_text(
        "".join(
            json.dumps(event) + "\n"
            for event in [
                {
                    "schema_version": 1,
                    "attempt_id": attempt_id,
                    "event": "started",
                    "recorded_at": started_at.isoformat(),
                    "session_id": session_id,
                    "document_id": session_id,
                    "remote_expectation": "expected",
                    "output_dir": str(output_dir),
                    "state_snapshot": {
                        "session_found": True,
                        "active_user_count": 5,
                        "active_assistant_count": 5,
                        "active_message_count": 10,
                        "max_message_id": 10,
                    },
                },
                {
                    "schema_version": 1,
                    "attempt_id": attempt_id,
                    "event": "export_succeeded",
                    "recorded_at": (started_at + timedelta(seconds=5)).isoformat(),
                    "session_id": session_id,
                    "document_id": session_id,
                    "manifest_path": str(manifest_path),
                    "candidate_path": str(candidate_path),
                    "candidate_sha256": candidate_sha,
                    "candidate_turn_count": 1,
                    "candidate_message_count": 2,
                },
                {
                    "schema_version": 1,
                    "attempt_id": attempt_id,
                    "event": "remote_write_started",
                    "recorded_at": (started_at + timedelta(seconds=6)).isoformat(),
                    "session_id": session_id,
                    "document_id": session_id,
                    "operation_id": attempt_id,
                    "bank_id": "Hermes",
                    "update_mode": "replace",
                    "candidate_sha256": candidate_sha,
                },
                {
                    "schema_version": 1,
                    "attempt_id": attempt_id,
                    "event": "remote_write_accepted",
                    "recorded_at": (started_at + timedelta(seconds=7)).isoformat(),
                    "session_id": session_id,
                    "document_id": session_id,
                    "operation_id": attempt_id,
                    "bank_id": "Hermes",
                    "candidate_sha256": candidate_sha,
                },
            ]
        ),
        encoding="utf-8",
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=now,
        operation_fetcher=lambda operation_id: {
            "status": "found",
            "operation_id": operation_id,
            "operation": {
                "id": operation_id,
                "task_type": "batch_retain",
                "status": "completed",
                "document_id": session_id,
                "items_count": 1,
                "extraction_errors_count": 0,
            },
        },
        document_fetcher=lambda document_id: {
            "status": "found",
            "document_id": document_id,
            "document": {"id": document_id, "original_text": candidate_content},
        },
    )

    assert result["remote_confirmed_count"] == 1
    assert result["alerts"] == [
        {
            "alert_key": f"retain:{attempt_id}:candidate_severely_incomplete",
            "type": "retain_candidate_severely_incomplete",
            "severity": "high",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "document_id": session_id,
            "started_at": started_at.isoformat(),
            "state_session_found": True,
            "state_active_message_count": 10,
            "candidate_message_count": 2,
            "missing_message_count": 8,
            "remote_write_status": "completed_exact_candidate",
            "operation_id": attempt_id,
            "remote_document_matches_candidate": True,
            "message": "Retain 候选相对开始时的 StateDB 会话少了一大块有效用户或 AI 消息",
        }
    ]


def test_remote_document_with_large_candidate_gap_is_high_alert(tmp_path: Path) -> None:
    module = load_module()
    now = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)
    started_at = now - timedelta(minutes=10)
    session_id = "20260825_173000_55667788"
    attempt_id = "attempt-severe-remote-gap"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    output_dir = tmp_path / "runs" / session_id / attempt_id
    output_dir.mkdir(parents=True)
    create_state_db(state_db, session_id)
    with sqlite3.connect(state_db) as conn:
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, 1, 0, NULL)",
            [
                (message_id, session_id, "user" if message_id % 2 else "assistant", f"message {message_id}", 1000.0 + message_id)
                for message_id in range(3, 11)
            ],
        )
    turns = [
        [
            {"role": "user", "content": f"User: request {index}"},
            {"role": "assistant", "content": f"Assistant: answer {index}"},
        ]
        for index in range(5)
    ]
    candidate_content = json.dumps(turns, ensure_ascii=False, separators=(",", ":"))
    candidate_sha = hashlib.sha256(candidate_content.encode()).hexdigest()
    candidate_path = output_dir / f"candidate_document_{session_id}.json"
    candidate_path.write_text(
        json.dumps(
            {
                "schema_version": "hindsight-conversation-document-v1",
                "session_id": session_id,
                "document_id": session_id,
                "turns": turns,
                "document_content": candidate_content,
                "document_content_sha256": candidate_sha,
                "audit": {"candidate_turn_count": 5, "candidate_message_count": 10},
            }
        ),
        encoding="utf-8",
    )
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"session_id": session_id}), encoding="utf-8")
    journal.write_text(
        "".join(
            json.dumps(event) + "\n"
            for event in [
                {
                    "schema_version": 1,
                    "attempt_id": attempt_id,
                    "event": "started",
                    "recorded_at": started_at.isoformat(),
                    "session_id": session_id,
                    "document_id": session_id,
                    "remote_expectation": "expected",
                    "output_dir": str(output_dir),
                    "state_snapshot": {
                        "session_found": True,
                        "active_user_count": 5,
                        "active_assistant_count": 5,
                        "active_message_count": 10,
                        "max_message_id": 10,
                    },
                },
                {
                    "schema_version": 1,
                    "attempt_id": attempt_id,
                    "event": "export_succeeded",
                    "recorded_at": (started_at + timedelta(seconds=5)).isoformat(),
                    "session_id": session_id,
                    "document_id": session_id,
                    "manifest_path": str(manifest_path),
                    "candidate_path": str(candidate_path),
                    "candidate_sha256": candidate_sha,
                    "candidate_turn_count": 5,
                    "candidate_message_count": 10,
                },
                {
                    "schema_version": 1,
                    "attempt_id": attempt_id,
                    "event": "remote_write_started",
                    "recorded_at": (started_at + timedelta(seconds=6)).isoformat(),
                    "session_id": session_id,
                    "document_id": session_id,
                    "operation_id": attempt_id,
                    "bank_id": "Hermes",
                    "update_mode": "replace",
                    "candidate_sha256": candidate_sha,
                },
            ]
        ),
        encoding="utf-8",
    )
    remote_content = json.dumps(turns[:1], ensure_ascii=False, separators=(",", ":"))

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=now,
        grace_seconds=300,
        operation_fetcher=lambda operation_id: {
            "status": "found",
            "operation_id": operation_id,
            "operation": {
                "id": operation_id,
                "task_type": "batch_retain",
                "status": "completed",
                "document_id": session_id,
                "items_count": 1,
                "extraction_errors_count": 0,
            },
        },
        document_fetcher=lambda document_id: {
            "status": "found",
            "document_id": document_id,
            "document": {"id": document_id, "original_text": remote_content},
        },
    )

    assert result["alerts"] == [
        {
            "alert_key": f"retain:{attempt_id}:remote_document_severely_incomplete",
            "type": "retain_remote_document_severely_incomplete",
            "severity": "high",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "document_id": session_id,
            "started_at": started_at.isoformat(),
            "state_session_found": True,
            "candidate_message_count": 10,
            "remote_message_count": 2,
            "missing_message_count": 8,
            "message": "Hindsight Document 存在，但相对本次 Retain 候选少了一大块内容",
        }
    ]


def test_explicit_export_failure_is_high_alert(tmp_path: Path) -> None:
    module = load_module()
    now = datetime(2026, 8, 25, 10, 30, tzinfo=timezone.utc)
    started_at = now - timedelta(minutes=10)
    session_id = "20260825_180000_99aabbcc"
    attempt_id = "attempt-export-failed"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    create_state_db(state_db, session_id)
    journal.write_text(
        "".join(
            json.dumps(event) + "\n"
            for event in [
                {
                    "schema_version": 1,
                    "attempt_id": attempt_id,
                    "event": "started",
                    "recorded_at": started_at.isoformat(),
                    "session_id": session_id,
                    "document_id": session_id,
                    "remote_expectation": "not_expected_export_only",
                    "output_dir": str(tmp_path / "runs" / session_id / attempt_id),
                },
                {
                    "schema_version": 1,
                    "attempt_id": attempt_id,
                    "event": "export_failed",
                    "recorded_at": (started_at + timedelta(seconds=3)).isoformat(),
                    "session_id": session_id,
                    "document_id": session_id,
                    "failure_type": "ExportFailure",
                },
            ]
        ),
        encoding="utf-8",
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=now,
        grace_seconds=300,
    )

    assert result["alerts"] == [
        {
            "alert_key": f"retain:{attempt_id}:export_failed",
            "type": "retain_export_failed",
            "severity": "high",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "document_id": session_id,
            "started_at": started_at.isoformat(),
            "state_session_found": True,
            "failure_type": "ExportFailure",
            "message": "Retain 本地候选生成明确失败",
        }
    ]


def test_success_receipt_with_missing_artifact_is_high_alert(tmp_path: Path) -> None:
    module = load_module()
    now = datetime(2026, 8, 25, 11, 0, tzinfo=timezone.utc)
    started_at = now - timedelta(minutes=10)
    session_id = "20260825_183000_ddeeff00"
    attempt_id = "attempt-artifact-missing"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    create_state_db(state_db, session_id)
    journal.write_text(
        "".join(
            json.dumps(event) + "\n"
            for event in [
                {
                    "schema_version": 1,
                    "attempt_id": attempt_id,
                    "event": "started",
                    "recorded_at": started_at.isoformat(),
                    "session_id": session_id,
                    "document_id": session_id,
                    "remote_expectation": "not_expected_export_only",
                    "output_dir": str(tmp_path / "runs" / session_id / attempt_id),
                    "state_snapshot": {
                        "session_found": True,
                        "active_user_count": 1,
                        "active_assistant_count": 1,
                        "active_message_count": 2,
                        "max_message_id": 2,
                    },
                },
                {
                    "schema_version": 1,
                    "attempt_id": attempt_id,
                    "event": "export_succeeded",
                    "recorded_at": (started_at + timedelta(seconds=3)).isoformat(),
                    "session_id": session_id,
                    "document_id": session_id,
                    "manifest_path": str(tmp_path / "missing-manifest.json"),
                    "candidate_path": str(tmp_path / "missing-candidate.json"),
                    "candidate_message_count": 2,
                },
            ]
        ),
        encoding="utf-8",
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=now,
        grace_seconds=300,
    )

    assert result["alerts"] == [
        {
            "alert_key": f"retain:{attempt_id}:artifacts_invalid",
            "type": "retain_export_artifacts_invalid",
            "severity": "high",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "document_id": session_id,
            "started_at": started_at.isoformat(),
            "state_session_found": True,
            "message": "Retain 记录为本地成功，但 manifest 或候选产物缺失、损坏或会话不匹配",
        }
    ]


def test_success_without_state_db_session_is_unresolved_alert(tmp_path: Path) -> None:
    module = load_module()
    now = datetime(2026, 8, 25, 11, 30, tzinfo=timezone.utc)
    started_at = now - timedelta(minutes=10)
    session_id = "20260825_190000_00112233"
    attempt_id = "attempt-state-missing"
    journal = tmp_path / "retain-attempts.jsonl"
    output_dir = tmp_path / "runs" / session_id / attempt_id
    output_dir.mkdir(parents=True)
    turns = [[
        {"role": "user", "content": "User: first request"},
        {"role": "assistant", "content": "Assistant: first answer"},
    ]]
    candidate_content = json.dumps(turns, separators=(",", ":"))
    candidate_sha = hashlib.sha256(candidate_content.encode()).hexdigest()
    candidate_path = output_dir / f"candidate_document_{session_id}.json"
    candidate_path.write_text(
        json.dumps(
            {
                "schema_version": "hindsight-conversation-document-v1",
                "session_id": session_id,
                "document_id": session_id,
                "turns": turns,
                "document_content": candidate_content,
                "document_content_sha256": candidate_sha,
                "audit": {"candidate_turn_count": 1, "candidate_message_count": 2, "state_reconciliation": {"status": "verified", "source_event_count": 2, "matched_event_count": 2, "added_event_count": 0, "uncovered_event_count": 0}},
            }
        ),
        encoding="utf-8",
    )
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"session_id": session_id}), encoding="utf-8")
    journal.write_text(
        "".join(
            json.dumps(event) + "\n"
            for event in [
                {
                    "schema_version": 1,
                    "attempt_id": attempt_id,
                    "event": "started",
                    "recorded_at": started_at.isoformat(),
                    "session_id": session_id,
                    "document_id": session_id,
                    "remote_expectation": "not_expected_export_only",
                    "output_dir": str(output_dir),
                    "state_snapshot": {
                        "session_found": False,
                        "active_user_count": 0,
                        "active_assistant_count": 0,
                        "active_message_count": 0,
                        "max_message_id": None,
                    },
                },
                {
                    "schema_version": 1,
                    "attempt_id": attempt_id,
                    "event": "export_succeeded",
                    "recorded_at": (started_at + timedelta(seconds=3)).isoformat(),
                    "session_id": session_id,
                    "document_id": session_id,
                    "manifest_path": str(manifest_path),
                    "candidate_path": str(candidate_path),
                    "candidate_sha256": candidate_sha,
                    "candidate_turn_count": 1,
                    "candidate_message_count": 2,
                },
            ]
        ),
        encoding="utf-8",
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=tmp_path / "missing-state.db",
        now=now,
        grace_seconds=300,
    )

    assert result["alerts"] == [
        {
            "alert_key": f"retain:{attempt_id}:state_session_missing",
            "type": "retain_state_session_missing",
            "severity": "medium",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "document_id": session_id,
            "started_at": started_at.isoformat(),
            "state_session_found": False,
            "message": "Retain 有本地记录，但 StateDB 中找不到对应会话，无法完成独立内容交叉验证",
        }
    ]


def test_retain_resolves_session_from_profile_state_db_family(tmp_path: Path) -> None:
    module = load_module()
    session_id = "session-profile-only"
    started_at = datetime(2026, 9, 10, 13, 23, tzinfo=timezone.utc)
    default_state_db = tmp_path / "state.db"
    profile_state_db = tmp_path / "profiles" / "evaluator" / "state.db"
    profile_state_db.parent.mkdir(parents=True)
    create_state_db(profile_state_db, session_id)
    journal = tmp_path / "retain-attempts.jsonl"
    exporter = tmp_path / "fake_exporter.py"
    write_valid_exporter(exporter)

    module.run_export(
        session_id=session_id,
        output_root=tmp_path / "runs",
        journal_path=journal,
        state_db_path=default_state_db,
        export_script=exporter,
        python_executable=sys.executable,
        remote_expectation="not_expected_export_only",
        attempt_id="attempt-profile-state",
        now=started_at,
        cutoff_at=started_at,
    )

    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert events[0]["state_snapshot"] == {
        "session_found": True,
        "active_user_count": 1,
        "active_assistant_count": 1,
        "active_message_count": 2,
        "max_message_id": 2,
    }
    candidate_path = Path(events[1]["candidate_path"])
    manifest = json.loads((candidate_path.parent / "manifest.json").read_text())
    assert manifest["received_state_db_path"] == str(profile_state_db)

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=default_state_db,
        now=started_at + timedelta(minutes=10),
    )

    assert result["alerts"] == []


def test_scan_rechecks_profile_state_when_old_snapshot_used_wrong_db(
    tmp_path: Path,
) -> None:
    module = load_module()
    session_id = "session-profile-old-receipt"
    attempt_id = "attempt-profile-old-receipt"
    started_at = datetime(2026, 9, 10, 13, 23, tzinfo=timezone.utc)
    default_state_db = tmp_path / "state.db"
    profile_state_db = tmp_path / "profiles" / "evaluator" / "state.db"
    profile_state_db.parent.mkdir(parents=True)
    create_state_db(profile_state_db, session_id)
    with sqlite3.connect(profile_state_db) as conn:
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, 1, 0, NULL)",
            [
                (message_id, session_id, role, f"message {message_id}", 1000.0 + message_id)
                for message_id, role in [
                    (3, "user"),
                    (4, "assistant"),
                    (5, "user"),
                    (6, "assistant"),
                    (7, "user"),
                    (8, "assistant"),
                ]
            ],
        )
    journal = tmp_path / "retain-attempts.jsonl"
    exporter = tmp_path / "fake_exporter.py"
    write_valid_exporter(exporter)
    module.run_export(
        session_id=session_id,
        output_root=tmp_path / "runs",
        journal_path=journal,
        state_db_path=default_state_db,
        export_script=exporter,
        python_executable=sys.executable,
        remote_expectation="not_expected_export_only",
        attempt_id=attempt_id,
        now=started_at,
        cutoff_at=started_at,
    )
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    events[0]["state_snapshot"] = {
        "session_found": False,
        "active_user_count": 0,
        "active_assistant_count": 0,
        "active_message_count": 0,
        "max_message_id": None,
    }
    journal.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=default_state_db,
        now=started_at + timedelta(minutes=10),
    )

    assert result["alerts"] == [
        {
            "alert_key": f"retain:{attempt_id}:candidate_severely_incomplete",
            "type": "retain_candidate_severely_incomplete",
            "severity": "high",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "document_id": session_id,
            "started_at": started_at.isoformat(),
            "state_session_found": True,
            "state_active_message_count": 8,
            "candidate_message_count": 2,
            "missing_message_count": 6,
            "message": "Retain 候选相对开始时的 StateDB 会话少了一大块有效用户或 AI 消息",
        }
    ]


def test_remote_unavailable_is_unresolved_not_missing(tmp_path: Path) -> None:
    module = load_module()
    now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    started_at = now - timedelta(minutes=10)
    session_id = "20260825_193000_44556677"
    attempt_id = "attempt-remote-unavailable"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    output_dir = tmp_path / "runs" / session_id / attempt_id
    output_dir.mkdir(parents=True)
    create_state_db(state_db, session_id)
    turns = [[
        {"role": "user", "content": "User: first request"},
        {"role": "assistant", "content": "Assistant: first answer"},
    ]]
    candidate_content = json.dumps(turns, separators=(",", ":"))
    candidate_sha = hashlib.sha256(candidate_content.encode()).hexdigest()
    candidate_path = output_dir / f"candidate_document_{session_id}.json"
    candidate_path.write_text(
        json.dumps(
            {
                "schema_version": "hindsight-conversation-document-v1",
                "session_id": session_id,
                "document_id": session_id,
                "turns": turns,
                "document_content": candidate_content,
                "document_content_sha256": candidate_sha,
                "audit": {"candidate_turn_count": 1, "candidate_message_count": 2, "state_reconciliation": {"status": "verified", "source_event_count": 2, "matched_event_count": 2, "added_event_count": 0, "uncovered_event_count": 0}},
            }
        ),
        encoding="utf-8",
    )
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"session_id": session_id}), encoding="utf-8")
    journal.write_text(
        "".join(
            json.dumps(event) + "\n"
            for event in [
                {
                    "schema_version": 1,
                    "attempt_id": attempt_id,
                    "event": "started",
                    "recorded_at": started_at.isoformat(),
                    "session_id": session_id,
                    "document_id": session_id,
                    "remote_expectation": "expected",
                    "output_dir": str(output_dir),
                    "state_snapshot": {
                        "session_found": True,
                        "active_user_count": 1,
                        "active_assistant_count": 1,
                        "active_message_count": 2,
                        "max_message_id": 2,
                    },
                },
                {
                    "schema_version": 1,
                    "attempt_id": attempt_id,
                    "event": "export_succeeded",
                    "recorded_at": (started_at + timedelta(seconds=3)).isoformat(),
                    "session_id": session_id,
                    "document_id": session_id,
                    "manifest_path": str(manifest_path),
                    "candidate_path": str(candidate_path),
                    "candidate_sha256": candidate_sha,
                    "candidate_turn_count": 1,
                    "candidate_message_count": 2,
                },
                {
                    "schema_version": 1,
                    "attempt_id": attempt_id,
                    "event": "remote_write_started",
                    "recorded_at": (started_at + timedelta(seconds=4)).isoformat(),
                    "session_id": session_id,
                    "document_id": session_id,
                    "operation_id": attempt_id,
                    "bank_id": "Hermes",
                    "update_mode": "replace",
                    "candidate_sha256": candidate_sha,
                },
            ]
        ),
        encoding="utf-8",
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=now,
        grace_seconds=300,
        operation_fetcher=lambda operation_id: {
            "status": "found",
            "operation_id": operation_id,
            "operation": {
                "id": operation_id,
                "task_type": "batch_retain",
                "status": "completed",
                "document_id": session_id,
                "items_count": 1,
                "extraction_errors_count": 0,
            },
        },
        document_fetcher=lambda document_id: {"status": "unavailable", "document_id": document_id},
    )

    assert result["alerts"] == [
        {
            "alert_key": f"retain:{attempt_id}:remote_check_unavailable",
            "type": "retain_remote_check_unavailable",
            "severity": "medium",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "document_id": session_id,
            "started_at": started_at.isoformat(),
            "state_session_found": True,
            "message": "Hindsight 暂时不可达，当前无法确认本次 Retain 是否已保存",
        }
    ]


def test_scan_cli_outputs_structured_json_for_empty_journal(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "scan",
            "--journal",
            str(tmp_path / "retain-attempts.jsonl"),
            "--state-db",
            str(tmp_path / "state.db"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout) == {
        "status": "ok",
        "journal_torn_tail": False,
        "attempt_count": 0,
        "remote_confirmed_count": 0,
        "remote_confirmed_attempts": [],
        "remote_superseded_count": 0,
        "remote_superseded_attempts": [],
        "alerts": [],
    }
    assert completed.stderr == ""


def test_export_cli_wraps_generator_and_returns_attempt_identity(tmp_path: Path) -> None:
    session_id = "20260825_200000_8899aabb"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    output_root = tmp_path / "runs"
    create_state_db(state_db, session_id)
    exporter = tmp_path / "fake_exporter.py"
    exporter.write_text(
        """
import argparse
import hashlib
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--session-id", required=True)
parser.add_argument("--skip-hindsight", action="store_true")
parser.add_argument("--cutoff-at")
parser.add_argument("--state-db-path")
parser.add_argument("--output-dir", type=Path, required=True)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
turns = [[
    {"role": "user", "content": "User: first request"},
    {"role": "assistant", "content": "Assistant: first answer"},
]]
content = json.dumps(turns, separators=(",", ":"))
candidate = args.output_dir / f"candidate_document_{args.session_id}.json"
candidate.write_text(json.dumps({
    "schema_version": "hindsight-conversation-document-v1",
    "session_id": args.session_id,
    "document_id": args.session_id,
    "turns": turns,
    "document_content": content,
    "document_content_sha256": hashlib.sha256(content.encode()).hexdigest(),
    "audit": {"candidate_turn_count": 1, "candidate_message_count": 2, "state_reconciliation": {"status": "verified", "source_event_count": 2, "matched_event_count": 2, "added_event_count": 0, "uncovered_event_count": 0}},
}), encoding="utf-8")
manifest = {"session_id": args.session_id, "read_only": True, "files": [str(candidate)]}
(args.output_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
print(json.dumps(manifest))
""".lstrip(),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "export",
            "--session-id",
            session_id,
            "--output-root",
            str(output_root),
            "--journal",
            str(journal),
            "--state-db",
            str(state_db),
            "--export-script",
            str(exporter),
            "--remote-expectation",
            "not_expected_export_only",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["status"] == "ok"
    assert payload["session_id"] == session_id
    assert payload["remote_expectation"] == "not_expected_export_only"
    assert Path(payload["output_dir"]).parent == output_root / session_id
    assert payload["attempt_id"] == Path(payload["output_dir"]).name
    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["started", "export_succeeded"]
    assert completed.stderr == ""


def test_directory_setup_failure_still_writes_terminal_failure(tmp_path: Path) -> None:
    module = load_module()
    session_id = "20260825_203000_ccddeeff"
    attempt_id = "attempt-directory-collision"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    output_root = tmp_path / "runs"
    create_state_db(state_db, session_id)
    (output_root / session_id / attempt_id).mkdir(parents=True)

    try:
        module.run_export(
            session_id=session_id,
            output_root=output_root,
            journal_path=journal,
            state_db_path=state_db,
            export_script=tmp_path / "unused.py",
            python_executable=sys.executable,
            attempt_id=attempt_id,
        )
    except module.ExportFailure:
        pass
    else:
        raise AssertionError("directory collision must fail the export")

    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["started", "export_failed"]
    assert events[1]["failure_type"] == "FileExistsError"
    assert events[1]["failure_stage"] == "prepare_output"


def test_export_process_failure_records_safe_diagnostics(tmp_path: Path) -> None:
    module = load_module()
    session_id = "20260825_204500_aabbccdd"
    attempt_id = "4bdb5e72-7d07-4f06-a6f6-1ecb0e5f4b77"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    output_root = tmp_path / "runs"
    exporter = tmp_path / "failing_exporter.py"
    create_state_db(state_db, session_id)
    exporter.write_text(
        """
import sys
print("trace page=1")
print("HERMES_LANGFUSE_SECRET_KEY=test-secret", file=sys.stderr)
print("Authorization: Bearer bearer-secret", file=sys.stderr)
print("x" * 9000, file=sys.stderr)
raise SystemExit(7)
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(module.ExportFailure):
        module.run_export(
            session_id=session_id,
            output_root=output_root,
            journal_path=journal,
            state_db_path=state_db,
            export_script=exporter,
            python_executable=sys.executable,
            attempt_id=attempt_id,
            remote_expectation="expected",
        )

    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    failure = events[-1]
    assert failure["event"] == "export_failed"
    assert failure["failure_stage"] == "export_process"
    assert failure["failure_type"] == "ExportFailure"
    assert failure["failure_message"] == "export process failed"
    assert failure["exporter_returncode"] == 7
    assert failure["exporter_stdout"] == "trace page=1\n"
    assert "test-secret" not in failure["exporter_stderr"]
    assert "bearer-secret" not in failure["exporter_stderr"]
    assert "<redacted>" in failure["exporter_stderr"]
    assert "[truncated]" in failure["exporter_stderr"]
    assert len(failure["exporter_stderr"]) <= module.MAX_FAILURE_LOG_CHARS


def test_failure_log_truncation_reports_original_length() -> None:
    module = load_module()
    source = "H" * 5000 + "T" * 5000

    rendered = module._sanitize_failure_text(source)

    assert len(rendered) == module.MAX_FAILURE_LOG_CHARS
    assert rendered.startswith("H")
    assert rendered.endswith("T")
    assert "...[truncated]..." in rendered
    assert f"original_chars={len(source)}" in rendered


@pytest.mark.parametrize("length", [4000, 4001])
def test_failure_log_truncation_limit_boundary(length: int) -> None:
    module = load_module()
    rendered = module._sanitize_failure_text("x" * length)

    assert len(rendered) == min(length, module.MAX_FAILURE_LOG_CHARS)
    if length == module.MAX_FAILURE_LOG_CHARS:
        assert "...[truncated]..." not in rendered
    else:
        assert "...[truncated]..." in rendered
        assert "original_chars=4001" in rendered


def test_export_process_spawn_failure_records_stage_without_result_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_module()
    session_id = "20260825_205000_eeff0011"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    exporter = tmp_path / "unused_exporter.py"
    create_state_db(state_db, session_id)

    def fail_spawn(*args, **kwargs):
        raise OSError("could not start exporter")

    monkeypatch.setattr(module.subprocess, "run", fail_spawn)

    with pytest.raises(module.ExportFailure):
        module.run_export(
            session_id=session_id,
            output_root=tmp_path / "runs",
            journal_path=journal,
            state_db_path=state_db,
            export_script=exporter,
            python_executable=sys.executable,
            attempt_id="spawn-failure",
        )

    failure = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
    assert failure["failure_stage"] == "export_process"
    assert failure["failure_type"] == "OSError"
    assert failure["failure_message"] == "could not start exporter"
    assert "exporter_returncode" not in failure
    assert "exporter_stdout" not in failure
    assert "exporter_stderr" not in failure


@pytest.mark.parametrize(
    ("stage", "hook_name"),
    [
        ("read_export_artifacts", "_read_export_artifacts"),
        ("validate_candidate", "_validate_candidate"),
        ("harden_artifacts", "_harden_and_sync_artifacts"),
    ],
)
def test_post_export_failure_records_stage_and_process_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    hook_name: str,
) -> None:
    module = load_module()
    session_id = "20260825_205500_11223344"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    exporter = tmp_path / "valid_exporter.py"
    create_state_db(state_db, session_id)
    write_valid_exporter(exporter)

    def fail_hook(*args, **kwargs):
        raise ValueError(f"forced {stage}")

    monkeypatch.setattr(module, hook_name, fail_hook)

    with pytest.raises(module.ExportFailure):
        module.run_export(
            session_id=session_id,
            output_root=tmp_path / "runs",
            journal_path=journal,
            state_db_path=state_db,
            export_script=exporter,
            python_executable=sys.executable,
            attempt_id="post-export-failure",
        )

    failure = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
    assert failure["failure_stage"] == stage
    assert failure["failure_type"] == "ValueError"
    assert failure["failure_message"] == f"forced {stage}"
    assert failure["exporter_returncode"] == 0
    assert session_id in failure["exporter_stdout"]
    assert failure["exporter_stderr"] == ""


def test_checker_does_not_misclassify_telegram_read_error_as_subagent_failure() -> None:
    checker = load_path_module("hindsight_checker_log_classifier_test", CHECKER_PATH)
    telegram_line = (
        "2026-09-13 23:30:12,705 WARNING "
        "hermes_plugins.telegram_platform.adapter: [Telegram] Telegram polling "
        "reconnect failed: httpx.ReadError:"
    )
    model_line = (
        "2026-09-14 09:59:50,026 WARNING agent.conversation_loop: "
        "API call failed (attempt 1/4) error_type=ReadError "
        "thread=bg-review:1 summary=[Errno 32] Broken pipe"
    )

    assert checker.classify(telegram_line) is None
    assert checker.classify(model_line) == ("subagent_api_broken_pipe", "medium")


def test_existing_checker_collects_retain_attempt_scan_json() -> None:
    checker = load_path_module("hindsight_checker_for_test", CHECKER_PATH)
    alert = {
        "alert_key": "retain:attempt-1:remote_document_missing",
        "type": "retain_expected_remote_document_missing",
        "severity": "high",
        "attempt_id": "attempt-1",
        "session_id": "20260825_210000_deadbeef",
    }

    class Result:
        returncode = 0
        stdout = json.dumps({"status": "ok", "attempt_count": 1, "alerts": [alert]})
        stderr = ""

    audit = checker.collect_retain_attempt_audit(
        runner=lambda command, **kwargs: Result()
    )

    assert audit == {"status": "ok", "attempt_count": 1, "alerts": [alert]}


def test_html_monitor_prompt_distinguishes_blocked_and_overwritten_candidates() -> None:
    scripts_dir = str(Path.home() / ".hermes" / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    monitor = load_path_module("hindsight_html_monitor_prompt_test", HTML_MONITOR_PATH)

    prompt = monitor.build_review_prompt({}, [])

    assert "remote_write_status=blocked_before_submit" in prompt
    assert "remote_write_status=completed_exact_candidate" in prompt
    assert "远端文档已经保存了这份严重不完整的候选" in prompt
    assert "机械差值，不等于已逐条确认的漏记条数" in prompt


def test_html_monitor_treats_retain_attempts_as_direct_alerts() -> None:
    scripts_dir = str(Path.home() / ".hermes" / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    monitor = load_path_module("hindsight_html_monitor_for_test", HTML_MONITOR_PATH)
    alert = {
        "alert_key": "retain:attempt-1:remote_document_missing",
        "type": "retain_expected_remote_document_missing",
        "severity": "high",
        "attempt_id": "attempt-1",
        "session_id": "20260825_210000_deadbeef",
    }
    context = {"retain_attempt_alerts": [alert]}

    assert monitor._direct_alerts_by_key(context) == {alert["alert_key"]: alert}
    assert monitor.has_direct_alert(context) is True


def test_export_only_success_never_queries_hindsight(tmp_path: Path) -> None:
    module = load_module()
    now = datetime(2026, 8, 25, 13, 0, tzinfo=timezone.utc)
    session_id = "20260825_210000_abcdef12"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    output_root = tmp_path / "runs"
    create_state_db(state_db, session_id)
    exporter = tmp_path / "fake_exporter.py"
    exporter.write_text(
        """
import argparse
import hashlib
import json
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("--session-id", required=True)
parser.add_argument("--skip-hindsight", action="store_true")
parser.add_argument("--cutoff-at")
parser.add_argument("--state-db-path")
parser.add_argument("--output-dir", type=Path, required=True)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
turns = [[{"role": "user", "content": "User: first request"}, {"role": "assistant", "content": "Assistant: first answer"}]]
content = json.dumps(turns, separators=(",", ":"))
candidate = args.output_dir / f"candidate_document_{args.session_id}.json"
candidate.write_text(json.dumps({"schema_version": "hindsight-conversation-document-v1", "session_id": args.session_id, "document_id": args.session_id, "turns": turns, "document_content": content, "document_content_sha256": hashlib.sha256(content.encode()).hexdigest(), "audit": {"candidate_turn_count": 1, "candidate_message_count": 2, "state_reconciliation": {"status": "verified", "source_event_count": 2, "matched_event_count": 2, "added_event_count": 0, "uncovered_event_count": 0}}}))
(args.output_dir / "manifest.json").write_text(json.dumps({"session_id": args.session_id, "read_only": True, "received_cutoff_at": args.cutoff_at, "files": [str(candidate)]}))
print(json.dumps({"session_id": args.session_id}))
""".lstrip(),
        encoding="utf-8",
    )
    module.run_export(
        session_id=session_id,
        output_root=output_root,
        journal_path=journal,
        state_db_path=state_db,
        export_script=exporter,
        python_executable=sys.executable,
        attempt_id="attempt-export-only",
        now=now - timedelta(minutes=10),
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=now,
        document_fetcher=lambda document_id: (_ for _ in ()).throw(
            AssertionError(f"unexpected remote query for {document_id}")
        ),
    )

    assert result["alerts"] == []


def test_exact_remote_candidate_is_confirmed_without_alert(tmp_path: Path) -> None:
    module = load_module()
    now = datetime(2026, 8, 25, 13, 30, tzinfo=timezone.utc)
    session_id = "20260825_213000_1234abcd"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    output_root = tmp_path / "runs"
    create_state_db(state_db, session_id)
    exporter = tmp_path / "fake_exporter.py"
    exporter.write_text(
        """
import argparse
import hashlib
import json
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("--session-id", required=True)
parser.add_argument("--skip-hindsight", action="store_true")
parser.add_argument("--cutoff-at")
parser.add_argument("--state-db-path")
parser.add_argument("--output-dir", type=Path, required=True)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
turns = [[{"role": "user", "content": "User: first request"}, {"role": "assistant", "content": "Assistant: first answer"}]]
content = json.dumps(turns, separators=(",", ":"))
digest = hashlib.sha256(content.encode()).hexdigest()
candidate = args.output_dir / f"candidate_document_{args.session_id}.json"
candidate.write_text(json.dumps({"schema_version": "hindsight-conversation-document-v1", "session_id": args.session_id, "document_id": args.session_id, "turns": turns, "document_content": content, "document_content_sha256": digest, "audit": {"candidate_turn_count": 1, "candidate_message_count": 2, "state_reconciliation": {"status": "verified", "source_event_count": 2, "matched_event_count": 2, "added_event_count": 0, "uncovered_event_count": 0}}}))
(args.output_dir / "manifest.json").write_text(json.dumps({"session_id": args.session_id, "read_only": True, "received_cutoff_at": args.cutoff_at, "files": [str(candidate)]}))
print(json.dumps({"session_id": args.session_id}))
""".lstrip(),
        encoding="utf-8",
    )
    exported = module.run_export(
        session_id=session_id,
        output_root=output_root,
        journal_path=journal,
        state_db_path=state_db,
        export_script=exporter,
        python_executable=sys.executable,
        remote_expectation="expected",
        attempt_id="4c3ef8f4-8e1d-4507-9a43-f471655344be",
        now=now - timedelta(minutes=10),
        remote_writer=lambda payload: {
            "success": True,
            "bank_id": "Hermes",
            "items_count": 1,
            "async": True,
            "operation_id": payload["operation_id"],
        },
    )
    candidate = json.loads(
        (Path(exported["output_dir"]) / f"candidate_document_{session_id}.json").read_text()
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=now,
        operation_fetcher=lambda operation_id: {
            "status": "found",
            "operation_id": operation_id,
            "operation": {
                "id": operation_id,
                "status": "completed",
                "task_type": "batch_retain",
                "document_id": session_id,
                "extraction_errors_count": 0,
            },
        },
        document_fetcher=lambda document_id: {
            "status": "found",
            "document_id": document_id,
            "document": {
                "id": document_id,
                "original_text": candidate["document_content"],
            },
        },
    )

    assert result["alerts"] == []
    assert result["remote_confirmed_count"] == 1
    assert result["remote_confirmed_attempts"] == [
        {
            "attempt_id": "4c3ef8f4-8e1d-4507-9a43-f471655344be",
            "session_id": session_id,
            "document_id": session_id,
            "operation_id": "4c3ef8f4-8e1d-4507-9a43-f471655344be",
            "candidate_sha256": candidate["document_content_sha256"],
        }
    ]


def test_completed_operation_requires_explicit_integer_zero_extraction_errors(
    tmp_path: Path,
) -> None:
    module = load_module()
    started_at = datetime(2026, 8, 26, 2, 20, tzinfo=timezone.utc)
    session_id = "20260826_022000_44556677"
    attempt_id = "641855cd-e552-4b5d-ad2a-94f907444417"
    state_db, journal, _result = create_accepted_attempt(
        module,
        tmp_path,
        session_id=session_id,
        attempt_id=attempt_id,
        started_at=started_at,
    )

    for include_field, value in ((False, None), (True, None), (True, "0")):
        operation = {
            "id": attempt_id,
            "task_type": "batch_retain",
            "status": "completed",
            "document_id": session_id,
            "items_count": 1,
        }
        if include_field:
            operation["extraction_errors_count"] = value
        scan = module.scan_attempts(
            journal_path=journal,
            state_db_path=state_db,
            now=started_at + timedelta(minutes=10),
            operation_fetcher=lambda operation_id, operation=operation: {
                "status": "found",
                "operation_id": operation_id,
                "operation": operation,
            },
            document_fetcher=lambda document_id: (_ for _ in ()).throw(
                AssertionError(f"invalid operation metadata must block {document_id}")
            ),
        )
        assert scan["remote_confirmed_count"] == 0
        assert [alert["type"] for alert in scan["alerts"]] == [
            "retain_remote_operation_metadata_unavailable"
        ]


def test_missing_operation_is_unresolved_without_document_lookup(tmp_path: Path) -> None:
    module = load_module()
    started_at = datetime(2026, 8, 26, 2, 21, tzinfo=timezone.utc)
    session_id = "20260826_022100_55667788"
    attempt_id = "2d111e76-aece-470f-a652-e5a8266ad610"
    state_db, journal, _result = create_accepted_attempt(
        module,
        tmp_path,
        session_id=session_id,
        attempt_id=attempt_id,
        started_at=started_at,
    )
    scan = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=started_at + timedelta(minutes=10),
        operation_fetcher=lambda operation_id: {
            "status": "missing",
            "operation_id": operation_id,
        },
        document_fetcher=lambda document_id: (_ for _ in ()).throw(
            AssertionError(f"missing operation must block {document_id}")
        ),
    )

    assert scan["remote_confirmed_count"] == 0
    assert [alert["type"] for alert in scan["alerts"]] == [
        "retain_remote_operation_missing"
    ]


def test_document_identity_mismatch_is_high_alert(tmp_path: Path) -> None:
    module = load_module()
    started_at = datetime(2026, 8, 26, 2, 22, tzinfo=timezone.utc)
    session_id = "20260826_022200_66778899"
    attempt_id = "4fc61de3-10b7-4bd8-b5f0-a0e51ae8b803"
    state_db, journal, result = create_accepted_attempt(
        module,
        tmp_path,
        session_id=session_id,
        attempt_id=attempt_id,
        started_at=started_at,
    )
    candidate = json.loads(
        (Path(result["output_dir"]) / f"candidate_document_{session_id}.json").read_text()
    )
    scan = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=started_at + timedelta(minutes=10),
        operation_fetcher=lambda operation_id: {
            "status": "found",
            "operation_id": operation_id,
            "operation": {
                "id": operation_id,
                "task_type": "batch_retain",
                "status": "completed",
                "document_id": session_id,
                "items_count": 1,
                "extraction_errors_count": 0,
            },
        },
        document_fetcher=lambda document_id: {
            "status": "found",
            "document_id": document_id,
            "document": {
                "id": "different-document",
                "original_text": candidate["document_content"],
            },
        },
    )

    assert scan["remote_confirmed_count"] == 0
    assert [alert["type"] for alert in scan["alerts"]] == [
        "retain_remote_document_identity_mismatch"
    ]


def test_later_replace_supersedes_older_document_comparison(tmp_path: Path) -> None:
    module = load_module()
    first_at = datetime(2026, 8, 26, 2, 23, tzinfo=timezone.utc)
    second_at = first_at + timedelta(minutes=5)
    session_id = "20260826_022300_778899aa"
    first_id = "6e46b5bd-d967-48af-9488-821dbb519bb7"
    second_id = "bbf341de-9bd7-41c2-8d34-a700e1b6e19a"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    output_root = tmp_path / "runs"
    exporter = tmp_path / "fake_exporter.py"
    create_state_db(state_db, session_id)
    write_valid_exporter(exporter)
    first = module.run_export(
        session_id=session_id,
        output_root=output_root,
        journal_path=journal,
        state_db_path=state_db,
        export_script=exporter,
        python_executable=sys.executable,
        remote_expectation="expected",
        attempt_id=first_id,
        now=first_at,
        remote_writer=lambda payload: {
            "success": True,
            "bank_id": "Hermes",
            "items_count": 1,
            "async": True,
            "operation_id": payload["operation_id"],
        },
    )
    exporter.write_text(
        exporter.read_text()
        .replace("User: persist this", "User: second version")
        .replace("Assistant: persisted", "Assistant: second saved")
    )
    second = module.run_export(
        session_id=session_id,
        output_root=output_root,
        journal_path=journal,
        state_db_path=state_db,
        export_script=exporter,
        python_executable=sys.executable,
        remote_expectation="expected",
        attempt_id=second_id,
        now=second_at,
        remote_writer=lambda payload: {
            "success": True,
            "bank_id": "Hermes",
            "items_count": 1,
            "async": True,
            "operation_id": payload["operation_id"],
        },
    )
    latest = json.loads(
        (
            Path(second["output_dir"])
            / f"candidate_document_{session_id}.json"
        ).read_text()
    )

    scan = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=second_at + timedelta(minutes=10),
        operation_fetcher=lambda operation_id: {
            "status": "found",
            "operation_id": operation_id,
            "operation": {
                "id": operation_id,
                "task_type": "batch_retain",
                "status": "completed",
                "document_id": session_id,
                "items_count": 1,
                "extraction_errors_count": 0,
            },
        },
        document_fetcher=lambda document_id: {
            "status": "found",
            "document_id": document_id,
            "document": {"id": document_id, "original_text": latest["document_content"]},
        },
    )

    assert scan["alerts"] == []
    assert scan["remote_confirmed_count"] == 1
    assert scan["remote_confirmed_attempts"][0]["attempt_id"] == second_id
    assert scan["remote_superseded_count"] == 1
    assert scan["remote_superseded_attempts"] == [
        {
            "attempt_id": first_id,
            "session_id": session_id,
            "document_id": session_id,
            "operation_id": first_id,
            "superseded_by_attempt_id": second_id,
        }
    ]


def test_tool_call_rows_do_not_create_false_severe_gap(tmp_path: Path) -> None:
    module = load_module()
    now = datetime(2026, 8, 25, 14, 0, tzinfo=timezone.utc)
    session_id = "20260825_220000_faceb00c"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    output_root = tmp_path / "runs"
    with sqlite3.connect(state_db) as conn:
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, session_key TEXT, profile_name TEXT, started_at REAL)"
        )
        conn.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT,
                role TEXT,
                content TEXT,
                timestamp REAL,
                active INTEGER,
                compacted INTEGER,
                display_kind TEXT,
                tool_calls TEXT,
                finish_reason TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO sessions VALUES (?, 'gateway', 'agent:main:telegram:dm:1', 'default', 1000)",
            (session_id,),
        )
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, 1, 0, NULL, ?, ?)",
            [
                (1, session_id, "user", "first request", 1001.0, None, None),
                (2, session_id, "assistant", "first answer", 1002.0, None, "stop"),
                *[
                    (message_id, session_id, "assistant", "", 1000.0 + message_id, "[{\"type\":\"function\"}]", "tool_calls")
                    for message_id in range(3, 11)
                ],
            ],
        )
    exporter = tmp_path / "fake_exporter.py"
    exporter.write_text(
        """
import argparse
import hashlib
import json
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("--session-id", required=True)
parser.add_argument("--skip-hindsight", action="store_true")
parser.add_argument("--cutoff-at")
parser.add_argument("--state-db-path")
parser.add_argument("--output-dir", type=Path, required=True)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
turns = [[{"role": "user", "content": "User: first request"}, {"role": "assistant", "content": "Assistant: first answer"}]]
content = json.dumps(turns, separators=(",", ":"))
candidate = args.output_dir / f"candidate_document_{args.session_id}.json"
candidate.write_text(json.dumps({"schema_version": "hindsight-conversation-document-v1", "session_id": args.session_id, "document_id": args.session_id, "turns": turns, "document_content": content, "document_content_sha256": hashlib.sha256(content.encode()).hexdigest(), "audit": {"candidate_turn_count": 1, "candidate_message_count": 2, "state_reconciliation": {"status": "verified", "source_event_count": 2, "matched_event_count": 2, "added_event_count": 0, "uncovered_event_count": 0}}}))
(args.output_dir / "manifest.json").write_text(json.dumps({"session_id": args.session_id, "read_only": True, "received_cutoff_at": args.cutoff_at, "files": [str(candidate)]}))
print(json.dumps({"session_id": args.session_id}))
""".lstrip(),
        encoding="utf-8",
    )
    module.run_export(
        session_id=session_id,
        output_root=output_root,
        journal_path=journal,
        state_db_path=state_db,
        export_script=exporter,
        python_executable=sys.executable,
        attempt_id="attempt-tool-calls",
        now=now - timedelta(minutes=10),
    )

    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert events[0]["state_snapshot"]["active_message_count"] == 2
    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=now,
    )
    assert result["alerts"] == []


def test_document_audit_excludes_attempt_managed_documents() -> None:
    checker = load_path_module("hindsight_checker_attempt_scope_test", CHECKER_PATH)
    now = datetime(2026, 8, 26, 2, 45, tzinfo=timezone.utc)
    document_id = "20260825_140702_01e0605b"
    checker.verify_manual_retain_scope = lambda: None
    checker.local_document_scope_index = lambda: ({}, {})
    checker.retain_attempt_managed_document_ids = lambda: {document_id}
    checker.document_audit_since = lambda now, coverage_start=None: now - timedelta(days=1)
    checker.remote_document_items = lambda: [{"id": document_id}]
    checker.document_time = lambda item: now
    checker.audit_scopes = lambda: []
    setattr(
        checker,
        "audit_attempt_managed_document",
        lambda _document_id: {
            "document_id": document_id,
            "saved_at": now.isoformat(),
            "profile_name": "default",
            "submission_binding_status": "attempt_exact",
            "submission_id": "attempt-1",
            "linked_sessions": [document_id],
            "source_entries": [],
            "local_retain_entries": [],
            "document_entries": [],
            "source_rows_after_document_window": 0,
            "source_provenance": {"mode": "langfuse_root"},
            "stage": {
                "source_matches_local_retain": True,
                "local_retain_matches_document": True,
                "failure_stage": None,
            },
            "candidates": [],
        },
    )
    checker.audit_one_document = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("attempt-managed document must not enter legacy ledger audit")
    )

    result = checker.audit_recent_manual_retain_documents(now)

    assert result["attempt_managed_remote_document_count"] == 1
    assert result["attempt_audited_document_count"] == 1
    assert result["eligible_document_count"] == 0
    assert result["unmapped_remote_document_count"] == 0
    assert result["candidate_document_count"] == 0


def test_checker_bank_is_loaded_from_config_and_ignores_environment(
    tmp_path: Path,
) -> None:
    checker = load_path_module("hindsight_checker_configured_bank_test", CHECKER_PATH)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"bank_id": "ConfiguredBank"}), encoding="utf-8")
    previous = os.environ.get("HERMES_HINDSIGHT_BANK_ID")
    os.environ["HERMES_HINDSIGHT_BANK_ID"] = "wrong-bank"
    try:
        bank_id = checker.load_hindsight_bank_id(config_path)
    finally:
        if previous is None:
            os.environ.pop("HERMES_HINDSIGHT_BANK_ID", None)
        else:
            os.environ["HERMES_HINDSIGHT_BANK_ID"] = previous
    assert bank_id == "ConfiguredBank"


def test_checker_sqlite_helper_is_uri_read_only_and_query_only(tmp_path: Path) -> None:
    checker = load_path_module("hindsight_checker_read_only_sqlite_test", CHECKER_PATH)
    state_db = tmp_path / "state.db"
    create_state_db(state_db, "20260826_024600_ddeeff00")

    with checker.open_sqlite_read_only(state_db) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        try:
            conn.execute("DELETE FROM sessions")
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("read-only checker connection accepted a write")


def test_run_monitor_exposes_retain_attempt_alerts_in_context() -> None:
    checker = load_path_module("hindsight_checker_context_test", CHECKER_PATH)
    alert = {
        "alert_key": "retain:attempt-context:attempt_incomplete",
        "type": "retain_attempt_incomplete",
        "severity": "high",
        "attempt_id": "attempt-context",
        "session_id": "20260825_220500_00c0ffee",
    }
    checker.get_since = lambda now, coverage_start=None: now - timedelta(hours=1)
    checker.monitor_log_files = lambda: []
    checker.drop_delivered_alerts = lambda alerts, **kwargs: list(alerts)
    checker.check_hindsight_api = lambda: []
    checker.collect_retain_attempt_audit = lambda: {
        "status": "ok",
        "attempt_count": 1,
        "alerts": [alert],
    }
    checker.collect_cron_alerts = lambda since, **kwargs: []
    checker.audit_recent_manual_retain_documents = lambda now, **kwargs: {
        "status": "ok",
        "candidate_count": 0,
        "documents": [],
    }
    output = io.StringIO()

    with redirect_stdout(output):
        code = checker.run_monitor(defer_state=True)

    payload = json.loads(output.getvalue())
    assert code == 0
    assert payload["wakeAgent"] is True
    assert payload["context"]["retain_attempt_alerts"] == [alert]
    assert payload["context"]["retain_attempt_audit"] == {
        "status": "ok",
        "attempt_count": 1,
    }


def test_hindsight_retain_config_reads_bank_and_legacy_manual_context(tmp_path: Path) -> None:
    module = load_module()
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "bank_id": "Configured_Bank-1",
                "retain_context": "configured manual retain context",
            }
        ),
        encoding="utf-8",
    )

    config = module.load_hindsight_retain_config(config_path)

    assert config == {
        "bank_id": "Configured_Bank-1",
        "retain_context": "configured manual retain context",
    }


def test_expected_export_uses_bank_from_hindsight_config(tmp_path: Path) -> None:
    module = load_module()
    session_id = "20260826_033000_aabbccdd"
    attempt_id = "505b0044-581d-44c4-a1a7-c4c74e834b0e"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    exporter = tmp_path / "fake_exporter.py"
    config_path = tmp_path / "config.json"
    create_state_db(state_db, session_id)
    write_valid_exporter(exporter)
    config_path.write_text(
        json.dumps({"bank_id": "ConfiguredBank"}), encoding="utf-8"
    )

    result = module.run_export(
        session_id=session_id,
        output_root=tmp_path / "runs",
        journal_path=journal,
        state_db_path=state_db,
        export_script=exporter,
        python_executable=sys.executable,
        remote_expectation="expected",
        attempt_id=attempt_id,
        hindsight_config_path=config_path,
        remote_writer=lambda payload: {
            "success": True,
            "bank_id": "ConfiguredBank",
            "items_count": 1,
            "async": True,
            "operation_id": payload["operation_id"],
        },
    )

    events = [json.loads(line) for line in journal.read_text().splitlines()]
    remote_started = next(
        event for event in events if event["event"] == "remote_write_started"
    )
    assert remote_started["bank_id"] == "ConfiguredBank"
    assert result["remote"]["bank_id"] == "ConfiguredBank"


def test_run_export_forwards_retain_cutoff_to_exporter(tmp_path: Path) -> None:
    module = load_module()
    session_id = "20260826_034000_ccddeeff"
    cutoff_at = datetime(2026, 8, 26, 3, 40, tzinfo=timezone.utc)
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    exporter = tmp_path / "fake_exporter.py"
    create_state_db(state_db, session_id)
    write_valid_exporter(exporter)

    result = module.run_export(
        session_id=session_id,
        output_root=tmp_path / "runs",
        journal_path=journal,
        state_db_path=state_db,
        export_script=exporter,
        python_executable=sys.executable,
        remote_expectation="not_expected_export_only",
        attempt_id="attempt-cutoff-forwarding",
        cutoff_at=cutoff_at,
    )

    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert result["manifest"]["received_cutoff_at"] == cutoff_at.isoformat()
    assert events[0]["cutoff_at"] == cutoff_at.isoformat()


def test_schedule_retain_records_cutoff_due_and_spawns_detached_child(
    tmp_path: Path,
) -> None:
    module = load_module()
    session_id = "20260826_040000_ddeeff00"
    attempt_id = "b8c5a83a-5f7a-42a3-ad9e-41c23711d568"
    requested_at = datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc)
    journal = tmp_path / "retain-attempts.jsonl"
    captured = {}

    class Process:
        pid = 43210

    def spawn(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Process()

    result = module.schedule_retain(
        session_id=session_id,
        output_root=tmp_path / "runs",
        journal_path=journal,
        state_db_path=tmp_path / "state.db",
        export_script=tmp_path / "exporter.py",
        python_executable=sys.executable,
        remote_expectation="expected",
        delay_seconds=1200,
        attempt_id=attempt_id,
        now=requested_at,
        process_spawner=spawn,
    )

    event = json.loads(journal.read_text().strip())
    due_at = requested_at + timedelta(minutes=20)
    assert event == {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "event": "scheduled",
        "recorded_at": requested_at.isoformat(),
        "requested_at": requested_at.isoformat(),
        "cutoff_at": requested_at.isoformat(),
        "due_at": due_at.isoformat(),
        "delay_seconds": 1200,
        "session_id": session_id,
        "document_id": session_id,
        "remote_expectation": "expected",
        "non_durable_worker": True,
    }
    assert result == {
        "status": "scheduled",
        "attempt_id": attempt_id,
        "session_id": session_id,
        "document_id": session_id,
        "cutoff_at": requested_at.isoformat(),
        "due_at": due_at.isoformat(),
        "delay_seconds": 1200,
        "non_durable_worker": True,
        "worker_pid": 43210,
    }
    assert captured["command"][0] == sys.executable
    assert captured["command"][1:3] == [str(module.Path(module.__file__)), "execute-scheduled"]
    assert captured["command"][captured["command"].index("--cutoff-at") + 1] == requested_at.isoformat()
    assert captured["command"][captured["command"].index("--due-at") + 1] == due_at.isoformat()
    assert captured["kwargs"] == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "start_new_session": True,
        "close_fds": True,
    }


def test_schedule_rejects_invalid_bank_before_receipt_or_spawn(tmp_path: Path) -> None:
    module = load_module()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"bank_id": ""}))
    journal = tmp_path / "retain-attempts.jsonl"
    spawned = []

    with pytest.raises(ValueError, match="invalid Hindsight bank_id"):
        module.schedule_retain(
            session_id="20260826_042000_00112233",
            output_root=tmp_path / "runs",
            journal_path=journal,
            state_db_path=tmp_path / "state.db",
            export_script=tmp_path / "exporter.py",
            python_executable=sys.executable,
            hindsight_config_path=config_path,
            process_spawner=lambda *args, **kwargs: spawned.append((args, kwargs)),
        )

    assert not journal.exists()
    assert spawned == []


def test_execute_scheduled_waits_until_due_and_preserves_cutoff(tmp_path: Path) -> None:
    module = load_module()
    session_id = "20260826_041000_eeff0011"
    attempt_id = "d2689aeb-0b98-4da8-b5e7-c40450f473b8"
    cutoff_at = datetime(2026, 8, 26, 4, 10, tzinfo=timezone.utc)
    due_at = cutoff_at + timedelta(minutes=20)
    observed_now = cutoff_at + timedelta(minutes=7)
    sleeps = []
    captured = {}

    def export_runner(**kwargs):
        captured.update(kwargs)
        return {"status": "accepted", "attempt_id": attempt_id}

    result = module.execute_scheduled_retain(
        session_id=session_id,
        output_root=tmp_path / "runs",
        journal_path=tmp_path / "retain-attempts.jsonl",
        state_db_path=tmp_path / "state.db",
        export_script=tmp_path / "exporter.py",
        python_executable=sys.executable,
        remote_expectation="expected",
        attempt_id=attempt_id,
        cutoff_at=cutoff_at,
        due_at=due_at,
        hindsight_config_path=tmp_path / "config.json",
        now_provider=lambda: observed_now,
        sleeper=sleeps.append,
        export_runner=export_runner,
    )

    assert sleeps == [13 * 60]
    assert result == {"status": "accepted", "attempt_id": attempt_id}
    assert captured["session_id"] == session_id
    assert captured["attempt_id"] == attempt_id
    assert captured["cutoff_at"] == cutoff_at
    assert captured["remote_expectation"] == "expected"


def test_schedule_cli_returns_immediately_with_due_time(tmp_path: Path) -> None:
    module = load_module()
    captured = {}
    scheduled = {
        "status": "scheduled",
        "attempt_id": "a1d23d2b-47c3-48b9-8955-bdef0d262498",
        "session_id": "session-cli-schedule",
        "document_id": "session-cli-schedule",
        "cutoff_at": "2026-08-26T04:20:00+00:00",
        "due_at": "2026-08-26T04:40:00+00:00",
        "delay_seconds": 1200,
        "non_durable_worker": True,
        "worker_pid": 1234,
    }

    def schedule_runner(**kwargs):
        captured.update(kwargs)
        return scheduled

    module.schedule_retain = schedule_runner
    output = io.StringIO()
    with redirect_stdout(output):
        code = module.main(
            [
                "schedule",
                "--session-id",
                "session-cli-schedule",
                "--output-root",
                str(tmp_path / "runs"),
                "--delay-seconds",
                "1200",
            ]
        )

    assert code == 0
    assert json.loads(output.getvalue()) == scheduled
    assert captured["delay_seconds"] == 1200
    assert captured["remote_expectation"] == "expected"


def test_schedule_cli_text_output_is_human_readable(tmp_path: Path) -> None:
    module = load_module()
    scheduled = {
        "status": "scheduled",
        "attempt_id": "a1d23d2b-47c3-48b9-8955-bdef0d262498",
        "session_id": "session-cli-schedule",
        "document_id": "session-cli-schedule",
        "cutoff_at": "2026-08-26T04:20:00+00:00",
        "due_at": "2026-08-26T04:40:00+00:00",
        "delay_seconds": 1200,
        "non_durable_worker": True,
        "worker_pid": 1234,
    }
    setattr(module, "schedule_retain", lambda **_kwargs: scheduled)
    output = io.StringIO()

    with redirect_stdout(output):
        code = module.main(
            [
                "schedule",
                "--session-id",
                "session-cli-schedule",
                "--output-root",
                str(tmp_path / "runs"),
                "--delay-seconds",
                "1200",
                "--output-format",
                "text",
            ]
        )

    assert code == 0
    assert output.getvalue() == "Retain 已排期，将在 20 分钟后保存本次会话。\n"
    assert not output.getvalue().lstrip().startswith("{")


def test_schedule_cli_text_output_is_human_readable_on_failure(tmp_path: Path) -> None:
    module = load_module()

    def fail_schedule(**_kwargs):
        raise ValueError("invalid Hindsight retain config")

    setattr(module, "schedule_retain", fail_schedule)
    output = io.StringIO()

    with redirect_stdout(output):
        code = module.main(
            [
                "schedule",
                "--session-id",
                "session-cli-schedule",
                "--output-root",
                str(tmp_path / "runs"),
                "--output-format",
                "text",
            ]
        )

    assert code == 1
    assert output.getvalue() == "Retain 排期失败（ValueError）。\n"
    assert not output.getvalue().lstrip().startswith("{")


def test_execute_scheduled_cli_parses_fixed_cutoff_and_due(tmp_path: Path) -> None:
    module = load_module()
    captured = {}

    def execute_runner(**kwargs):
        captured.update(kwargs)
        return {"status": "ok", "attempt_id": kwargs["attempt_id"]}

    module.execute_scheduled_retain = execute_runner
    output = io.StringIO()
    with redirect_stdout(output):
        code = module.main(
            [
                "execute-scheduled",
                "--session-id",
                "session-cli-worker",
                "--attempt-id",
                "4ea09112-398e-41d6-80cf-322f5763b805",
                "--cutoff-at",
                "2026-08-26T04:30:00+00:00",
                "--due-at",
                "2026-08-26T04:50:00+00:00",
                "--output-root",
                str(tmp_path / "runs"),
            ]
        )

    assert code == 0
    assert captured["cutoff_at"] == datetime(2026, 8, 26, 4, 30, tzinfo=timezone.utc)
    assert captured["due_at"] == datetime(2026, 8, 26, 4, 50, tzinfo=timezone.utc)
    assert captured["remote_expectation"] == "expected"
    assert json.loads(output.getvalue())["attempt_id"] == captured["attempt_id"]


def test_state_snapshot_uses_retain_cutoff(tmp_path: Path) -> None:
    module = load_module()
    session_id = "20260826_044000_11223344"
    state_db = tmp_path / "state.db"
    with sqlite3.connect(state_db) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY);
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
        conn.execute("INSERT INTO sessions VALUES (?)", (session_id,))
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, 1, 0)",
            [
                (1, session_id, "user", "before cutoff", 1000.0),
                (2, session_id, "assistant", "after cutoff", 2000.0),
            ],
        )

    snapshot = module._state_snapshot(
        state_db,
        session_id,
        cutoff_at=datetime.fromtimestamp(1500, tz=timezone.utc),
    )

    assert snapshot == {
        "session_found": True,
        "active_user_count": 1,
        "active_assistant_count": 0,
        "active_message_count": 1,
        "max_message_id": 1,
    }


def test_invalid_configured_bank_fails_before_remote_write(tmp_path: Path) -> None:
    module = load_module()
    session_id = "20260826_033100_bbccddee"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    exporter = tmp_path / "fake_exporter.py"
    config_path = tmp_path / "config.json"
    create_state_db(state_db, session_id)
    write_valid_exporter(exporter)
    config_path.write_text(json.dumps({"bank_id": ""}), encoding="utf-8")
    remote_called = False

    def must_not_write(_payload):
        nonlocal remote_called
        remote_called = True
        raise AssertionError("invalid Bank config must block the remote write")

    try:
        module.run_export(
            session_id=session_id,
            output_root=tmp_path / "runs",
            journal_path=journal,
            state_db_path=state_db,
            export_script=exporter,
            python_executable=sys.executable,
            remote_expectation="expected",
            attempt_id="7f03797d-3be0-4aa7-ae9e-a9507b90c120",
            hindsight_config_path=config_path,
            remote_writer=must_not_write,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("invalid Bank config must fail closed")

    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert remote_called is False
    assert not any(event["event"].startswith("remote_write_") for event in events)


def test_remote_retain_request_matches_old_manual_retain_contract() -> None:
    module = load_module()
    submitted_at = datetime(2026, 8, 26, 0, 40, tzinfo=timezone.utc)
    session_id = "20260825_140702_01e0605b"
    attempt_id = "2a6f158e-4028-4d84-9d95-e05b0b5ff356"
    content = '[[{"role":"user","content":"User: keep this"}]]'
    digest = __import__("hashlib").sha256(content.encode("utf-8")).hexdigest()
    candidate = {
        "schema_version": "hindsight-conversation-document-v1",
        "session_id": session_id,
        "document_id": session_id,
        "document_content": content,
        "document_content_sha256": digest,
        "turns": [[{"role": "user", "content": "User: keep this"}]],
        "audit": {
            "candidate_turn_count": 1,
            "candidate_message_count": 1,
        },
    }

    request = module.build_remote_retain_request(
        candidate=candidate,
        session_id=session_id,
        attempt_id=attempt_id,
        submitted_at=submitted_at,
        retain_context="configured manual retain context",
    )

    assert module.HINDSIGHT_API_URL == "https://hindsight-api.chantx.top"
    assert request == {
        "items": [
            {
                "content": content,
                "context": "configured manual retain context",
                "document_id": session_id,
                "update_mode": "replace",
            }
        ],
        "async": True,
        "operation_id": attempt_id,
    }


def test_remote_retain_request_rejects_candidate_dual_representation_and_audit_spoof() -> None:
    module = load_module()
    session_id = "20260826_021500_00112233"
    attempt_id = "cf37c2ac-2a8f-4df8-8c6d-143f1623e93f"
    turns = [[{"role": "user", "content": "User: source"}]]
    content = json.dumps(turns, separators=(",", ":"))
    base = {
        "schema_version": "hindsight-conversation-document-v1",
        "session_id": session_id,
        "document_id": session_id,
        "document_content": content,
        "document_content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "turns": turns,
        "audit": {"candidate_turn_count": 1, "candidate_message_count": 1},
    }
    bad_candidates = []
    mismatched_turns = json.loads(json.dumps(base))
    mismatched_turns["turns"] = [[{"role": "user", "content": "User: different"}]]
    bad_candidates.append(mismatched_turns)
    spoofed_counts = json.loads(json.dumps(base))
    spoofed_counts["audit"]["candidate_message_count"] = 99
    bad_candidates.append(spoofed_counts)

    for candidate in bad_candidates:
        try:
            module.build_remote_retain_request(
                candidate=candidate,
                session_id=session_id,
                attempt_id=attempt_id,
                submitted_at=datetime(2026, 8, 26, 2, 15, tzinfo=timezone.utc),
            )
        except ValueError:
            pass
        else:
            raise AssertionError("inconsistent candidate must be rejected")


def test_remote_retain_request_requires_canonical_uuid_operation_id() -> None:
    module = load_module()
    session_id = "20260826_021600_11223344"
    turns = [[{"role": "user", "content": "User: source"}]]
    content = json.dumps(turns, separators=(",", ":"))
    candidate = {
        "schema_version": "hindsight-conversation-document-v1",
        "session_id": session_id,
        "document_id": session_id,
        "document_content": content,
        "document_content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "turns": turns,
        "audit": {"candidate_turn_count": 1, "candidate_message_count": 1},
    }

    try:
        module.build_remote_retain_request(
            candidate=candidate,
            session_id=session_id,
            attempt_id="safe-but-not-a-uuid",
            submitted_at=datetime(2026, 8, 26, 2, 16, tzinfo=timezone.utc),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("remote operation id must be a canonical UUID")


def test_scanner_rejects_candidate_replaced_after_success_receipt(tmp_path: Path) -> None:
    module = load_module()
    started_at = datetime(2026, 8, 26, 2, 17, tzinfo=timezone.utc)
    session_id = "20260826_021700_22334455"
    attempt_id = "a1c791c2-05ad-4c73-9c95-1c7b23d67f8a"
    state_db, journal, result = create_accepted_attempt(
        module,
        tmp_path,
        session_id=session_id,
        attempt_id=attempt_id,
        started_at=started_at,
    )
    candidate_path = Path(result["output_dir"]) / f"candidate_document_{session_id}.json"
    candidate = json.loads(candidate_path.read_text())
    candidate["turns"][0][0]["content"] = "User: replaced after receipt"
    candidate["document_content"] = json.dumps(candidate["turns"], separators=(",", ":"))
    candidate["document_content_sha256"] = hashlib.sha256(
        candidate["document_content"].encode()
    ).hexdigest()
    candidate_path.write_text(json.dumps(candidate))

    scan = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=started_at + timedelta(minutes=10),
        operation_fetcher=lambda operation_id: {
            "status": "found",
            "operation_id": operation_id,
            "operation": {
                "id": operation_id,
                "task_type": "batch_retain",
                "status": "completed",
                "document_id": session_id,
                "items_count": 1,
                "extraction_errors_count": 0,
            },
        },
        document_fetcher=lambda document_id: {
            "status": "found",
            "document_id": document_id,
            "document": {"id": document_id, "original_text": candidate["document_content"]},
        },
    )

    assert scan["remote_confirmed_count"] == 0
    assert [alert["type"] for alert in scan["alerts"]] == [
        "retain_export_artifacts_invalid"
    ]


def test_export_artifacts_are_private(tmp_path: Path) -> None:
    module = load_module()
    session_id = "20260826_021800_33445566"
    state_db = tmp_path / "state.db"
    exporter = tmp_path / "fake_exporter.py"
    output_root = tmp_path / "runs"
    create_state_db(state_db, session_id)
    write_valid_exporter(exporter)
    result = module.run_export(
        session_id=session_id,
        output_root=output_root,
        journal_path=tmp_path / "retain-attempts.jsonl",
        state_db_path=state_db,
        export_script=exporter,
        python_executable=sys.executable,
        remote_expectation="not_expected_export_only",
        attempt_id="attempt-private-artifacts",
        now=datetime(2026, 8, 26, 2, 18, tzinfo=timezone.utc),
    )
    output_dir = Path(result["output_dir"])

    assert output_dir.stat().st_mode & 0o777 == 0o700
    assert output_dir.parent.stat().st_mode & 0o777 == 0o700
    assert output_root.stat().st_mode & 0o777 == 0o700
    for path in output_dir.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600


def test_submit_remote_retain_posts_fixed_endpoint() -> None:
    module = load_module()
    attempt_id = "e87d7aa9-b57c-4597-a0fa-b65c72ca5f91"
    payload = {
        "items": [{"content": "[]", "document_id": "session-1", "update_mode": "replace"}],
        "async": True,
        "operation_id": attempt_id,
    }
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "success": True,
                    "bank_id": "Hermes",
                    "items_count": 1,
                    "async": True,
                    "operation_id": attempt_id,
                }
            ).encode("utf-8")

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return Response()

    result = module.submit_remote_retain(payload, opener=opener, timeout_seconds=10)

    assert captured == {
        "url": "https://hindsight-api.chantx.top/v1/default/banks/Hermes/memories",
        "method": "POST",
        "headers": {"Content-type": "application/json"},
        "body": payload,
        "timeout": 10,
    }
    assert result == {
        "success": True,
        "bank_id": "Hermes",
        "items_count": 1,
        "async": True,
        "operation_id": attempt_id,
    }


def test_submit_remote_retain_distinguishes_rejection_from_uncertain_transport() -> None:
    module = load_module()
    attempt_id = "a348304e-54c5-4891-a74d-c6951cdb9a2c"
    payload = {
        "items": [{"content": "[]", "document_id": "session-1", "update_mode": "replace"}],
        "async": True,
        "operation_id": attempt_id,
    }

    def rejected(request, timeout):
        raise module.HTTPError(request.full_url, 422, "invalid", {}, None)

    def uncertain(request, timeout):
        raise TimeoutError("response lost")

    try:
        module.submit_remote_retain(payload, opener=rejected)
    except module.RemoteWriteRejected:
        pass
    else:
        raise AssertionError("HTTP 422 must be a definite rejection")

    try:
        module.submit_remote_retain(payload, opener=uncertain)
    except module.RemoteWriteUncertain:
        pass
    else:
        raise AssertionError("timeout after POST must be transport-uncertain")


def test_expected_export_journals_remote_write_acceptance(tmp_path: Path) -> None:
    module = load_module()
    now = datetime(2026, 8, 26, 1, 0, tzinfo=timezone.utc)
    session_id = "20260826_010000_0a1b2c3d"
    attempt_id = "e0ff7041-910a-4881-9d48-f6bfbb5deee1"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    output_root = tmp_path / "runs"
    create_state_db(state_db, session_id)
    exporter = tmp_path / "fake_exporter.py"
    exporter.write_text(
        """
import argparse
import hashlib
import json
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("--session-id", required=True)
parser.add_argument("--skip-hindsight", action="store_true")
parser.add_argument("--cutoff-at")
parser.add_argument("--state-db-path")
parser.add_argument("--output-dir", type=Path, required=True)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
turns = [[{"role": "user", "content": "User: persist this"}, {"role": "assistant", "content": "Assistant: persisted"}]]
content = json.dumps(turns, separators=(",", ":"))
digest = hashlib.sha256(content.encode()).hexdigest()
candidate = args.output_dir / f"candidate_document_{args.session_id}.json"
candidate.write_text(json.dumps({"schema_version": "hindsight-conversation-document-v1", "session_id": args.session_id, "document_id": args.session_id, "turns": turns, "document_content": content, "document_content_sha256": digest, "audit": {"candidate_turn_count": 1, "candidate_message_count": 2, "state_reconciliation": {"status": "verified", "source_event_count": 2, "matched_event_count": 2, "added_event_count": 0, "uncovered_event_count": 0}}}))
(args.output_dir / "manifest.json").write_text(json.dumps({"session_id": args.session_id, "read_only": True, "received_cutoff_at": args.cutoff_at, "files": [str(candidate)]}))
print(json.dumps({"session_id": args.session_id}))
""".lstrip(),
        encoding="utf-8",
    )
    submitted_payloads = []

    def writer(payload):
        submitted_payloads.append(payload)
        return {
            "success": True,
            "bank_id": "Hermes",
            "items_count": 1,
            "async": True,
            "operation_id": attempt_id,
        }

    result = module.run_export(
        session_id=session_id,
        output_root=output_root,
        journal_path=journal,
        state_db_path=state_db,
        export_script=exporter,
        python_executable=sys.executable,
        remote_expectation="expected",
        attempt_id=attempt_id,
        now=now,
        remote_writer=writer,
    )

    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [event["event"] for event in events] == [
        "started",
        "export_succeeded",
        "remote_write_started",
        "remote_write_accepted",
    ]
    assert len(submitted_payloads) == 1
    assert submitted_payloads[0]["operation_id"] == attempt_id
    assert submitted_payloads[0]["items"][0]["update_mode"] == "replace"
    assert result["remote"] == {
        "status": "accepted",
        "bank_id": "Hermes",
        "operation_id": attempt_id,
    }


def test_remote_write_failure_is_journaled_and_reported(tmp_path: Path) -> None:
    module = load_module()
    now = datetime(2026, 8, 26, 1, 30, tzinfo=timezone.utc)
    session_id = "20260826_013000_10293847"
    attempt_id = "a4fd0e5a-97ac-44dd-b7e5-e426bde9ee0c"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    output_root = tmp_path / "runs"
    exporter = tmp_path / "fake_exporter.py"
    create_state_db(state_db, session_id)
    write_valid_exporter(exporter)

    def writer(payload):
        raise module.RemoteWriteRejected("simulated rejection")

    try:
        module.run_export(
            session_id=session_id,
            output_root=output_root,
            journal_path=journal,
            state_db_path=state_db,
            export_script=exporter,
            python_executable=sys.executable,
            remote_expectation="expected",
            attempt_id=attempt_id,
            now=now,
            remote_writer=writer,
        )
    except module.RemoteWriteFailure:
        pass
    else:
        raise AssertionError("remote rejection must fail the command")

    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [event["event"] for event in events] == [
        "started",
        "export_succeeded",
        "remote_write_started",
        "remote_write_rejected",
    ]
    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=now + timedelta(minutes=10),
        document_fetcher=lambda document_id: {
            "status": "missing",
            "document_id": document_id,
        },
    )
    assert result["alerts"] == [
        {
            "alert_key": f"retain:{attempt_id}:remote_write_rejected",
            "type": "retain_remote_write_rejected",
            "severity": "high",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "document_id": session_id,
            "operation_id": attempt_id,
            "started_at": now.isoformat(),
            "state_session_found": True,
            "failure_type": "RemoteWriteRejected",
            "message": "Retain 本地候选已完成，但 Hindsight 明确拒绝了写入请求",
        }
    ]


def test_transport_uncertain_is_recovered_by_operation_without_reposting(tmp_path: Path) -> None:
    module = load_module()
    now = datetime(2026, 8, 26, 1, 31, tzinfo=timezone.utc)
    session_id = "20260826_013100_21304958"
    attempt_id = "a2525e04-4e95-4bb6-94b6-cfead7664192"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    output_root = tmp_path / "runs"
    exporter = tmp_path / "fake_exporter.py"
    create_state_db(state_db, session_id)
    write_valid_exporter(exporter)

    def writer(payload):
        raise module.RemoteWriteUncertain("response lost after POST")

    try:
        module.run_export(
            session_id=session_id,
            output_root=output_root,
            journal_path=journal,
            state_db_path=state_db,
            export_script=exporter,
            python_executable=sys.executable,
            remote_expectation="expected",
            attempt_id=attempt_id,
            now=now,
            remote_writer=writer,
        )
    except module.RemoteWriteUncertain:
        pass
    else:
        raise AssertionError("uncertain transport must not return accepted")

    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [event["event"] for event in events] == [
        "started",
        "export_succeeded",
        "remote_write_started",
        "remote_write_uncertain",
    ]
    candidate = json.loads(
        (output_root / session_id / attempt_id / f"candidate_document_{session_id}.json").read_text()
    )
    scan = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=now + timedelta(minutes=10),
        operation_fetcher=lambda operation_id: {
            "status": "found",
            "operation_id": operation_id,
            "operation": {
                "id": operation_id,
                "task_type": "batch_retain",
                "status": "completed",
                "document_id": session_id,
                "items_count": 1,
                "extraction_errors_count": 0,
            },
        },
        document_fetcher=lambda document_id: {
            "status": "found",
            "document_id": document_id,
            "document": {"id": document_id, "original_text": candidate["document_content"]},
        },
    )
    assert scan["alerts"] == []
    assert scan["remote_confirmed_count"] == 1


def test_processing_operation_does_not_report_document_missing(tmp_path: Path) -> None:
    module = load_module()
    started_at = datetime(2026, 8, 26, 2, 0, tzinfo=timezone.utc)
    observed_at = started_at + timedelta(minutes=10)
    session_id = "20260826_020000_aabbccdd"
    attempt_id = "b4c05ef5-97c1-427a-9473-dcbf7c919885"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    output_root = tmp_path / "runs"
    exporter = tmp_path / "fake_exporter.py"
    create_state_db(state_db, session_id)
    write_valid_exporter(exporter)
    module.run_export(
        session_id=session_id,
        output_root=output_root,
        journal_path=journal,
        state_db_path=state_db,
        export_script=exporter,
        python_executable=sys.executable,
        remote_expectation="expected",
        attempt_id=attempt_id,
        now=started_at,
        remote_writer=lambda payload: {
            "success": True,
            "bank_id": "Hermes",
            "items_count": 1,
            "async": True,
            "operation_id": payload["operation_id"],
        },
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=observed_at,
        operation_fetcher=lambda operation_id: {
            "status": "found",
            "operation_id": operation_id,
            "operation": {
                "id": operation_id,
                "status": "processing",
                "task_type": "retain",
                "document_id": session_id,
            },
        },
        document_fetcher=lambda document_id: (_ for _ in ()).throw(
            AssertionError(f"document lookup must wait for operation {document_id}")
        ),
    )

    assert result["alerts"] == []


def test_failed_operation_is_high_alert(tmp_path: Path) -> None:
    module = load_module()
    started_at = datetime(2026, 8, 26, 2, 30, tzinfo=timezone.utc)
    session_id = "20260826_023000_ddeeff00"
    attempt_id = "3e9bfc87-31ac-42dd-90ee-790323605ded"
    state_db, journal, _result = create_accepted_attempt(
        module,
        tmp_path,
        session_id=session_id,
        attempt_id=attempt_id,
        started_at=started_at,
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=started_at + timedelta(minutes=10),
        operation_fetcher=lambda operation_id: {
            "status": "found",
            "operation_id": operation_id,
            "operation": {
                "id": operation_id,
                "status": "failed",
                "task_type": "retain",
                "document_id": session_id,
            },
        },
        document_fetcher=lambda document_id: {
            "status": "missing",
            "document_id": document_id,
        },
    )

    assert result["alerts"] == [
        {
            "alert_key": f"retain:{attempt_id}:remote_operation_failed",
            "type": "retain_remote_operation_failed",
            "severity": "high",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "document_id": session_id,
            "operation_id": attempt_id,
            "operation_status": "failed",
            "started_at": started_at.isoformat(),
            "state_session_found": True,
            "message": "Hindsight 已接受 Retain，但远端 operation 明确失败或取消",
        }
    ]


def test_unavailable_operation_is_unresolved_not_missing(tmp_path: Path) -> None:
    module = load_module()
    started_at = datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc)
    session_id = "20260826_030000_1234cdef"
    attempt_id = "01780c42-ebbe-445a-aa84-15fa04fba7d4"
    state_db, journal, _result = create_accepted_attempt(
        module,
        tmp_path,
        session_id=session_id,
        attempt_id=attempt_id,
        started_at=started_at,
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=started_at + timedelta(minutes=10),
        operation_fetcher=lambda operation_id: {
            "status": "unavailable",
            "operation_id": operation_id,
        },
        document_fetcher=lambda document_id: (_ for _ in ()).throw(
            AssertionError(f"document lookup must not hide unavailable operation {document_id}")
        ),
    )

    assert result["alerts"] == [
        {
            "alert_key": f"retain:{attempt_id}:remote_operation_unavailable",
            "type": "retain_remote_operation_unavailable",
            "severity": "medium",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "document_id": session_id,
            "operation_id": attempt_id,
            "started_at": started_at.isoformat(),
            "state_session_found": True,
            "message": "Hindsight operation 暂时不可达，当前无法确认本次 Retain 处理状态",
        }
    ]


def test_processing_operation_after_stall_window_is_alert(tmp_path: Path) -> None:
    module = load_module()
    started_at = datetime(2026, 8, 26, 3, 30, tzinfo=timezone.utc)
    session_id = "20260826_033000_abcddcba"
    attempt_id = "a5f8e4fb-e35a-473c-8ea2-f8fc2827309d"
    state_db, journal, _result = create_accepted_attempt(
        module,
        tmp_path,
        session_id=session_id,
        attempt_id=attempt_id,
        started_at=started_at,
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=started_at + timedelta(minutes=31),
        operation_stall_seconds=1800,
        operation_fetcher=lambda operation_id: {
            "status": "found",
            "operation_id": operation_id,
            "operation": {
                "id": operation_id,
                "status": "processing",
                "task_type": "retain",
                "document_id": session_id,
            },
        },
        document_fetcher=lambda document_id: (_ for _ in ()).throw(
            AssertionError(f"processing operation must not query document {document_id}")
        ),
    )

    assert result["alerts"] == [
        {
            "alert_key": f"retain:{attempt_id}:remote_operation_stalled",
            "type": "retain_remote_operation_stalled",
            "severity": "medium",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "document_id": session_id,
            "operation_id": attempt_id,
            "operation_status": "processing",
            "started_at": started_at.isoformat(),
            "state_session_found": True,
            "message": "Hindsight 已接受 Retain，但 operation 长时间仍未完成",
        }
    ]


def test_operation_identity_mismatch_is_high_alert(tmp_path: Path) -> None:
    module = load_module()
    started_at = datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc)
    session_id = "20260826_040000_99887766"
    attempt_id = "36572663-dc74-4c77-b843-679aa03ebc03"
    state_db, journal, _result = create_accepted_attempt(
        module,
        tmp_path,
        session_id=session_id,
        attempt_id=attempt_id,
        started_at=started_at,
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=started_at + timedelta(minutes=10),
        operation_fetcher=lambda operation_id: {
            "status": "found",
            "operation_id": operation_id,
            "operation": {
                "id": operation_id,
                "status": "completed",
                "task_type": "retain",
                "document_id": "another-session",
            },
        },
        document_fetcher=lambda document_id: (_ for _ in ()).throw(
            AssertionError(f"mismatched operation must not query document {document_id}")
        ),
    )

    assert result["alerts"] == [
        {
            "alert_key": f"retain:{attempt_id}:remote_operation_identity_mismatch",
            "type": "retain_remote_operation_identity_mismatch",
            "severity": "high",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "document_id": session_id,
            "operation_id": attempt_id,
            "started_at": started_at.isoformat(),
            "state_session_found": True,
            "message": "Hindsight operation 与本次 Retain 的任务类型或 Document 身份不一致",
        }
    ]


def test_unknown_operation_status_is_unresolved(tmp_path: Path) -> None:
    module = load_module()
    started_at = datetime(2026, 8, 26, 4, 30, tzinfo=timezone.utc)
    session_id = "20260826_043000_aa55aa55"
    attempt_id = "b2f03e25-daf3-49ee-8af6-cd28b30d36db"
    state_db, journal, _result = create_accepted_attempt(
        module,
        tmp_path,
        session_id=session_id,
        attempt_id=attempt_id,
        started_at=started_at,
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=started_at + timedelta(minutes=10),
        operation_fetcher=lambda operation_id: {
            "status": "found",
            "operation_id": operation_id,
            "operation": {
                "id": operation_id,
                "status": "retrying",
                "task_type": "retain",
                "document_id": session_id,
            },
        },
        document_fetcher=lambda document_id: (_ for _ in ()).throw(
            AssertionError(f"unknown operation must not query document {document_id}")
        ),
    )

    assert result["alerts"] == [
        {
            "alert_key": f"retain:{attempt_id}:remote_operation_unknown",
            "type": "retain_remote_operation_unavailable",
            "severity": "medium",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "document_id": session_id,
            "operation_id": attempt_id,
            "operation_status": "retrying",
            "started_at": started_at.isoformat(),
            "state_session_found": True,
            "message": "Hindsight operation 返回未知状态，当前无法确认本次 Retain",
        }
    ]


def test_completed_document_with_same_counts_but_different_hash_is_alert(tmp_path: Path) -> None:
    module = load_module()
    started_at = datetime(2026, 8, 26, 5, 0, tzinfo=timezone.utc)
    session_id = "20260826_050000_0f0e0d0c"
    attempt_id = "57d81cf0-ed99-49de-a2ee-127fd2e6e71d"
    state_db, journal, exported = create_accepted_attempt(
        module,
        tmp_path,
        session_id=session_id,
        attempt_id=attempt_id,
        started_at=started_at,
    )
    candidate = json.loads(
        (
            Path(exported["output_dir"])
            / f"candidate_document_{session_id}.json"
        ).read_text()
    )
    candidate_content = candidate["document_content"]
    remote_content = candidate_content.replace("persisted", "changed")

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=started_at + timedelta(minutes=10),
        operation_fetcher=lambda operation_id: {
            "status": "found",
            "operation_id": operation_id,
            "operation": {
                "id": operation_id,
                "status": "completed",
                "task_type": "retain",
                "document_id": session_id,
                "extraction_errors_count": 0,
            },
        },
        document_fetcher=lambda document_id: {
            "status": "found",
            "document_id": document_id,
            "document": {"id": document_id, "original_text": remote_content},
        },
    )

    assert result["remote_confirmed_count"] == 0
    assert result["alerts"] == [
        {
            "alert_key": f"retain:{attempt_id}:remote_document_content_mismatch",
            "type": "retain_remote_document_content_mismatch",
            "severity": "medium",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "document_id": session_id,
            "operation_id": attempt_id,
            "started_at": started_at.isoformat(),
            "state_session_found": True,
            "candidate_sha256": hashlib.sha256(candidate_content.encode()).hexdigest(),
            "remote_sha256": hashlib.sha256(remote_content.encode()).hexdigest(),
            "message": "Hindsight Document 存在，但正文与本次 Retain 候选不完全一致",
        }
    ]


def test_expected_export_cli_reports_accepted_not_confirmed(tmp_path: Path) -> None:
    module = load_module()
    session_id = "20260826_053000_11223344"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "retain-attempts.jsonl"
    exporter = tmp_path / "fake_exporter.py"
    create_state_db(state_db, session_id)
    write_valid_exporter(exporter)
    def accepted_writer(payload, *, bank_id):
        assert bank_id == "Hermes"
        return {
            "success": True,
            "bank_id": bank_id,
            "items_count": 1,
            "async": True,
            "operation_id": payload["operation_id"],
        }

    module.submit_remote_retain = accepted_writer

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = module.main(
            [
                "export",
                "--session-id",
                session_id,
                "--output-root",
                str(tmp_path / "runs"),
                "--journal",
                str(journal),
                "--state-db",
                str(state_db),
                "--export-script",
                str(exporter),
                "--remote-expectation",
                "expected",
            ]
        )

    payload = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert payload["status"] == "accepted"
    assert payload["remote"] == {
        "status": "accepted",
        "bank_id": "Hermes",
        "operation_id": payload["attempt_id"],
    }


def test_fetch_operation_uses_configured_bank_and_rejects_other_task_type(
    tmp_path: Path,
) -> None:
    module = load_module()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"bank_id": "ConfiguredBank"}), encoding="utf-8")
    module.DEFAULT_HINDSIGHT_CONFIG_PATH = config_path
    operation_id = "f21ecf83-fd24-40f7-9a19-589205a19d5b"
    captured = []
    payloads = [
        {
            "operation_id": operation_id,
            "operation_type": "batch_retain",
            "status": "completed",
            "result_metadata": {
                "document_id": "session-1",
                "items_count": 1,
                "unit_ids_count": 4,
                "extraction_errors_count": 0,
            },
            "created_at": "2026-08-26T05:30:00Z",
        },
        {
            "id": operation_id,
            "task_type": "refresh_mental_model",
            "items_count": 1,
            "document_id": "session-1",
            "created_at": "2026-08-26T05:30:00Z",
            "status": "completed",
            "error_message": None,
        },
        {
            "operation_id": operation_id,
            "status": "not_found",
        },
    ]

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def opener(request, timeout):
        captured.append((request.full_url, request.get_method(), timeout))
        return Response(payloads.pop(0))

    module.urlopen = opener
    found = module.fetch_hindsight_operation(operation_id)
    rejected = module.fetch_hindsight_operation(operation_id)
    missing = module.fetch_hindsight_operation(operation_id)

    assert captured == [
        (
            f"https://hindsight-api.chantx.top/v1/default/banks/ConfiguredBank/operations/{operation_id}",
            "GET",
            30,
        ),
        (
            f"https://hindsight-api.chantx.top/v1/default/banks/ConfiguredBank/operations/{operation_id}",
            "GET",
            30,
        ),
        (
            f"https://hindsight-api.chantx.top/v1/default/banks/ConfiguredBank/operations/{operation_id}",
            "GET",
            30,
        ),
    ]
    assert found["status"] == "found"
    assert found["operation"] == {
        "id": operation_id,
        "task_type": "batch_retain",
        "status": "completed",
        "document_id": "session-1",
        "items_count": 1,
        "unit_ids_count": 4,
        "extraction_errors_count": 0,
    }
    assert rejected == {"status": "unavailable", "operation_id": operation_id}
    assert missing == {"status": "missing", "operation_id": operation_id}


def test_completed_operation_with_extraction_errors_is_high_alert(tmp_path: Path) -> None:
    module = load_module()
    started_at = datetime(2026, 8, 26, 6, 0, tzinfo=timezone.utc)
    session_id = "20260826_060000_abcdef12"
    attempt_id = "8199a407-45c5-4b7c-863e-1712e904b023"
    state_db, journal, _result = create_accepted_attempt(
        module,
        tmp_path,
        session_id=session_id,
        attempt_id=attempt_id,
        started_at=started_at,
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=started_at + timedelta(minutes=10),
        operation_fetcher=lambda operation_id: {
            "status": "found",
            "operation_id": operation_id,
            "operation": {
                "id": operation_id,
                "status": "completed",
                "task_type": "batch_retain",
                "document_id": session_id,
                "items_count": 1,
                "unit_ids_count": 4,
                "extraction_errors_count": 2,
            },
        },
        document_fetcher=lambda document_id: (_ for _ in ()).throw(
            AssertionError(f"failed extraction must not confirm document {document_id}")
        ),
    )

    assert result["remote_confirmed_count"] == 0
    assert result["alerts"] == [
        {
            "alert_key": f"retain:{attempt_id}:remote_operation_extraction_errors",
            "type": "retain_remote_operation_extraction_errors",
            "severity": "high",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "document_id": session_id,
            "operation_id": attempt_id,
            "extraction_errors_count": 2,
            "started_at": started_at.isoformat(),
            "state_session_found": True,
            "message": "Hindsight operation 已结束，但 fact extraction 存在明确错误",
        }
    ]


def test_lost_acceptance_receipt_recovers_from_deterministic_operation(tmp_path: Path) -> None:
    module = load_module()
    started_at = datetime(2026, 8, 26, 6, 30, tzinfo=timezone.utc)
    session_id = "20260826_063000_a1b2c3d4"
    attempt_id = "c5e9787d-542c-45ec-95bb-2ed5a740967b"
    state_db, journal, exported = create_accepted_attempt(
        module,
        tmp_path,
        session_id=session_id,
        attempt_id=attempt_id,
        started_at=started_at,
    )
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert events[-1]["event"] == "remote_write_accepted"
    journal.write_text(
        "\n".join(
            json.dumps(event, separators=(",", ":"))
            for event in events
            if event["event"] != "remote_write_accepted"
        )
        + "\n"
    )
    candidate = json.loads(
        (
            Path(exported["output_dir"])
            / f"candidate_document_{session_id}.json"
        ).read_text()
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=started_at + timedelta(minutes=10),
        operation_fetcher=lambda operation_id: {
            "status": "found",
            "operation_id": operation_id,
            "operation": {
                "id": operation_id,
                "status": "completed",
                "task_type": "batch_retain",
                "document_id": session_id,
                "items_count": 1,
                "unit_ids_count": 4,
                "extraction_errors_count": 0,
            },
        },
        document_fetcher=lambda document_id: {
            "status": "found",
            "document_id": document_id,
            "document": {"id": document_id, "original_text": candidate["document_content"]},
        },
    )

    assert result["alerts"] == []
    assert result["remote_confirmed_count"] == 1
    assert result["remote_confirmed_attempts"][0]["operation_id"] == attempt_id


def test_expected_attempt_without_remote_write_started_is_high_alert(tmp_path: Path) -> None:
    module = load_module()
    started_at = datetime(2026, 8, 26, 7, 0, tzinfo=timezone.utc)
    session_id = "20260826_070000_13572468"
    attempt_id = "70a7ba42-9654-4514-b45b-24426f6b1b14"
    state_db, journal, exported = create_accepted_attempt(
        module,
        tmp_path,
        session_id=session_id,
        attempt_id=attempt_id,
        started_at=started_at,
    )
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    journal.write_text(
        "\n".join(
            json.dumps(event, separators=(",", ":"))
            for event in events
            if event["event"] not in {"remote_write_started", "remote_write_accepted"}
        )
        + "\n"
    )
    candidate = json.loads(
        (
            Path(exported["output_dir"])
            / f"candidate_document_{session_id}.json"
        ).read_text()
    )

    result = module.scan_attempts(
        journal_path=journal,
        state_db_path=state_db,
        now=started_at + timedelta(minutes=10),
        operation_fetcher=lambda operation_id: (_ for _ in ()).throw(
            AssertionError(f"no remote write was started for {operation_id}")
        ),
        document_fetcher=lambda document_id: {
            "status": "found",
            "document_id": document_id,
            "document": {"id": document_id, "original_text": candidate["document_content"]},
        },
    )

    assert result["remote_confirmed_count"] == 0
    assert result["alerts"] == [
        {
            "alert_key": f"retain:{attempt_id}:remote_write_not_started",
            "type": "retain_remote_write_not_started",
            "severity": "high",
            "attempt_id": attempt_id,
            "session_id": session_id,
            "document_id": session_id,
            "started_at": started_at.isoformat(),
            "state_session_found": True,
            "message": "Retain 本地候选已完成，但宽限期后仍没有 Hindsight 写入开始凭证",
        }
    ]
