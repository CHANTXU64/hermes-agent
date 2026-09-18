#!/usr/bin/env python3
"""Crash-durable Retain attempt journal and integrity scanner."""
from __future__ import annotations

import argparse
import hashlib
import json
import fcntl
import os
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


HINDSIGHT_API_URL = "https://hindsight-api.chantx.top"
DEFAULT_HINDSIGHT_CONFIG_PATH = Path.home() / ".hermes" / "hindsight" / "config.json"
DEFAULT_RETAIN_CONTEXT = "conversation between Hermes Agent and the User"
DEFAULT_JOURNAL_PATH = Path.home() / ".hermes" / "hindsight" / "retain-attempts.jsonl"
DEFAULT_STATE_DB_PATH = Path.home() / ".hermes" / "state.db"
DEFAULT_EXPORT_SCRIPT = Path(__file__).resolve().with_name("langfuse_hindsight_export.py")
REMOTE_EXPECTATIONS = {
    "not_expected_export_only",
    "expected",
}
RETAIN_OPERATION_TYPES = {"retain", "batch_retain"}
DocumentFetcher = Callable[[str], dict[str, Any]]
OperationFetcher = Callable[[str], dict[str, Any]]

MAX_FAILURE_LOG_CHARS = 4000
_FAILURE_REDACTION = "<redacted>"
_FAILURE_TRUNCATION_MARKER = "...[truncated]..."
_FAILURE_URL_CREDENTIALS_RE = re.compile(
    r"(?i)(https?://)[^/\s:@]+:[^@\s/]+@"
)
_FAILURE_AUTH_RE = re.compile(
    r"(?i)(\b(?:proxy-)?authorization\s*:\s*(?:bearer|basic)\s+|\b(?:bearer|basic)\s+)[^\s,;]+"
)
_FAILURE_FLAG_RE = re.compile(
    r"(?i)(\s--(?:api[-_]?key|secret(?:[-_]?key)?|token|password)(?:=|\s+))[^\s,;]+"
)
_FAILURE_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:[a-z0-9_]*(?:api[-_]?key|secret|token|password|passwd|credential|authorization)[a-z0-9_]*|api[-_]?key|secret|token|password)\s*[:=]\s*)[^\s,;]+"
)


def _sanitize_failure_text(value: Any) -> str:
    """Keep useful failure output while removing credentials and bounding size."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text:
        return ""
    text = _FAILURE_URL_CREDENTIALS_RE.sub(
        lambda match: f"{match.group(1)}{_FAILURE_REDACTION}@",
        text,
    )
    text = _FAILURE_AUTH_RE.sub(
        lambda match: f"{match.group(1)}{_FAILURE_REDACTION}",
        text,
    )
    text = _FAILURE_FLAG_RE.sub(
        lambda match: f"{match.group(1)}{_FAILURE_REDACTION}",
        text,
    )
    text = _FAILURE_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{_FAILURE_REDACTION}",
        text,
    )
    if len(text) <= MAX_FAILURE_LOG_CHARS:
        return text
    marker = f"\n{_FAILURE_TRUNCATION_MARKER} (original_chars={len(text)})\n"
    if len(marker) >= MAX_FAILURE_LOG_CHARS:
        return marker[:MAX_FAILURE_LOG_CHARS]
    remaining = MAX_FAILURE_LOG_CHARS - len(marker)
    head_chars = remaining // 2
    tail_chars = remaining - head_chars
    return f"{text[:head_chars]}{marker}{text[-tail_chars:]}"


class ExportFailure(RuntimeError):
    """The wrapped read-only export did not produce a valid manifest."""


class RemoteWriteFailure(RuntimeError):
    """Base class for a retain submission that did not return accepted."""


class RemoteWriteRejected(RemoteWriteFailure):
    """The server definitively rejected the retain request before acceptance."""


class RemoteWriteUncertain(RemoteWriteFailure):
    """The POST may have been accepted; recover only through operation lookup."""


def _canonical_uuid(value: str, label: str) -> str:
    text = _safe_component(value, label)
    try:
        parsed = uuid.UUID(text)
    except ValueError as exc:
        raise ValueError(f"invalid {label}") from exc
    if str(parsed) != text:
        raise ValueError(f"invalid {label}")
    return text


def _strict_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid {label}")
    return value


def _validate_candidate(
    candidate: Any,
    session_id: str,
    *,
    expected_sha256: str | None = None,
    expected_turn_count: int | None = None,
    expected_message_count: int | None = None,
) -> dict[str, Any]:
    """Return one internally consistent, receipt-bound candidate representation."""
    if not isinstance(candidate, dict) or candidate.get("session_id") != session_id:
        raise ValueError("candidate session_id does not match retain session")
    if candidate.get("document_id") not in {None, session_id}:
        raise ValueError("candidate document_id does not match retain session")
    content = candidate.get("document_content")
    if not isinstance(content, str) or not content:
        raise ValueError("candidate document_content is unavailable")
    try:
        content_turns = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("candidate document_content is invalid JSON") from exc
    turns = candidate.get("turns")
    if not isinstance(content_turns, list) or turns != content_turns:
        raise ValueError("candidate turns and document_content do not match")

    counts = {"user": 0, "assistant": 0}
    message_count = 0
    for turn in content_turns:
        if not isinstance(turn, list):
            raise ValueError("candidate turn is invalid")
        for message in turn:
            if not isinstance(message, dict):
                raise ValueError("candidate message is invalid")
            role = str(message.get("role") or "")
            content_value = message.get("content")
            if role not in counts or not isinstance(content_value, str) or not content_value.strip():
                raise ValueError("candidate message role or content is invalid")
            counts[role] += 1
            message_count += 1

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if candidate.get("document_content_sha256") != digest:
        raise ValueError("candidate document_content hash mismatch")
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError("candidate content no longer matches success receipt")
    audit = candidate.get("audit")
    if not isinstance(audit, dict):
        raise ValueError("candidate audit is unavailable")
    turn_count = len(content_turns)
    audit_turn_count = _strict_nonnegative_int(
        audit.get("candidate_turn_count"), "candidate turn count"
    )
    audit_message_count = _strict_nonnegative_int(
        audit.get("candidate_message_count"), "candidate message count"
    )
    if audit_turn_count != turn_count or audit_message_count != message_count:
        raise ValueError("candidate audit counts do not match document_content")
    if expected_turn_count is not None and turn_count != expected_turn_count:
        raise ValueError("candidate turn count no longer matches success receipt")
    if expected_message_count is not None and message_count != expected_message_count:
        raise ValueError("candidate message count no longer matches success receipt")
    schema_version = str(candidate.get("schema_version") or "")
    if not schema_version:
        raise ValueError("candidate schema_version is unavailable")
    return {
        "content": content,
        "sha256": digest,
        "schema_version": schema_version,
        "turn_count": turn_count,
        "message_count": message_count,
        "audit": audit,
        "counts": {
            "user": counts["user"],
            "assistant": counts["assistant"],
            "total": message_count,
        },
    }


def build_remote_retain_request(
    *,
    candidate: dict[str, Any],
    session_id: str,
    attempt_id: str,
    submitted_at: datetime,
    retain_context: str | None = DEFAULT_RETAIN_CONTEXT,
) -> dict[str, Any]:
    """Build one async replace request matching the retired manual-Retain contract."""
    session_id = _safe_component(session_id, "session_id")
    attempt_id = _canonical_uuid(attempt_id, "attempt_id")
    material = _validate_candidate(candidate, session_id)
    item: dict[str, Any] = {
        "content": material["content"],
        "document_id": session_id,
        "update_mode": "replace",
    }
    if retain_context:
        item["context"] = retain_context
    return {
        "items": [item],
        "async": True,
        "operation_id": attempt_id,
    }


def submit_remote_retain(
    payload: dict[str, Any],
    *,
    bank_id: str | None = None,
    opener: Callable[..., Any] = urlopen,
    timeout_seconds: float = 10.0,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Submit one bounded async retain to the fixed approved Hindsight API."""
    operation_id = str(payload.get("operation_id") or "")
    if not operation_id:
        raise RemoteWriteRejected("remote retain operation_id is unavailable")
    resolved_bank_id = bank_id or str(
        load_hindsight_retain_config(DEFAULT_HINDSIGHT_CONFIG_PATH)["bank_id"]
    )
    url = (
        f"{HINDSIGHT_API_URL}/v1/default/banks/"
        f"{quote(resolved_bank_id, safe='')}/memories"
    )
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        ),
        headers=headers,
        method="POST",
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            response_status = int(getattr(response, "status", 0))
            if response_status not in {200, 202}:
                if 400 <= response_status < 500 and response_status not in {
                    408,
                    409,
                    425,
                    429,
                }:
                    raise RemoteWriteRejected(
                        "remote retain was rejected before acceptance"
                    )
                raise RemoteWriteUncertain(
                    "remote retain returned an uncertain status"
                )
            result = json.loads(response.read().decode("utf-8"))
    except (RemoteWriteRejected, RemoteWriteUncertain):
        raise
    except HTTPError as exc:
        if 400 <= exc.code < 500 and exc.code not in {408, 409, 425, 429}:
            raise RemoteWriteRejected(
                "remote retain was rejected before acceptance"
            ) from exc
        raise RemoteWriteUncertain("remote retain response was not confirmed") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RemoteWriteUncertain("remote retain response was not confirmed") from exc
    if not isinstance(result, dict):
        raise RemoteWriteUncertain("remote retain response is invalid")
    return _validate_remote_acceptance(result, operation_id, resolved_bank_id)


def _validate_remote_acceptance(
    result: Any,
    operation_id: str,
    bank_id: str | None = None,
) -> dict[str, Any]:
    resolved_bank_id = bank_id or str(
        load_hindsight_retain_config(DEFAULT_HINDSIGHT_CONFIG_PATH)["bank_id"]
    )
    expected = {
        "success": True,
        "bank_id": resolved_bank_id,
        "items_count": 1,
        "async": True,
        "operation_id": operation_id,
    }
    if any(result.get(key) != value for key, value in expected.items()):
        raise RemoteWriteUncertain("remote retain response identity mismatch")
    return {key: result[key] for key in expected}


def _parse_time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_events(
    journal_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, int] | None]:
    if not journal_path.exists():
        return [], None
    events: list[dict[str, Any]] = []
    torn_tail: dict[str, int] | None = None
    lock_path = journal_path.with_name(f".{journal_path.name}.lock")
    with lock_path.open("a+b") as lock_handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_SH)
        raw_lines = journal_path.read_bytes().splitlines(keepends=True)
        for index, raw_line in enumerate(raw_lines):
            line_number = index + 1
            has_newline = raw_line.endswith(b"\n")
            payload = raw_line[:-1] if has_newline else raw_line
            if not payload.strip():
                continue
            try:
                event = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if index == len(raw_lines) - 1 and not has_newline:
                    torn_tail = {
                        "line_number": line_number,
                        "discarded_bytes": len(raw_line),
                    }
                    break
                raise ValueError(
                    f"invalid retain attempt journal line {line_number}"
                ) from exc
            if not isinstance(event, dict):
                raise ValueError(f"invalid retain attempt journal line {line_number}")
            events.append(event)
    return events, torn_tail


def _state_db_candidates(state_db_path: Path) -> tuple[Path, ...]:
    """Return the configured StateDB plus default/profile siblings."""
    configured = Path(state_db_path).expanduser()
    candidates = [configured]
    if configured.name == "state.db":
        parent = configured.parent
        hermes_home = (
            parent.parent.parent
            if parent.parent.name == "profiles"
            else parent
        )
        candidates.append(hermes_home / "state.db")
        profiles_dir = hermes_home / "profiles"
        if profiles_dir.is_dir():
            candidates.extend(sorted(profiles_dir.glob("*/state.db")))
    return tuple(dict.fromkeys(candidates))


def _state_db_for_session(state_db_path: Path, session_id: str) -> Path | None:
    for candidate in _state_db_candidates(state_db_path):
        if not candidate.exists():
            continue
        try:
            with sqlite3.connect(f"file:{candidate}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ? LIMIT 1",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error:
            continue
        if row is not None:
            return candidate
    return None


def _session_exists(state_db_path: Path, session_id: str) -> bool:
    return _state_db_for_session(state_db_path, session_id) is not None


def _state_snapshot(
    state_db_path: Path,
    session_id: str,
    *,
    cutoff_at: datetime | None = None,
) -> dict[str, Any]:
    empty = {
        "session_found": False,
        "active_user_count": 0,
        "active_assistant_count": 0,
        "active_message_count": 0,
        "max_message_id": None,
    }
    resolved_state_db_path = _state_db_for_session(state_db_path, session_id)
    if resolved_state_db_path is None:
        return empty
    try:
        with sqlite3.connect(
            f"file:{resolved_state_db_path}?mode=ro", uri=True
        ) as conn:
            conn.row_factory = sqlite3.Row
            session_found = conn.execute(
                "SELECT 1 FROM sessions WHERE id = ? LIMIT 1",
                (session_id,),
            ).fetchone() is not None
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(messages)")
            }
            optional = {
                "tool_calls": "tool_calls" if "tool_calls" in columns else "NULL AS tool_calls",
                "finish_reason": (
                    "finish_reason" if "finish_reason" in columns else "NULL AS finish_reason"
                ),
                "display_kind": (
                    "display_kind" if "display_kind" in columns else "NULL AS display_kind"
                ),
            }
            rows = conn.execute(
                f"""
                SELECT id, role, content, timestamp, compacted,
                       {optional['tool_calls']},
                       {optional['finish_reason']},
                       {optional['display_kind']}
                FROM messages
                WHERE session_id = ? AND (active = 1 OR compacted = 1)
                  AND role IN ('user', 'assistant')
                ORDER BY id
                """,
                (session_id,),
            ).fetchall()
    except sqlite3.Error:
        return empty

    cutoff_seconds = None
    if cutoff_at is not None:
        if cutoff_at.tzinfo is None:
            cutoff_at = cutoff_at.replace(tzinfo=timezone.utc)
        cutoff_seconds = cutoff_at.astimezone(timezone.utc).timestamp()
        eligible_rows = []
        for row in rows:
            value = row["timestamp"]
            try:
                row_seconds = (
                    float(value)
                    if isinstance(value, (int, float))
                    else _parse_time(value).timestamp()
                )
            except (TypeError, ValueError):
                continue
            if row_seconds <= cutoff_seconds:
                eligible_rows.append(row)
        rows = eligible_rows

    runtime_prefixes = (
        "[ASYNC DELEGATION COMPLETE —",
        "[ASYNC DELEGATION BATCH COMPLETE —",
        "Operation interrupted: waiting for model response",
        "Operation interrupted: retrying API call after error",
        "You just executed tool calls but returned an empty response.",
        "[IMPORTANT: Background process ",
        "[IMPORTANT: Background task ",
        "[kanban] ",
        "[System: Your previous response contained only internal reasoning and never produced a visible answer or tool call.",
    )
    compression_prefixes = (
        "[Session Arc Summary ",
        "[Your active task list was preserved across context compression]",
        "[Current user objective preserved from compacted history]",
        "[Recent Summary (",
        "[CONTEXT COMPACTION —",
        "[Durable Summary (",
    )
    visible: list[tuple[str, str, Any]] = []
    for row in rows:
        role = str(row["role"] or "")
        display_kind = str(row["display_kind"] or "")
        if display_kind in {"hidden", "internal_notification"}:
            continue
        if role == "assistant" and (
            bool(row["tool_calls"])
            or str(row["finish_reason"] or "") == "tool_calls"
        ):
            continue
        content = " ".join(
            unicodedata.normalize("NFKC", str(row["content"] or ""))
            .replace("\r\n", "\n")
            .split()
        )
        if not content or any(content.startswith(prefix) for prefix in runtime_prefixes):
            continue
        if any(content.startswith(prefix) for prefix in compression_prefixes) or bool(
            re.match(r"\[Depth-\d+ Summary \(", content)
        ):
            continue
        visible.append((role, content, row["timestamp"]))

    # StateDB can retain duplicate physical copies around compression. The
    # existing monitor treats identical role/content/timestamp rows as one
    # logical occurrence for coarse completeness checks.
    logical = list(dict.fromkeys(visible))
    user_count = sum(1 for role, _content, _timestamp in logical if role == "user")
    assistant_count = sum(
        1 for role, _content, _timestamp in logical if role == "assistant"
    )
    return {
        "session_found": session_found,
        "active_user_count": user_count,
        "active_assistant_count": assistant_count,
        "active_message_count": user_count + assistant_count,
        "max_message_id": max((int(row["id"]) for row in rows), default=None),
    }


def append_event_durable(journal_path: Path, event: dict[str, Any]) -> None:
    """Append one complete JSONL event and force it to stable local storage."""
    journal_path = Path(journal_path)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = journal_path.with_name(f".{journal_path.name}.lock")
    encoded = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    with lock_path.open("a+b") as lock_handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        fd = os.open(journal_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            written = 0
            while written < len(encoded):
                written += os.write(fd, encoded[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        directory_fd = os.open(journal_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _safe_component(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not text or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for character in text):
        raise ValueError(f"invalid {label}")
    return text


def load_hindsight_retain_config(config_path: Path) -> dict[str, str | None]:
    """Load the manual-Retain Bank and context from provider configuration."""
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Hindsight retain config") from exc
    if not isinstance(config, dict):
        raise ValueError("invalid Hindsight retain config")
    raw_bank_id = config.get("bank_id")
    if not isinstance(raw_bank_id, str):
        raise ValueError("invalid Hindsight bank_id")
    bank_id = raw_bank_id.strip()
    if not bank_id or any(
        character
        not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for character in bank_id
    ):
        raise ValueError("invalid Hindsight bank_id")
    retain_context = config.get("retain_context", DEFAULT_RETAIN_CONTEXT)
    if retain_context is not None and not isinstance(retain_context, str):
        raise ValueError("invalid Hindsight retain_context")
    return {"bank_id": bank_id, "retain_context": retain_context}


def _read_export_artifacts(output_dir: Path, session_id: str) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    manifest_path = output_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExportFailure("export manifest is missing or invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("session_id") != session_id:
        raise ExportFailure("export manifest has the wrong session")
    candidate_path = output_dir / f"candidate_document_{session_id}.json"
    try:
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExportFailure("candidate document is missing or invalid") from exc
    if not isinstance(candidate, dict) or candidate.get("session_id") != session_id:
        raise ExportFailure("candidate document has the wrong session")
    return manifest, manifest_path, candidate


def _harden_and_sync_artifacts(
    output_dir: Path,
    manifest: dict[str, Any],
    manifest_path: Path,
    candidate_path: Path,
) -> None:
    root = output_dir.resolve()
    paths = [manifest_path, candidate_path]
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, list):
        raise ExportFailure("export manifest file list is unavailable")
    paths.extend(Path(str(value)) for value in manifest_files)
    seen: set[Path] = set()
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ExportFailure("export manifest contains an invalid artifact path")
        os.chmod(resolved, 0o600)
        fd = os.open(resolved, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    os.chmod(output_dir, 0o700)
    os.chmod(output_dir.parent, 0o700)
    for directory in (output_dir, output_dir.parent):
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def schedule_retain(
    *,
    session_id: str,
    output_root: Path,
    journal_path: Path = DEFAULT_JOURNAL_PATH,
    state_db_path: Path = DEFAULT_STATE_DB_PATH,
    export_script: Path = DEFAULT_EXPORT_SCRIPT,
    python_executable: str,
    remote_expectation: str = "expected",
    delay_seconds: int = 1200,
    attempt_id: str | None = None,
    now: datetime | None = None,
    hindsight_config_path: Path = DEFAULT_HINDSIGHT_CONFIG_PATH,
    process_spawner: Callable[..., Any] = subprocess.Popen,
) -> dict[str, Any]:
    session_id = _safe_component(session_id, "session_id")
    attempt_id = _safe_component(attempt_id or str(uuid.uuid4()), "attempt_id")
    if remote_expectation not in REMOTE_EXPECTATIONS:
        raise ValueError("invalid remote expectation")
    if remote_expectation == "expected":
        attempt_id = _canonical_uuid(attempt_id, "attempt_id")
    if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, int) or delay_seconds < 0:
        raise ValueError("invalid delay_seconds")
    load_hindsight_retain_config(Path(hindsight_config_path))
    requested_at = now or datetime.now(timezone.utc)
    if requested_at.tzinfo is None:
        requested_at = requested_at.replace(tzinfo=timezone.utc)
    requested_at = requested_at.astimezone(timezone.utc)
    due_at = requested_at + timedelta(seconds=delay_seconds)
    event = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "event": "scheduled",
        "recorded_at": requested_at.isoformat(),
        "requested_at": requested_at.isoformat(),
        "cutoff_at": requested_at.isoformat(),
        "due_at": due_at.isoformat(),
        "delay_seconds": delay_seconds,
        "session_id": session_id,
        "document_id": session_id,
        "remote_expectation": remote_expectation,
        "non_durable_worker": True,
    }
    append_event_durable(Path(journal_path), event)
    command = [
        str(python_executable),
        str(Path(__file__).resolve()),
        "execute-scheduled",
        "--session-id",
        session_id,
        "--attempt-id",
        attempt_id,
        "--cutoff-at",
        requested_at.isoformat(),
        "--due-at",
        due_at.isoformat(),
        "--output-root",
        str(output_root),
        "--journal",
        str(journal_path),
        "--state-db",
        str(state_db_path),
        "--export-script",
        str(export_script),
        "--hindsight-config",
        str(hindsight_config_path),
        "--remote-expectation",
        remote_expectation,
    ]
    worker = process_spawner(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return {
        "status": "scheduled",
        "attempt_id": attempt_id,
        "session_id": session_id,
        "document_id": session_id,
        "cutoff_at": requested_at.isoformat(),
        "due_at": due_at.isoformat(),
        "delay_seconds": delay_seconds,
        "non_durable_worker": True,
        "worker_pid": int(worker.pid),
    }


def run_export(
    *,
    session_id: str,
    output_root: Path,
    journal_path: Path = DEFAULT_JOURNAL_PATH,
    state_db_path: Path = DEFAULT_STATE_DB_PATH,
    export_script: Path = DEFAULT_EXPORT_SCRIPT,
    python_executable: str,
    remote_expectation: str = "not_expected_export_only",
    attempt_id: str | None = None,
    now: datetime | None = None,
    cutoff_at: datetime | None = None,
    hindsight_config_path: Path = DEFAULT_HINDSIGHT_CONFIG_PATH,
    remote_writer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    session_id = _safe_component(session_id, "session_id")
    attempt_id = _safe_component(attempt_id or str(uuid.uuid4()), "attempt_id")
    if remote_expectation not in REMOTE_EXPECTATIONS:
        raise ValueError("invalid remote expectation")
    if remote_expectation == "expected":
        attempt_id = _canonical_uuid(attempt_id, "attempt_id")
    cutoff_iso = None
    if cutoff_at is not None:
        if cutoff_at.tzinfo is None:
            cutoff_at = cutoff_at.replace(tzinfo=timezone.utc)
        cutoff_iso = cutoff_at.astimezone(timezone.utc).isoformat()
    recorded_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    output_root = Path(output_root)
    output_dir = output_root / session_id / attempt_id
    requested_state_db_path = Path(state_db_path)
    resolved_state_db_path = (
        _state_db_for_session(requested_state_db_path, session_id)
        or requested_state_db_path
    )
    started = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "event": "started",
        "recorded_at": recorded_at,
        "session_id": session_id,
        "document_id": session_id,
        "remote_expectation": remote_expectation,
        "output_dir": str(output_dir),
        "state_snapshot": _state_snapshot(
            resolved_state_db_path,
            session_id,
            cutoff_at=cutoff_at,
        ),
    }
    if cutoff_iso is not None:
        started["cutoff_at"] = cutoff_iso
    append_event_durable(Path(journal_path), started)

    command = [
        str(python_executable),
        str(export_script),
        "--session-id",
        session_id,
        "--skip-hindsight",
        "--output-dir",
        str(output_dir),
        "--state-db-path",
        str(resolved_state_db_path),
    ]
    if cutoff_iso is not None:
        command.extend(["--cutoff-at", cutoff_iso])
    completed = None
    failure_stage = "prepare_output"
    try:
        output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(output_root, 0o700)
        output_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(output_dir.parent, 0o700)
        output_dir.mkdir(mode=0o700, exist_ok=False)

        failure_stage = "export_process"
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ExportFailure("export process failed")

        failure_stage = "read_export_artifacts"
        manifest, manifest_path, candidate = _read_export_artifacts(output_dir, session_id)
        failure_stage = "validate_candidate"
        material = _validate_candidate(candidate, session_id)
        candidate_path = output_dir / f"candidate_document_{session_id}.json"
        failure_stage = "harden_artifacts"
        _harden_and_sync_artifacts(
            output_dir,
            manifest,
            manifest_path,
            candidate_path,
        )
    except Exception as exc:
        failure_event = {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "event": "export_failed",
            "recorded_at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
            "session_id": session_id,
            "document_id": session_id,
            "failure_stage": failure_stage,
            "failure_type": type(exc).__name__,
            "failure_message": _sanitize_failure_text(str(exc)),
        }
        if completed is not None:
            failure_event.update(
                {
                    "exporter_returncode": int(completed.returncode),
                    "exporter_stdout": _sanitize_failure_text(completed.stdout),
                    "exporter_stderr": _sanitize_failure_text(completed.stderr),
                }
            )
        append_event_durable(Path(journal_path), failure_event)
        if isinstance(exc, ExportFailure):
            raise
        raise ExportFailure("export could not be completed") from exc

    success = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "event": "export_succeeded",
        "recorded_at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
        "session_id": session_id,
        "document_id": session_id,
        "manifest_path": str(manifest_path),
        "candidate_path": str(candidate_path),
        "candidate_sha256": material["sha256"],
        "candidate_turn_count": material["turn_count"],
        "candidate_message_count": material["message_count"],
    }
    append_event_durable(Path(journal_path), success)
    result = {
        "attempt_id": attempt_id,
        "session_id": session_id,
        "document_id": session_id,
        "remote_expectation": remote_expectation,
        "output_dir": str(output_dir),
        "manifest": manifest,
    }
    if remote_expectation != "expected":
        return result

    reconciliation = material["audit"].get("state_reconciliation")
    if not _state_reconciliation_is_verified(material["audit"]):
        reconciliation_status = (
            str(reconciliation.get("status") or "invalid")
            if isinstance(reconciliation, dict)
            else "missing"
        )
        append_event_durable(
            Path(journal_path),
            {
                "schema_version": 1,
                "attempt_id": attempt_id,
                "event": "remote_write_blocked_unverified_candidate",
                "recorded_at": (now or datetime.now(timezone.utc))
                .astimezone(timezone.utc)
                .isoformat(),
                "session_id": session_id,
                "document_id": session_id,
                "state_reconciliation_status": reconciliation_status,
            },
        )
        result["status"] = "blocked_unverified_candidate"
        return result

    state_snapshot = started.get("state_snapshot")
    if isinstance(state_snapshot, dict) and _candidate_gap_is_severe(
        state_snapshot, material["counts"]
    ):
        state_total = int(state_snapshot.get("active_message_count") or 0)
        candidate_total = material["counts"]["total"]
        append_event_durable(
            Path(journal_path),
            {
                "schema_version": 1,
                "attempt_id": attempt_id,
                "event": "remote_write_blocked_incomplete_candidate",
                "recorded_at": (now or datetime.now(timezone.utc))
                .astimezone(timezone.utc)
                .isoformat(),
                "session_id": session_id,
                "document_id": session_id,
                "state_active_message_count": state_total,
                "candidate_message_count": candidate_total,
                "missing_message_count": max(0, state_total - candidate_total),
            },
        )
        result["status"] = "blocked_incomplete_candidate"
        return result

    visible_event_gap = (
        _candidate_visible_event_gap(state_snapshot, material["counts"], material["audit"])
        if isinstance(state_snapshot, dict)
        else None
    )
    if visible_event_gap is not None:
        append_event_durable(
            Path(journal_path),
            {
                "schema_version": 1,
                "attempt_id": attempt_id,
                "event": "remote_write_blocked_visible_event_gap",
                "recorded_at": (now or datetime.now(timezone.utc))
                .astimezone(timezone.utc)
                .isoformat(),
                "session_id": session_id,
                "document_id": session_id,
                "candidate_user_count": material["counts"]["user"],
                "candidate_assistant_count": material["counts"]["assistant"],
                **visible_event_gap,
            },
        )
        result["status"] = "blocked_visible_event_gap"
        return result

    operation_id = attempt_id
    submitted_at = now or datetime.now(timezone.utc)
    retain_config = load_hindsight_retain_config(hindsight_config_path)
    bank_id = str(retain_config["bank_id"])
    try:
        payload = build_remote_retain_request(
            candidate=candidate,
            session_id=session_id,
            attempt_id=attempt_id,
            submitted_at=submitted_at,
            retain_context=retain_config["retain_context"],
        )
        append_event_durable(
            Path(journal_path),
            {
                "schema_version": 1,
                "attempt_id": attempt_id,
                "event": "remote_write_started",
                "recorded_at": submitted_at.astimezone(timezone.utc).isoformat(),
                "session_id": session_id,
                "document_id": session_id,
                "operation_id": operation_id,
                "bank_id": bank_id,
                "update_mode": "replace",
                "candidate_sha256": material["sha256"],
            },
        )
        if remote_writer is None:
            accepted = submit_remote_retain(payload, bank_id=bank_id)
        else:
            accepted = remote_writer(payload)
        accepted = _validate_remote_acceptance(accepted, operation_id, bank_id)
    except Exception as exc:
        if isinstance(exc, RemoteWriteRejected):
            remote_event = "remote_write_rejected"
            raised: RemoteWriteFailure = exc
        elif isinstance(exc, RemoteWriteUncertain):
            remote_event = "remote_write_uncertain"
            raised = exc
        else:
            remote_event = "remote_write_uncertain"
            raised = RemoteWriteUncertain(
                "remote retain outcome is unknown; inspect deterministic operation"
            )
        append_event_durable(
            Path(journal_path),
            {
                "schema_version": 1,
                "attempt_id": attempt_id,
                "event": remote_event,
                "recorded_at": (now or datetime.now(timezone.utc))
                .astimezone(timezone.utc)
                .isoformat(),
                "session_id": session_id,
                "document_id": session_id,
                "operation_id": operation_id,
                "failure_type": type(exc).__name__,
            },
        )
        if raised is exc:
            raise
        raise raised from exc

    append_event_durable(
        Path(journal_path),
        {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "event": "remote_write_accepted",
            "recorded_at": (now or datetime.now(timezone.utc))
            .astimezone(timezone.utc)
            .isoformat(),
            "session_id": session_id,
            "document_id": session_id,
            "operation_id": accepted["operation_id"],
            "bank_id": accepted["bank_id"],
            "candidate_sha256": material["sha256"],
        },
    )
    result["status"] = "accepted"
    result["remote"] = {
        "status": "accepted",
        "bank_id": accepted["bank_id"],
        "operation_id": accepted["operation_id"],
    }
    return result


def execute_scheduled_retain(
    *,
    session_id: str,
    output_root: Path,
    journal_path: Path = DEFAULT_JOURNAL_PATH,
    state_db_path: Path = DEFAULT_STATE_DB_PATH,
    export_script: Path = DEFAULT_EXPORT_SCRIPT,
    python_executable: str,
    remote_expectation: str,
    attempt_id: str,
    cutoff_at: datetime,
    due_at: datetime,
    hindsight_config_path: Path = DEFAULT_HINDSIGHT_CONFIG_PATH,
    now_provider: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleeper: Callable[[float], Any] = time.sleep,
    export_runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    for label, value in (("cutoff_at", cutoff_at), ("due_at", due_at)):
        if not isinstance(value, datetime):
            raise ValueError(f"invalid {label}")
    if cutoff_at.tzinfo is None:
        cutoff_at = cutoff_at.replace(tzinfo=timezone.utc)
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=timezone.utc)
    cutoff_at = cutoff_at.astimezone(timezone.utc)
    due_at = due_at.astimezone(timezone.utc)
    observed_now = now_provider()
    if observed_now.tzinfo is None:
        observed_now = observed_now.replace(tzinfo=timezone.utc)
    remaining = (due_at - observed_now.astimezone(timezone.utc)).total_seconds()
    if remaining > 0:
        sleeper(remaining)
    runner = export_runner or run_export
    return runner(
        session_id=session_id,
        output_root=output_root,
        journal_path=journal_path,
        state_db_path=state_db_path,
        export_script=export_script,
        python_executable=python_executable,
        remote_expectation=remote_expectation,
        attempt_id=attempt_id,
        cutoff_at=cutoff_at,
        hindsight_config_path=hindsight_config_path,
    )


def fetch_hindsight_document(
    document_id: str,
    *,
    bank_id: str | None = None,
) -> dict[str, Any]:
    """Read one document from the configured Bank at the approved endpoint."""
    document_id = _safe_component(document_id, "document_id")
    resolved_bank_id = bank_id or str(
        load_hindsight_retain_config(DEFAULT_HINDSIGHT_CONFIG_PATH)["bank_id"]
    )
    url = (
        f"{HINDSIGHT_API_URL}/v1/default/banks/{quote(resolved_bank_id, safe='')}/documents/"
        f"{quote(document_id, safe='')}"
    )
    request = Request(url, method="GET")
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            return {"status": "missing", "document_id": document_id}
        return {
            "status": "unavailable",
            "document_id": document_id,
            "http_status": int(exc.code),
        }
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        return {"status": "unavailable", "document_id": document_id}
    if not isinstance(payload, dict):
        return {"status": "unavailable", "document_id": document_id}
    actual_document_id = str(
        payload.get("id") or payload.get("document_id") or ""
    )
    if actual_document_id != document_id:
        return {
            "status": "identity_mismatch",
            "document_id": document_id,
        }
    return {
        "status": "found",
        "document_id": document_id,
        "document": payload,
    }


def fetch_hindsight_operation(
    operation_id: str,
    *,
    bank_id: str | None = None,
) -> dict[str, Any]:
    """Read one async retain operation from the configured Bank."""
    operation_id = _safe_component(operation_id, "operation_id")
    resolved_bank_id = bank_id or str(
        load_hindsight_retain_config(DEFAULT_HINDSIGHT_CONFIG_PATH)["bank_id"]
    )
    url = (
        f"{HINDSIGHT_API_URL}/v1/default/banks/{quote(resolved_bank_id, safe='')}/operations/"
        f"{quote(operation_id, safe='')}"
    )
    request = Request(url, method="GET")
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            return {"status": "missing", "operation_id": operation_id}
        return {
            "status": "unavailable",
            "operation_id": operation_id,
            "http_status": int(exc.code),
        }
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        return {"status": "unavailable", "operation_id": operation_id}
    if (
        isinstance(payload, dict)
        and payload.get("status") == "not_found"
        and str(payload.get("operation_id") or "") == operation_id
    ):
        return {"status": "missing", "operation_id": operation_id}
    if not isinstance(payload, dict):
        return {"status": "unavailable", "operation_id": operation_id}
    result_metadata = payload.get("result_metadata")
    if not isinstance(result_metadata, dict):
        result_metadata = {}
    payload_operation_id = str(
        payload.get("id") or payload.get("operation_id") or ""
    )
    operation_type = str(
        payload.get("task_type") or payload.get("operation_type") or ""
    )
    document_id = payload.get("document_id") or result_metadata.get("document_id")
    items_count = payload.get("items_count")
    if items_count is None:
        items_count = result_metadata.get("items_count")
    unit_ids_count = result_metadata.get("unit_ids_count")
    extraction_errors_count = result_metadata.get("extraction_errors_count")
    if (
        payload_operation_id != operation_id
        or operation_type not in RETAIN_OPERATION_TYPES
    ):
        return {"status": "unavailable", "operation_id": operation_id}
    return {
        "status": "found",
        "operation_id": operation_id,
        "operation": {
            "id": operation_id,
            "task_type": operation_type,
            "status": str(payload.get("status") or ""),
            "document_id": document_id,
            "items_count": items_count,
            "unit_ids_count": unit_ids_count,
            "extraction_errors_count": extraction_errors_count,
        },
    }


def _candidate_counts(candidate_path: Path, session_id: str) -> dict[str, int] | None:
    try:
        candidate = json.loads(Path(candidate_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return _validate_candidate(candidate, session_id)["counts"]
    except (TypeError, ValueError):
        return None


def _success_candidate_material(
    succeeded: dict[str, Any], session_id: str
) -> dict[str, Any] | None:
    manifest_path = Path(str(succeeded.get("manifest_path") or ""))
    candidate_path = Path(str(succeeded.get("candidate_path") or ""))
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        manifest_session = (
            (manifest.get("session_id") or manifest.get("document_id"))
            if isinstance(manifest, dict)
            else None
        )
        if manifest_session != session_id:
            return None
        expected_turn_count = _strict_nonnegative_int(
            succeeded.get("candidate_turn_count"), "success receipt turn count"
        )
        expected_message_count = _strict_nonnegative_int(
            succeeded.get("candidate_message_count"), "success receipt message count"
        )
        expected_sha256 = str(succeeded.get("candidate_sha256") or "")
        if len(expected_sha256) != 64:
            return None
        return _validate_candidate(
            candidate,
            session_id,
            expected_sha256=expected_sha256,
            expected_turn_count=expected_turn_count,
            expected_message_count=expected_message_count,
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _candidate_gap_is_severe(state_snapshot: dict[str, Any], counts: dict[str, int]) -> bool:
    state_user = int(state_snapshot.get("active_user_count") or 0)
    state_assistant = int(state_snapshot.get("active_assistant_count") or 0)
    state_total = int(state_snapshot.get("active_message_count") or 0)
    candidate_total = counts["total"]
    if state_total >= 4 and candidate_total == 0:
        return True
    if state_user >= 2 and counts["user"] == 0:
        return True
    if state_assistant >= 2 and counts["assistant"] == 0:
        return True
    missing = state_total - candidate_total
    return missing >= 6 and candidate_total < state_total * 0.85


def _candidate_visible_event_gap(
    state_snapshot: dict[str, Any],
    counts: dict[str, int],
    audit: dict[str, Any],
) -> dict[str, int] | None:
    reconciliation = audit.get("state_reconciliation")
    if not isinstance(reconciliation, dict):
        return None
    try:
        state_user = _strict_nonnegative_int(
            int(state_snapshot.get("active_user_count") or 0), "state user count"
        )
        state_assistant = _strict_nonnegative_int(
            int(state_snapshot.get("active_assistant_count") or 0),
            "state assistant count",
        )
        clarify_questions = _strict_nonnegative_int(
            int(reconciliation.get("clarify_question_event_count") or 0),
            "clarify question count",
        )
        clarify_responses = _strict_nonnegative_int(
            int(reconciliation.get("clarify_response_event_count") or 0),
            "clarify response count",
        )
    except (TypeError, ValueError):
        return None
    required_user = state_user + clarify_responses
    required_assistant = state_assistant + clarify_questions
    missing_user = max(0, required_user - counts["user"])
    missing_assistant = max(0, required_assistant - counts["assistant"])
    if missing_user == 0 and missing_assistant == 0:
        return None
    return {
        "required_user_count": required_user,
        "required_assistant_count": required_assistant,
        "missing_user_count": missing_user,
        "missing_assistant_count": missing_assistant,
    }


def _confirmed_only_repair_scope_is_verified(
    started: dict[str, Any],
    succeeded: dict[str, Any],
    material: dict[str, Any],
) -> bool:
    if (
        started.get("repair_scope") != "confirmed_missing_only"
        or succeeded.get("repair_scope") != "confirmed_missing_only"
    ):
        return False
    repair = material.get("audit", {}).get("repair_scope")
    if not isinstance(repair, dict):
        return False
    base_sha256 = str(repair.get("base_remote_sha256") or "")
    if (
        repair.get("status") != "confirmed_missing_only_verified"
        or repair.get("old_messages_preserved_as_ordered_subsequence") is not True
        or len(base_sha256) != 64
        or any(character not in "0123456789abcdef" for character in base_sha256)
        or str(started.get("base_remote_sha256") or "") != base_sha256
        or material.get("sha256") == base_sha256
    ):
        return False
    try:
        old_message_count = _strict_nonnegative_int(
            repair.get("old_message_count"), "repair old message count"
        )
        inserted_message_count = _strict_nonnegative_int(
            repair.get("inserted_message_count"), "repair inserted message count"
        )
        _strict_nonnegative_int(
            repair.get("excluded_review_candidate_count"),
            "repair excluded review candidate count",
        )
    except ValueError:
        return False
    selected_occurrence_ids = repair.get("selected_occurrence_ids")
    if (
        inserted_message_count == 0
        or not isinstance(selected_occurrence_ids, list)
        or len(selected_occurrence_ids) != inserted_message_count
        or len({str(value) for value in selected_occurrence_ids})
        != inserted_message_count
        or any(not str(value).strip() for value in selected_occurrence_ids)
    ):
        return False
    return material.get("message_count") == old_message_count + inserted_message_count


def _state_reconciliation_is_verified(audit: dict[str, Any]) -> bool:
    reconciliation = audit.get("state_reconciliation")
    if not isinstance(reconciliation, dict) or reconciliation.get("status") != "verified":
        return False
    try:
        source_count = _strict_nonnegative_int(
            reconciliation.get("source_event_count"), "state source event count"
        )
        matched_count = _strict_nonnegative_int(
            reconciliation.get("matched_event_count"), "state matched event count"
        )
        added_count = _strict_nonnegative_int(
            reconciliation.get("added_event_count"), "state added event count"
        )
        uncovered_count = _strict_nonnegative_int(
            reconciliation.get("uncovered_event_count"), "state uncovered event count"
        )
    except ValueError:
        return False
    return uncovered_count == 0 and matched_count + added_count == source_count


def _document_content_and_counts(
    candidate_path: Path,
    session_id: str,
) -> tuple[str, dict[str, int]] | None:
    try:
        candidate = json.loads(Path(candidate_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(candidate, dict) or candidate.get("session_id") != session_id:
        return None
    content = candidate.get("document_content")
    if not isinstance(content, str):
        return None
    counts = _counts_from_document_content(content)
    if counts is None:
        return None
    return content, counts


def _counts_from_document_content(content: str) -> dict[str, int] | None:
    try:
        turns = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(turns, list):
        return None
    counts = {"user": 0, "assistant": 0}
    for turn in turns:
        if not isinstance(turn, list):
            return None
        for message in turn:
            if not isinstance(message, dict):
                return None
            role = str(message.get("role") or "")
            if role in counts and str(message.get("content") or "").strip():
                counts[role] += 1
    return {
        "user": counts["user"],
        "assistant": counts["assistant"],
        "total": counts["user"] + counts["assistant"],
    }


def _remote_gap_is_severe(candidate: dict[str, int], remote: dict[str, int]) -> bool:
    if candidate["user"] >= 2 and remote["user"] == 0:
        return True
    if candidate["assistant"] >= 2 and remote["assistant"] == 0:
        return True
    missing = candidate["total"] - remote["total"]
    return missing >= 6 and remote["total"] < candidate["total"] * 0.85


def scan_attempts(
    *,
    journal_path: Path = DEFAULT_JOURNAL_PATH,
    state_db_path: Path = DEFAULT_STATE_DB_PATH,
    now: datetime | None = None,
    grace_seconds: int = 300,
    document_fetcher: DocumentFetcher | None = None,
    operation_fetcher: OperationFetcher | None = None,
    operation_stall_seconds: int = 1800,
) -> dict[str, Any]:
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    events, torn_tail = _load_events(Path(journal_path))
    attempts: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        attempt_id = str(event.get("attempt_id") or "").strip()
        if not attempt_id:
            raise ValueError("retain attempt event has no attempt_id")
        attempts.setdefault(attempt_id, []).append(event)

    latest_remote_attempt_by_document: dict[str, tuple[datetime, int, str]] = {}
    for order, (attempt_id, attempt_events) in enumerate(attempts.items()):
        started = next(
            (event for event in attempt_events if event.get("event") == "started"),
            None,
        )
        remote_started = next(
            (
                event
                for event in reversed(attempt_events)
                if event.get("event") == "remote_write_started"
            ),
            None,
        )
        if (
            started is None
            or started.get("remote_expectation") != "expected"
            or remote_started is None
        ):
            continue
        document_id = str(started.get("document_id") or started.get("session_id") or "")
        try:
            remote_started_at = _parse_time(remote_started.get("recorded_at"))
        except (TypeError, ValueError):
            continue
        candidate_latest = (remote_started_at, order, attempt_id)
        if candidate_latest > latest_remote_attempt_by_document.get(
            document_id,
            (datetime.min.replace(tzinfo=timezone.utc), -1, ""),
        ):
            latest_remote_attempt_by_document[document_id] = candidate_latest

    alerts: list[dict[str, Any]] = []
    if torn_tail is not None:
        alerts.append(
            {
                "alert_key": "retain:journal:torn_tail",
                "type": "retain_journal_torn_tail",
                "severity": "high",
                "line_number": torn_tail["line_number"],
                "discarded_bytes": torn_tail["discarded_bytes"],
                "message": "Retain attempt journal 尾行不完整；已保留并继续扫描此前完整凭证",
            }
        )
    remote_confirmed_attempts: list[dict[str, Any]] = []
    remote_superseded_attempts: list[dict[str, Any]] = []
    for attempt_id, attempt_events in attempts.items():
        scheduled = next(
            (event for event in attempt_events if event.get("event") == "scheduled"),
            None,
        )
        started = next(
            (event for event in attempt_events if event.get("event") == "started"),
            None,
        )
        if started is None and scheduled is not None:
            due_at = _parse_time(scheduled.get("due_at"))
            overdue_seconds = (observed_at - due_at).total_seconds()
            if overdue_seconds > grace_seconds:
                session_id = str(scheduled.get("session_id") or "").strip()
                document_id = str(
                    scheduled.get("document_id") or session_id
                ).strip()
                alerts.append(
                    {
                        "alert_key": f"retain:{attempt_id}:scheduled_worker_missing",
                        "type": "retain_scheduled_worker_missing",
                        "severity": "high",
                        "attempt_id": attempt_id,
                        "session_id": session_id,
                        "document_id": document_id,
                        "scheduled_at": _parse_time(
                            scheduled.get("recorded_at")
                        ).isoformat(),
                        "due_at": due_at.isoformat(),
                        "state_session_found": _session_exists(
                            Path(state_db_path), session_id
                        ),
                        "message": "Retain 已到期，但非持久延迟子进程没有开始提取",
                    }
                )
            continue
        if started is None:
            continue
        terminal = any(
            event.get("event") in {"export_succeeded", "export_failed"}
            for event in attempt_events
        )
        started_at = _parse_time(started.get("recorded_at"))
        age_seconds = (observed_at - started_at).total_seconds()
        if not terminal and age_seconds <= grace_seconds:
            continue
        session_id = str(started.get("session_id") or "").strip()
        document_id = str(started.get("document_id") or session_id).strip()
        state_session_found = _session_exists(Path(state_db_path), session_id)
        state_snapshot = started.get("state_snapshot")
        if (
            state_session_found
            and isinstance(state_snapshot, dict)
            and not state_snapshot.get("session_found")
        ):
            cutoff_value = started.get("cutoff_at")
            if cutoff_value is None and scheduled is not None:
                cutoff_value = scheduled.get("cutoff_at")
            try:
                cutoff_at = _parse_time(cutoff_value)
            except (TypeError, ValueError):
                cutoff_at = None
            if cutoff_at is not None:
                recovered_snapshot = _state_snapshot(
                    Path(state_db_path),
                    session_id,
                    cutoff_at=cutoff_at,
                )
                if recovered_snapshot.get("session_found"):
                    state_snapshot = recovered_snapshot
        if not terminal:
            alerts.append(
                {
                    "alert_key": f"retain:{attempt_id}:attempt_incomplete",
                    "type": "retain_attempt_incomplete",
                    "severity": "high",
                    "attempt_id": attempt_id,
                    "session_id": session_id,
                    "document_id": document_id,
                    "started_at": started_at.isoformat(),
                    "state_session_found": state_session_found,
                    "message": "Retain 已开始，但宽限期后仍没有本地完成或失败记录",
                }
            )
            continue
        failed = next(
            (
                event
                for event in reversed(attempt_events)
                if event.get("event") == "export_failed"
            ),
            None,
        )
        if failed is not None:
            alerts.append(
                {
                    "alert_key": f"retain:{attempt_id}:export_failed",
                    "type": "retain_export_failed",
                    "severity": "high",
                    "attempt_id": attempt_id,
                    "session_id": session_id,
                    "document_id": document_id,
                    "started_at": started_at.isoformat(),
                    "state_session_found": state_session_found,
                    "failure_type": str(failed.get("failure_type") or "unknown"),
                    "message": "Retain 本地候选生成明确失败",
                }
            )
            continue
        succeeded = next(
            (
                event
                for event in reversed(attempt_events)
                if event.get("event") == "export_succeeded"
            ),
            None,
        )
        candidate_material = (
            _success_candidate_material(succeeded, session_id)
            if succeeded is not None
            else None
        )
        if succeeded is not None and candidate_material is None:
            alerts.append(
                {
                    "alert_key": f"retain:{attempt_id}:artifacts_invalid",
                    "type": "retain_export_artifacts_invalid",
                    "severity": "high",
                    "attempt_id": attempt_id,
                    "session_id": session_id,
                    "document_id": document_id,
                    "started_at": started_at.isoformat(),
                    "state_session_found": state_session_found,
                    "message": "Retain 记录为本地成功，但 manifest 或候选产物缺失、损坏或会话不匹配",
                }
            )
            continue
        repair_scope_requested = (
            started.get("repair_scope") == "confirmed_missing_only"
            or (
                succeeded is not None
                and succeeded.get("repair_scope") == "confirmed_missing_only"
            )
        )
        repair_scope_verified = (
            succeeded is not None
            and candidate_material is not None
            and _confirmed_only_repair_scope_is_verified(
                started, succeeded, candidate_material
            )
        )
        if repair_scope_requested and not repair_scope_verified:
            alerts.append(
                {
                    "alert_key": f"retain:{attempt_id}:repair_scope_invalid",
                    "type": "retain_repair_scope_invalid",
                    "severity": "high",
                    "attempt_id": attempt_id,
                    "session_id": session_id,
                    "document_id": document_id,
                    "started_at": started_at.isoformat(),
                    "state_session_found": state_session_found,
                    "message": "Retain 历史修复候选缺少可验证的确认缺项范围凭证",
                }
            )
            continue
        if succeeded is not None and not state_session_found:
            alerts.append(
                {
                    "alert_key": f"retain:{attempt_id}:state_session_missing",
                    "type": "retain_state_session_missing",
                    "severity": "medium",
                    "attempt_id": attempt_id,
                    "session_id": session_id,
                    "document_id": document_id,
                    "started_at": started_at.isoformat(),
                    "state_session_found": False,
                    "message": "Retain 有本地记录，但 StateDB 中找不到对应会话，无法完成独立内容交叉验证",
                }
            )
        unverified_block = next(
            (
                event
                for event in reversed(attempt_events)
                if event.get("event") == "remote_write_blocked_unverified_candidate"
            ),
            None,
        )
        if unverified_block is not None:
            reconciliation_status = str(
                unverified_block.get("state_reconciliation_status") or "unknown"
            )
            alerts.append(
                {
                    "alert_key": (
                        f"retain:{attempt_id}:candidate_reconciliation_unverified"
                    ),
                    "type": "retain_candidate_reconciliation_unverified",
                    "severity": "high",
                    "attempt_id": attempt_id,
                    "session_id": session_id,
                    "document_id": document_id,
                    "started_at": started_at.isoformat(),
                    "state_session_found": state_session_found,
                    "state_reconciliation_status": reconciliation_status,
                    "remote_write_status": "blocked_before_submit",
                    "message": "Retain 候选未完成 StateDB 可见事件对账，已在提交远端前拦截",
                }
            )
            continue
        visible_event_gap_block = next(
            (
                event
                for event in reversed(attempt_events)
                if event.get("event") == "remote_write_blocked_visible_event_gap"
            ),
            None,
        )
        if visible_event_gap_block is not None:
            live_gap = None
            cutoff_value = started.get("cutoff_at")
            if cutoff_value is None and scheduled is not None:
                cutoff_value = scheduled.get("cutoff_at")
            try:
                gap_cutoff = _parse_time(cutoff_value)
            except (TypeError, ValueError):
                gap_cutoff = None
            if (
                candidate_material is not None
                and gap_cutoff is not None
                and state_session_found
            ):
                live_snapshot = _state_snapshot(
                    Path(state_db_path),
                    session_id,
                    cutoff_at=gap_cutoff,
                )
                if live_snapshot.get("session_found"):
                    live_gap = _candidate_visible_event_gap(
                        live_snapshot,
                        candidate_material["counts"],
                        candidate_material.get("audit") or {},
                    )
                    if live_gap is None:
                        continue
            gap = live_gap or {
                "required_user_count": int(
                    visible_event_gap_block.get("required_user_count") or 0
                ),
                "required_assistant_count": int(
                    visible_event_gap_block.get("required_assistant_count") or 0
                ),
                "missing_user_count": int(
                    visible_event_gap_block.get("missing_user_count") or 0
                ),
                "missing_assistant_count": int(
                    visible_event_gap_block.get("missing_assistant_count") or 0
                ),
            }
            alerts.append(
                {
                    "alert_key": f"retain:{attempt_id}:candidate_visible_event_gap",
                    "type": "retain_candidate_visible_event_gap",
                    "severity": "high",
                    "attempt_id": attempt_id,
                    "session_id": session_id,
                    "document_id": document_id,
                    "started_at": started_at.isoformat(),
                    "state_session_found": state_session_found,
                    "required_user_count": gap["required_user_count"],
                    "required_assistant_count": gap["required_assistant_count"],
                    "candidate_user_count": int(
                        visible_event_gap_block.get("candidate_user_count") or 0
                    ),
                    "candidate_assistant_count": int(
                        visible_event_gap_block.get("candidate_assistant_count") or 0
                    ),
                    "missing_user_count": gap["missing_user_count"],
                    "missing_assistant_count": gap["missing_assistant_count"],
                    "remote_write_status": "blocked_before_submit",
                    "message": "Retain 候选少了可见用户或 AI 对话，已在提交远端前拦截",
                }
            )
            continue
        candidate_gap_alert: dict[str, Any] | None = None
        if (
            succeeded is not None
            and isinstance(state_snapshot, dict)
            and not repair_scope_verified
        ):
            counts = candidate_material["counts"] if candidate_material is not None else None
            if counts is not None and _candidate_gap_is_severe(state_snapshot, counts):
                state_total = int(state_snapshot.get("active_message_count") or 0)
                candidate_total = counts["total"]
                candidate_gap_alert = {
                    "alert_key": f"retain:{attempt_id}:candidate_severely_incomplete",
                    "type": "retain_candidate_severely_incomplete",
                    "severity": "high",
                    "attempt_id": attempt_id,
                    "session_id": session_id,
                    "document_id": document_id,
                    "started_at": started_at.isoformat(),
                    "state_session_found": state_session_found,
                    "state_active_message_count": state_total,
                    "candidate_message_count": candidate_total,
                    "missing_message_count": max(0, state_total - candidate_total),
                    "message": "Retain 候选相对开始时的 StateDB 会话少了一大块有效用户或 AI 消息",
                }
                alerts.append(candidate_gap_alert)
        remote_write_blocked = next(
            (
                event
                for event in reversed(attempt_events)
                if event.get("event")
                == "remote_write_blocked_incomplete_candidate"
            ),
            None,
        )
        if remote_write_blocked is not None:
            if candidate_gap_alert is not None:
                candidate_gap_alert["remote_write_status"] = "blocked_before_submit"
            continue
        if (
            succeeded is not None
            and started.get("remote_expectation") == "expected"
        ):
            remote_write_rejected = next(
                (
                    event
                    for event in reversed(attempt_events)
                    if event.get("event")
                    in {"remote_write_rejected", "remote_write_failed"}
                ),
                None,
            )
            if remote_write_rejected is not None:
                legacy_failed = remote_write_rejected.get("event") == "remote_write_failed"
                alerts.append(
                    {
                        "alert_key": (
                            f"retain:{attempt_id}:remote_write_failed"
                            if legacy_failed
                            else f"retain:{attempt_id}:remote_write_rejected"
                        ),
                        "type": (
                            "retain_remote_write_failed"
                            if legacy_failed
                            else "retain_remote_write_rejected"
                        ),
                        "severity": "high",
                        "attempt_id": attempt_id,
                        "session_id": session_id,
                        "document_id": document_id,
                        "operation_id": str(
                            remote_write_rejected.get("operation_id") or attempt_id
                        ),
                        "started_at": started_at.isoformat(),
                        "state_session_found": state_session_found,
                        "failure_type": str(
                            remote_write_rejected.get("failure_type") or "unknown"
                        ),
                        "message": (
                            "Retain 本地候选已完成，但 Hindsight 写入请求明确失败"
                            if legacy_failed
                            else "Retain 本地候选已完成，但 Hindsight 明确拒绝了写入请求"
                        ),
                    }
                )
                continue
        if (
            succeeded is not None
            and started.get("remote_expectation") == "expected"
            and age_seconds > grace_seconds
        ):
            remote_started_event = next(
                (
                    event
                    for event in reversed(attempt_events)
                    if event.get("event") == "remote_write_started"
                ),
                None,
            )
            if remote_started_event is None:
                alerts.append(
                    {
                        "alert_key": f"retain:{attempt_id}:remote_write_not_started",
                        "type": "retain_remote_write_not_started",
                        "severity": "high",
                        "attempt_id": attempt_id,
                        "session_id": session_id,
                        "document_id": document_id,
                        "started_at": started_at.isoformat(),
                        "state_session_found": state_session_found,
                        "message": "Retain 本地候选已完成，但宽限期后仍没有 Hindsight 写入开始凭证",
                    }
                )
                continue
            completed_operation_id: str | None = None
            remote_operation_event = next(
                (
                    event
                    for event in reversed(attempt_events)
                    if event.get("event") == "remote_write_accepted"
                ),
                None,
            )
            if remote_operation_event is None:
                remote_operation_event = next(
                    (
                        event
                        for event in reversed(attempt_events)
                        if event.get("event") == "remote_write_started"
                    ),
                    None,
                )
            if remote_operation_event is not None:
                operation_id = str(
                    remote_operation_event.get("operation_id") or attempt_id
                )
                operation_result = (
                    operation_fetcher or fetch_hindsight_operation
                )(operation_id)
                if operation_result.get("status") == "unavailable":
                    alerts.append(
                        {
                            "alert_key": f"retain:{attempt_id}:remote_operation_unavailable",
                            "type": "retain_remote_operation_unavailable",
                            "severity": "medium",
                            "attempt_id": attempt_id,
                            "session_id": session_id,
                            "document_id": document_id,
                            "operation_id": operation_id,
                            "started_at": started_at.isoformat(),
                            "state_session_found": state_session_found,
                            "message": "Hindsight operation 暂时不可达，当前无法确认本次 Retain 处理状态",
                        }
                    )
                    continue
                if operation_result.get("status") == "missing":
                    alerts.append(
                        {
                            "alert_key": f"retain:{attempt_id}:remote_operation_missing",
                            "type": "retain_remote_operation_missing",
                            "severity": "medium",
                            "attempt_id": attempt_id,
                            "session_id": session_id,
                            "document_id": document_id,
                            "operation_id": operation_id,
                            "started_at": started_at.isoformat(),
                            "state_session_found": state_session_found,
                            "message": "Hindsight 中找不到本次 deterministic operation，当前结果未解决",
                        }
                    )
                    continue
                if operation_result.get("status") == "found":
                    operation = operation_result.get("operation")
                    if not isinstance(operation, dict):
                        operation = {}
                    if (
                        str(operation.get("id") or "") != operation_id
                        or str(operation.get("task_type") or "")
                        not in RETAIN_OPERATION_TYPES
                        or str(operation.get("document_id") or "") != document_id
                    ):
                        alerts.append(
                            {
                                "alert_key": f"retain:{attempt_id}:remote_operation_identity_mismatch",
                                "type": "retain_remote_operation_identity_mismatch",
                                "severity": "high",
                                "attempt_id": attempt_id,
                                "session_id": session_id,
                                "document_id": document_id,
                                "operation_id": operation_id,
                                "started_at": started_at.isoformat(),
                                "state_session_found": state_session_found,
                                "message": "Hindsight operation 与本次 Retain 的任务类型或 Document 身份不一致",
                            }
                        )
                        continue
                    operation_status = str(operation.get("status") or "")
                    if operation_status in {"pending", "processing"}:
                        if age_seconds > operation_stall_seconds:
                            alerts.append(
                                {
                                    "alert_key": f"retain:{attempt_id}:remote_operation_stalled",
                                    "type": "retain_remote_operation_stalled",
                                    "severity": "medium",
                                    "attempt_id": attempt_id,
                                    "session_id": session_id,
                                    "document_id": document_id,
                                    "operation_id": operation_id,
                                    "operation_status": operation_status,
                                    "started_at": started_at.isoformat(),
                                    "state_session_found": state_session_found,
                                    "message": "Hindsight 已接受 Retain，但 operation 长时间仍未完成",
                                }
                            )
                        continue
                    if operation_status in {"failed", "cancelled"}:
                        alerts.append(
                            {
                                "alert_key": f"retain:{attempt_id}:remote_operation_failed",
                                "type": "retain_remote_operation_failed",
                                "severity": "high",
                                "attempt_id": attempt_id,
                                "session_id": session_id,
                                "document_id": document_id,
                                "operation_id": operation_id,
                                "operation_status": operation_status,
                                "started_at": started_at.isoformat(),
                                "state_session_found": state_session_found,
                                "message": "Hindsight 已接受 Retain，但远端 operation 明确失败或取消",
                            }
                        )
                        continue
                    if operation_status == "completed":
                        extraction_errors_raw = operation.get(
                            "extraction_errors_count"
                        )
                        if (
                            isinstance(extraction_errors_raw, bool)
                            or not isinstance(extraction_errors_raw, int)
                            or extraction_errors_raw < 0
                        ):
                            alerts.append(
                                {
                                    "alert_key": f"retain:{attempt_id}:remote_operation_metadata_unavailable",
                                    "type": "retain_remote_operation_metadata_unavailable",
                                    "severity": "medium",
                                    "attempt_id": attempt_id,
                                    "session_id": session_id,
                                    "document_id": document_id,
                                    "operation_id": operation_id,
                                    "started_at": started_at.isoformat(),
                                    "state_session_found": state_session_found,
                                    "message": "Hindsight operation 已完成，但 extraction error 计数缺失或无效",
                                }
                            )
                            continue
                        extraction_errors_count = extraction_errors_raw
                    else:
                        extraction_errors_count = None
                    if operation_status == "completed" and extraction_errors_count > 0:
                        alerts.append(
                            {
                                "alert_key": f"retain:{attempt_id}:remote_operation_extraction_errors",
                                "type": "retain_remote_operation_extraction_errors",
                                "severity": "high",
                                "attempt_id": attempt_id,
                                "session_id": session_id,
                                "document_id": document_id,
                                "operation_id": operation_id,
                                "extraction_errors_count": extraction_errors_count,
                                "started_at": started_at.isoformat(),
                                "state_session_found": state_session_found,
                                "message": "Hindsight operation 已结束，但 fact extraction 存在明确错误",
                            }
                        )
                        continue
                    if operation_status == "completed":
                        completed_operation_id = operation_id
                    else:
                        alerts.append(
                            {
                                "alert_key": f"retain:{attempt_id}:remote_operation_unknown",
                                "type": "retain_remote_operation_unavailable",
                                "severity": "medium",
                                "attempt_id": attempt_id,
                                "session_id": session_id,
                                "document_id": document_id,
                                "operation_id": operation_id,
                                "operation_status": operation_status or "unknown",
                                "started_at": started_at.isoformat(),
                                "state_session_found": state_session_found,
                                "message": "Hindsight operation 返回未知状态，当前无法确认本次 Retain",
                            }
                        )
                        continue
            latest_remote_attempt = latest_remote_attempt_by_document.get(document_id)
            if (
                completed_operation_id is not None
                and latest_remote_attempt is not None
                and latest_remote_attempt[2] != attempt_id
            ):
                remote_superseded_attempts.append(
                    {
                        "attempt_id": attempt_id,
                        "session_id": session_id,
                        "document_id": document_id,
                        "operation_id": completed_operation_id,
                        "superseded_by_attempt_id": latest_remote_attempt[2],
                    }
                )
                continue
            remote = (document_fetcher or fetch_hindsight_document)(document_id)
            if remote.get("status") == "missing":
                alerts.append(
                    {
                        "alert_key": f"retain:{attempt_id}:remote_document_missing",
                        "type": "retain_expected_remote_document_missing",
                        "severity": "high",
                        "attempt_id": attempt_id,
                        "session_id": session_id,
                        "document_id": document_id,
                        "started_at": started_at.isoformat(),
                        "state_session_found": state_session_found,
                        "message": "Retain 本地候选已完成，但宽限期后 Hindsight 中没有对应 Document",
                    }
                )
            elif remote.get("status") == "unavailable":
                alerts.append(
                    {
                        "alert_key": f"retain:{attempt_id}:remote_check_unavailable",
                        "type": "retain_remote_check_unavailable",
                        "severity": "medium",
                        "attempt_id": attempt_id,
                        "session_id": session_id,
                        "document_id": document_id,
                        "started_at": started_at.isoformat(),
                        "state_session_found": state_session_found,
                        "message": "Hindsight 暂时不可达，当前无法确认本次 Retain 是否已保存",
                    }
                )
            elif remote.get("status") == "identity_mismatch":
                alerts.append(
                    {
                        "alert_key": f"retain:{attempt_id}:remote_document_identity_mismatch",
                        "type": "retain_remote_document_identity_mismatch",
                        "severity": "high",
                        "attempt_id": attempt_id,
                        "session_id": session_id,
                        "document_id": document_id,
                        "started_at": started_at.isoformat(),
                        "state_session_found": state_session_found,
                        "message": "Hindsight Document 响应身份与本次 Retain 不一致",
                    }
                )
            elif remote.get("status") == "found":
                document = remote.get("document")
                if not isinstance(document, dict):
                    document = {}
                actual_document_id = str(
                    document.get("id") or document.get("document_id") or ""
                )
                if actual_document_id != document_id:
                    alerts.append(
                        {
                            "alert_key": f"retain:{attempt_id}:remote_document_identity_mismatch",
                            "type": "retain_remote_document_identity_mismatch",
                            "severity": "high",
                            "attempt_id": attempt_id,
                            "session_id": session_id,
                            "document_id": document_id,
                            "started_at": started_at.isoformat(),
                            "state_session_found": state_session_found,
                            "message": "Hindsight Document 响应身份与本次 Retain 不一致",
                        }
                    )
                    continue
                remote_content = document.get("original_text")
                if not isinstance(remote_content, str):
                    payload = document.get("payload")
                    remote_content = (
                        payload.get("original_text")
                        if isinstance(payload, dict)
                        else None
                    )
                remote_counts = (
                    _counts_from_document_content(remote_content)
                    if isinstance(remote_content, str)
                    else None
                )
                if (
                    candidate_material is not None
                    and remote_counts is not None
                    and candidate_material["content"] != remote_content
                    and _remote_gap_is_severe(candidate_material["counts"], remote_counts)
                ):
                    candidate_total = candidate_material["counts"]["total"]
                    remote_total = remote_counts["total"]
                    alerts.append(
                        {
                            "alert_key": f"retain:{attempt_id}:remote_document_severely_incomplete",
                            "type": "retain_remote_document_severely_incomplete",
                            "severity": "high",
                            "attempt_id": attempt_id,
                            "session_id": session_id,
                            "document_id": document_id,
                            "started_at": started_at.isoformat(),
                            "state_session_found": state_session_found,
                            "candidate_message_count": candidate_total,
                            "remote_message_count": remote_total,
                            "missing_message_count": max(0, candidate_total - remote_total),
                            "message": "Hindsight Document 存在，但相对本次 Retain 候选少了一大块内容",
                        }
                    )
                elif (
                    candidate_material is not None
                    and isinstance(remote_content, str)
                    and candidate_material["content"] == remote_content
                    and completed_operation_id is not None
                ):
                    if candidate_gap_alert is not None:
                        candidate_gap_alert.update(
                            {
                                "remote_write_status": "completed_exact_candidate",
                                "operation_id": completed_operation_id,
                                "remote_document_matches_candidate": True,
                            }
                        )
                    remote_confirmed_attempts.append(
                        {
                            "attempt_id": attempt_id,
                            "session_id": session_id,
                            "document_id": document_id,
                            "operation_id": completed_operation_id,
                            "candidate_sha256": str(
                                candidate_material["sha256"]
                            ),
                        }
                    )
                elif candidate_material is not None:
                    candidate_content = candidate_material["content"]
                    remote_text = remote_content if isinstance(remote_content, str) else ""
                    alerts.append(
                        {
                            "alert_key": f"retain:{attempt_id}:remote_document_content_mismatch",
                            "type": "retain_remote_document_content_mismatch",
                            "severity": "medium",
                            "attempt_id": attempt_id,
                            "session_id": session_id,
                            "document_id": document_id,
                            "operation_id": completed_operation_id,
                            "started_at": started_at.isoformat(),
                            "state_session_found": state_session_found,
                            "candidate_sha256": hashlib.sha256(
                                candidate_content.encode("utf-8")
                            ).hexdigest(),
                            "remote_sha256": hashlib.sha256(
                                remote_text.encode("utf-8")
                            ).hexdigest(),
                            "message": "Hindsight Document 存在，但正文与本次 Retain 候选不完全一致",
                        }
                    )

    return {
        "status": "ok",
        "journal_torn_tail": torn_tail is not None,
        "attempt_count": len(attempts),
        "remote_confirmed_count": len(remote_confirmed_attempts),
        "remote_confirmed_attempts": remote_confirmed_attempts,
        "remote_superseded_count": len(remote_superseded_attempts),
        "remote_superseded_attempts": remote_superseded_attempts,
        "alerts": alerts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crash-durable Retain integrity monitor")
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan_parser = subparsers.add_parser("scan", help="scan durable Retain attempt receipts")
    scan_parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL_PATH)
    scan_parser.add_argument("--state-db", type=Path, default=DEFAULT_STATE_DB_PATH)
    scan_parser.add_argument("--grace-seconds", type=int, default=300)
    schedule_parser = subparsers.add_parser(
        "schedule",
        help="record one Retain request and start a non-durable delayed worker",
    )
    schedule_parser.add_argument("--session-id", required=True)
    schedule_parser.add_argument("--output-root", type=Path, required=True)
    schedule_parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL_PATH)
    schedule_parser.add_argument("--state-db", type=Path, default=DEFAULT_STATE_DB_PATH)
    schedule_parser.add_argument(
        "--export-script", type=Path, default=DEFAULT_EXPORT_SCRIPT
    )
    schedule_parser.add_argument(
        "--hindsight-config", type=Path, default=DEFAULT_HINDSIGHT_CONFIG_PATH
    )
    schedule_parser.add_argument("--delay-seconds", type=int, default=1200)
    schedule_parser.add_argument(
        "--output-format",
        choices=("json", "text"),
        default="json",
    )
    schedule_parser.add_argument(
        "--remote-expectation",
        choices=sorted(REMOTE_EXPECTATIONS),
        default="expected",
    )
    execute_parser = subparsers.add_parser(
        "execute-scheduled",
        help="wait until due_at and execute one scheduled Retain",
    )
    execute_parser.add_argument("--session-id", required=True)
    execute_parser.add_argument("--attempt-id", required=True)
    execute_parser.add_argument("--cutoff-at", type=_parse_time, required=True)
    execute_parser.add_argument("--due-at", type=_parse_time, required=True)
    execute_parser.add_argument("--output-root", type=Path, required=True)
    execute_parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL_PATH)
    execute_parser.add_argument("--state-db", type=Path, default=DEFAULT_STATE_DB_PATH)
    execute_parser.add_argument(
        "--export-script", type=Path, default=DEFAULT_EXPORT_SCRIPT
    )
    execute_parser.add_argument(
        "--hindsight-config", type=Path, default=DEFAULT_HINDSIGHT_CONFIG_PATH
    )
    execute_parser.add_argument(
        "--remote-expectation",
        choices=sorted(REMOTE_EXPECTATIONS),
        default="expected",
    )
    export_parser = subparsers.add_parser(
        "export",
        help="record one attempt and run the read-only Langfuse exporter",
    )
    export_parser.add_argument("--session-id", required=True)
    export_parser.add_argument("--output-root", type=Path, required=True)
    export_parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL_PATH)
    export_parser.add_argument("--state-db", type=Path, default=DEFAULT_STATE_DB_PATH)
    export_parser.add_argument("--export-script", type=Path, default=DEFAULT_EXPORT_SCRIPT)
    export_parser.add_argument(
        "--remote-expectation",
        choices=sorted(REMOTE_EXPECTATIONS),
        default="not_expected_export_only",
    )
    args = parser.parse_args(argv)

    try:
        if args.command == "scan":
            result = scan_attempts(
                journal_path=args.journal,
                state_db_path=args.state_db,
                grace_seconds=args.grace_seconds,
            )
        elif args.command == "schedule":
            result = schedule_retain(
                session_id=args.session_id,
                output_root=args.output_root,
                journal_path=args.journal,
                state_db_path=args.state_db,
                export_script=args.export_script,
                python_executable=sys.executable,
                remote_expectation=args.remote_expectation,
                delay_seconds=args.delay_seconds,
                hindsight_config_path=args.hindsight_config,
            )
        elif args.command == "execute-scheduled":
            result = execute_scheduled_retain(
                session_id=args.session_id,
                output_root=args.output_root,
                journal_path=args.journal,
                state_db_path=args.state_db,
                export_script=args.export_script,
                python_executable=sys.executable,
                remote_expectation=args.remote_expectation,
                attempt_id=args.attempt_id,
                cutoff_at=args.cutoff_at,
                due_at=args.due_at,
                hindsight_config_path=args.hindsight_config,
            )
        elif args.command == "export":
            result = {
                "status": "ok",
                **run_export(
                    session_id=args.session_id,
                    output_root=args.output_root,
                    journal_path=args.journal,
                    state_db_path=args.state_db,
                    export_script=args.export_script,
                    python_executable=sys.executable,
                    remote_expectation=args.remote_expectation,
                ),
            }
        else:
            raise ValueError("unsupported command")
    except Exception as exc:
        if args.command == "schedule" and args.output_format == "text":
            print(f"Retain 排期失败（{type(exc).__name__}）。")
        else:
            print(
                json.dumps(
                    {"status": "failed", "error_type": type(exc).__name__},
                    ensure_ascii=False,
                )
            )
        return 1
    if args.command == "schedule" and args.output_format == "text":
        delay_seconds = int(result.get("delay_seconds", args.delay_seconds))
        delay_text = (
            f"{delay_seconds // 60} 分钟"
            if delay_seconds % 60 == 0
            else f"{delay_seconds} 秒"
        )
        print(f"Retain 已排期，将在 {delay_text}后保存本次会话。")
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
