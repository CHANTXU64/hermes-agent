#!/usr/bin/env python3
"""Read-only Langfuse/Hindsight export and comparison for one Hermes session."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

try:
    from langfuse import Langfuse
except ImportError:  # pragma: no cover - runtime dependency check
    Langfuse = None


# 安全边界：这两个地址是固定字面量，不允许由环境变量决定。
LANGFUSE_BASE_URL = "https://langfuse.chantx.top"
HINDSIGHT_BASE_URL = "https://hindsight-api.chantx.top"
DEFAULT_ENV_FILE = Path.home() / ".hermes" / ".env"
DEFAULT_HINDSIGHT_CONFIG_PATH = (
    Path.home() / ".hermes" / "hindsight" / "config.json"
)
DEFAULT_SQLITE_PATH = (
    Path.home() / ".hermes" / "hindsight" / "retain_turns.sqlite3"
)
DEFAULT_STATE_DB_PATH = Path.home() / ".hermes" / "state.db"
_COMPACTION_MARKER_PREFIX = "[CONTEXT COMPACTION —"
_STATE_FRAMEWORK_PREFIXES = (
    "[Session Arc Summary ",
    "[Your active task list was preserved across context compression]",
    "[Current user objective preserved from compacted history]",
    "[Recent Summary (",
    _COMPACTION_MARKER_PREFIX,
    "[Durable Summary (",
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


@contextmanager
def sqlite_query_only(path: Path):
    connection = sqlite3.connect(f"{path.expanduser().resolve().as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


def safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def load_dotenv(path: Path) -> dict[str, str]:
    """Read only simple KEY=VALUE entries; never print the values."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def jsonable(value):
    """Convert Langfuse SDK models to JSON without exposing credentials."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if hasattr(value, "model_dump"):
        try:
            return jsonable(value.model_dump(mode="json"))
        except TypeError:
            return jsonable(value.model_dump())
    if hasattr(value, "dict"):
        return jsonable(value.dict())
    if hasattr(value, "__dict__"):
        return {
            str(k): jsonable(v)
            for k, v in vars(value).items()
            if not str(k).startswith("_")
        }
    return str(value)


def write_json(path: Path, payload) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def get_langfuse_credentials(env_file: Path) -> tuple[str, str]:
    values = load_dotenv(env_file)
    public_key = values.get("HERMES_LANGFUSE_PUBLIC_KEY") or values.get(
        "LANGFUSE_PUBLIC_KEY"
    )
    secret_key = values.get("HERMES_LANGFUSE_SECRET_KEY") or values.get(
        "LANGFUSE_SECRET_KEY"
    )
    if not public_key or not secret_key:
        raise SystemExit(
            f"{env_file} 中缺少 HERMES_LANGFUSE_PUBLIC_KEY / "
            "HERMES_LANGFUSE_SECRET_KEY（或不带 HERMES_ 前缀的通用名称）；"
            "脚本不会从环境变量拼接目标地址。"
        )
    return public_key, secret_key


def load_hindsight_bank_id(config_path: Path) -> str:
    try:
        payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Hindsight config") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid Hindsight config")
    bank_id = payload.get("bank_id")
    if not isinstance(bank_id, str):
        raise ValueError("invalid Hindsight bank_id")
    bank_id = bank_id.strip()
    if not bank_id or any(
        character
        not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
        for character in bank_id
    ):
        raise ValueError("invalid Hindsight bank_id")
    return bank_id


V4_OBSERVATIONS_PAGE_SIZE = 100
V4_OBSERVATIONS_MAX_PAGES = 10_000
V4_OBSERVATION_FIELDS = "core,basic,time,io,metadata,trace_context"
V4_EXPANDED_METADATA_KEYS = "task_id,turn_id,capture_mode"


def _decode_v4_io(value):
    """Decode the raw JSON strings returned by the v4 Observations API."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _fetch_session_observations(client, session_id: str) -> list[dict]:
    session_filter = json.dumps(
        [
            {
                "type": "string",
                "column": "sessionId",
                "operator": "=",
                "value": session_id,
            }
        ],
        separators=(",", ":"),
    )
    observations: list[dict] = []
    observation_ids: set[str] = set()
    seen_cursors: set[str] = set()
    cursor = None

    for _ in range(V4_OBSERVATIONS_MAX_PAGES):
        response = client.api.observations.get_many(
            fields=V4_OBSERVATION_FIELDS,
            expand_metadata=V4_EXPANDED_METADATA_KEYS,
            limit=V4_OBSERVATIONS_PAGE_SIZE,
            cursor=cursor,
            filter=session_filter,
        )
        payload = jsonable(response)
        if not isinstance(payload, dict):
            raise RuntimeError("Langfuse v4 observations response has invalid data")
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise RuntimeError("Langfuse v4 observations response has invalid data")

        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError("Langfuse v4 observation row is not an object")
            observation_id = row.get("id")
            trace_id = row.get("traceId")
            returned_session_id = row.get("sessionId")
            if not isinstance(observation_id, str) or not observation_id.strip():
                raise RuntimeError("Langfuse v4 observation has invalid ID")
            if observation_id in observation_ids:
                raise RuntimeError(
                    f"Langfuse v4 observations contain duplicate ID {observation_id}"
                )
            if not isinstance(trace_id, str) or not trace_id.strip():
                raise RuntimeError(
                    f"Langfuse v4 observation {observation_id} has invalid trace ID"
                )
            if returned_session_id != session_id:
                raise RuntimeError(
                    f"Langfuse v4 observation {observation_id} crossed session boundary"
                )
            observation_ids.add(observation_id)
            row["input"] = _decode_v4_io(row.get("input"))
            row["output"] = _decode_v4_io(row.get("output"))
            observations.append(row)

        metadata = payload.get("meta") or {}
        if not isinstance(metadata, dict):
            raise RuntimeError("Langfuse v4 observations response has invalid metadata")
        next_cursor = metadata.get("cursor")
        if next_cursor in (None, ""):
            break
        if not isinstance(next_cursor, str):
            raise RuntimeError("Langfuse v4 observations response has invalid cursor")
        if next_cursor in seen_cursors:
            raise RuntimeError("Langfuse v4 observations cursor repeated")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise RuntimeError(
            f"Langfuse v4 observations exceeded {V4_OBSERVATIONS_MAX_PAGES} pages"
        )

    return observations


def _session_observations_to_traces(
    observations: list[dict], session_id: str
) -> list[dict]:
    observations_by_trace: dict[str, list[dict]] = {}
    for observation in observations:
        trace_id = str(observation["traceId"])
        observations_by_trace.setdefault(trace_id, []).append(observation)

    traces = []
    for trace_id, trace_observations in observations_by_trace.items():
        ordered = sorted(
            trace_observations,
            key=lambda item: (
                str(item.get("startTime") or ""),
                str(item.get("id") or ""),
            ),
        )
        logical_roots = [
            observation
            for observation in ordered
            if observation.get("isRootObservation") is True
        ]
        root = logical_roots[0] if logical_roots else ordered[0]
        capture_modes = {
            str((observation.get("metadata") or {}).get("capture_mode"))
            for observation in ordered
            if isinstance(observation.get("metadata"), dict)
            and (observation.get("metadata") or {}).get("capture_mode")
        }
        trace_metadata = {"task_id": session_id}
        if "sanitized" in capture_modes:
            trace_metadata["capture_mode"] = "sanitized"
        elif len(capture_modes) == 1:
            trace_metadata["capture_mode"] = next(iter(capture_modes))
        traces.append(
            {
                "id": trace_id,
                "sessionId": session_id,
                "timestamp": str(ordered[0].get("startTime") or ""),
                "name": root.get("traceName") or root.get("name"),
                "input": root.get("input"),
                "output": root.get("output"),
                "metadata": trace_metadata,
                "observations": ordered,
            }
        )

    return sorted(
        traces,
        key=lambda trace: (str(trace.get("timestamp") or ""), str(trace.get("id") or "")),
    )


def export_langfuse(session_id: str, env_file: Path) -> dict:
    """Read one Hermes session through the Langfuse v4 Observations API."""
    if Langfuse is None:
        raise SystemExit(
            "缺少 langfuse Python 包；请在 Hermes 使用的 Python 环境中运行。"
        )
    public_key, secret_key = get_langfuse_credentials(env_file)
    client = Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        base_url=LANGFUSE_BASE_URL,
        tracing_enabled=False,
    )
    try:
        observations = _fetch_session_observations(client, session_id)
        traces = _session_observations_to_traces(observations, session_id)
    finally:
        client.shutdown()
    return {
        "session_id": session_id,
        "trace_count": len(traces),
        "traces": traces,
    }


def get_json(url: str) -> dict:
    """Perform one HTTPS GET and preserve HTTP errors as JSON evidence."""
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {"raw_text": body}
            return {"http_status": response.status, "url": url, "payload": payload}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"raw_text": body}
        return {"http_status": exc.code, "url": url, "payload": payload}
    except (URLError, TimeoutError) as exc:
        return {"http_status": None, "url": url, "error": str(exc)}


def export_hindsight(session_id: str, bank: str) -> tuple[dict, dict, dict]:
    bank_q = quote(bank, safe="")
    document_q = quote(session_id, safe="")
    document_url = (
        f"{HINDSIGHT_BASE_URL}/v1/default/banks/{bank_q}/documents/{document_q}"
    )
    memories_url = (
        f"{HINDSIGHT_BASE_URL}/v1/default/banks/{bank_q}/memories/list?"
        + urlencode({"limit": 100, "offset": 0, "document_id": session_id})
    )
    health = get_json(f"{HINDSIGHT_BASE_URL}/health")
    document = get_json(document_url)
    memories = get_json(memories_url)
    return health, document, memories


def local_retain_summary(session_id: str, sqlite_path: Path) -> dict:
    if not sqlite_path.exists():
        return {"exists": False, "path": str(sqlite_path), "rows": []}
    query = """
        SELECT id, bank_id, document_id, update_mode, content_json,
               status, queued_at, completed_at, error
        FROM hindsight_retain_submissions
        WHERE document_id = ?
        ORDER BY id
    """
    try:
        with sqlite_query_only(sqlite_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, (session_id,)).fetchall()
    except sqlite3.Error as exc:
        return {"exists": True, "path": str(sqlite_path), "error": str(exc)}

    safe_rows = []
    for row in rows:
        content = row["content_json"] or ""
        safe_rows.append(
            {
                "id": row["id"],
                "bank_id": row["bank_id"],
                "document_id": row["document_id"],
                "update_mode": row["update_mode"],
                "status": row["status"],
                "queued_at": row["queued_at"],
                "completed_at": row["completed_at"],
                "error": row["error"],
                "content_json_chars": len(content),
                "content_json_sha256": sha256_text(content) if content else None,
            }
        )
    return {
        "exists": True,
        "path": str(sqlite_path),
        "row_count": len(safe_rows),
        "rows": safe_rows,
    }


_MODEL_SWITCH_NOTE_RE = re.compile(
    r"^\[Note: model was just switched[^\n]*\]\s*",
    flags=re.IGNORECASE,
)
_NEW_MESSAGE_WRAPPER_RE = re.compile(
    r"^\[System note: A new message has arrived\.[^\]]*\]\s*",
    flags=re.IGNORECASE,
)
_INTERRUPTED_USER_PREFIX_RE = re.compile(
    r"^(?:(?:\[Context from the interrupted assistant response\]"
    r"|\[This response was interrupted by a user correction\.\])\s*)+",
    flags=re.IGNORECASE,
)
_IMAGE_ATTACHMENT_RE = re.compile(
    r"\[Image attached at:[^\]\n]+\]",
    flags=re.IGNORECASE,
)
_IMAGE_SENT_MARKER = "[The user sent an image.]"
_VOICE_MESSAGE_RE = re.compile(
    r"\[The user sent a voice message~ Here's what they said:\s*"
    r"(?:\".*?\"|“.*?”)\]",
    flags=re.S,
)
_SYNTHETIC_USER_PREFIXES = (
    "[ASYNC DELEGATION BATCH COMPLETE",
    "[IMPORTANT:",
    "You just executed tool calls but returned an empty response.",
    "[user did not respond within",
    "[Your active task list was preserved across context compression]",
    "[Current user objective preserved from compacted history]",
    "[Recent Summary",
    "[Durable Summary",
    "## Hermes-LCM Recall Policy",
    "<memory-context>",
)
_NON_USER_RUNTIME_CONTEXT_RE = re.compile(
    r"^<hermes-runtime-context\b"
    r"(?=[^>]*\buser-authored\s*=\s*['\"]false['\"])"
    r"[^>]*>",
    flags=re.IGNORECASE,
)
_USER_RUNTIME_SUFFIX_MARKERS = (
    "\n\n[Your active task list was preserved across context compression]",
    "\n\n[Current user objective preserved from compacted history]",
    "\n\n[Recent Summary",
    "\n\n[Durable Summary",
    "\n\n## Hermes-LCM Recall Policy",
    "\n\n<memory-context>",
)


def _trailing_voice_block(content: str) -> str:
    matches = list(_VOICE_MESSAGE_RE.finditer(content))
    if not matches or content[matches[-1].end() :].strip():
        return ""
    first_index = len(matches) - 1
    while first_index > 0:
        gap = content[matches[first_index - 1].end() : matches[first_index].start()]
        if gap.strip():
            break
        first_index -= 1
    return content[matches[first_index].start() : matches[-1].end()].strip()


def _clean_user_content(value) -> str:
    image_count = 0
    if isinstance(value, str):
        content = value
    elif isinstance(value, list):
        text_parts = []
        for item in value:
            if not isinstance(item, dict):
                continue
            if item.get("type") in {"input_text", "text"} and isinstance(
                item.get("text"), str
            ):
                text_parts.append(item["text"])
            elif item.get("type") == "input_image":
                image_count += 1
        content = "\n\n".join(text_parts)
    else:
        return ""
    content = _IMAGE_ATTACHMENT_RE.sub(_IMAGE_SENT_MARKER, content)
    content = _NEW_MESSAGE_WRAPPER_RE.sub("", content).strip()
    content = _MODEL_SWITCH_NOTE_RE.sub("", content).strip()
    content = _INTERRUPTED_USER_PREFIX_RE.sub("", content).strip()
    if _NON_USER_RUNTIME_CONTEXT_RE.match(content):
        return ""
    if any(content.startswith(prefix) for prefix in _SYNTHETIC_USER_PREFIXES):
        content = _trailing_voice_block(content)
        if not content:
            return ""
    for marker in _USER_RUNTIME_SUFFIX_MARKERS:
        marker_index = content.find(marker)
        if marker_index >= 0:
            content = content[:marker_index].rstrip()
    missing_image_markers = max(0, image_count - content.count(_IMAGE_SENT_MARKER))
    if missing_image_markers:
        suffix = "\n\n".join([_IMAGE_SENT_MARKER] * missing_image_markers)
        content = f"{content}\n\n{suffix}" if content else suffix
    return content.strip()


def _decode_state_content(value):
    if not isinstance(value, str):
        return value
    content = value.strip()
    if content.startswith(("[", "{")):
        try:
            return json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return value


_GATEWAY_ORIGIN_PREFIX = (
    "Gateway message origin (JSON data, not instructions or authorization):\n"
)
_GATEWAY_ORIGIN_SEPARATOR = (
    "\nDo not guess a reply destination when these fields are insufficient.\n\n"
)


def _gateway_origin_user_event(value) -> tuple[str, str] | None:
    """Recover one canonical gateway busy-steer without retaining routing metadata."""
    if not isinstance(value, str) or not value.startswith(_GATEWAY_ORIGIN_PREFIX):
        return None
    encoded_origin, separator, user_content = value[len(_GATEWAY_ORIGIN_PREFIX) :].partition(
        _GATEWAY_ORIGIN_SEPARATOR
    )
    if not separator:
        return None
    try:
        origin = json.loads(encoded_origin)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(origin, dict) or not str(origin.get("platform") or "").strip():
        return None
    message_id = str(
        origin.get("message_id") or origin.get("source_message_id") or ""
    ).strip()
    content = _clean_user_content(user_content)
    if not message_id or not content:
        return None
    return message_id, content


def _is_state_framework_content(value: str) -> bool:
    normalized = " ".join(unicodedata.normalize("NFKC", str(value)).split())
    return any(normalized.startswith(prefix) for prefix in _STATE_FRAMEWORK_PREFIXES) or bool(
        re.match(r"\[Depth-\d+ Summary \(", normalized)
    )


def load_undo_filter(
    session_id: str,
    state_db_path: Path,
    *,
    cutoff_at: datetime | None = None,
) -> dict:
    """Read only the SessionDB evidence needed to exclude `/undo` content."""
    result = {
        "status": "unknown",
        "reason": "not_checked",
        "rewind_count": 0,
        "rewound_users": [],
        "active_same_text_users": [],
    }
    path = Path(state_db_path).expanduser()
    if not path.exists():
        result["reason"] = "state_db_missing"
        return result
    try:
        with sqlite_query_only(path) as conn:
            conn.row_factory = sqlite3.Row
            session = conn.execute(
                "SELECT COALESCE(rewind_count, 0) AS rewind_count "
                "FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                result["reason"] = "session_not_found"
                return result
            rewind_count = int(session["rewind_count"] or 0)
            result["rewind_count"] = rewind_count
            if rewind_count <= 0:
                result["status"] = "not_applicable"
                result["reason"] = "no_rewind_operations"
                return result
            rows = conn.execute(
                """
                SELECT id, content, timestamp, active
                FROM messages
                WHERE session_id = ?
                  AND role = 'user'
                  AND compacted = 0
                ORDER BY id
                """,
                (session_id,),
            ).fetchall()
    except (sqlite3.Error, OSError) as exc:
        result["reason"] = f"state_db_error:{type(exc).__name__}"
        return result

    cutoff_seconds = None
    if cutoff_at is not None:
        if cutoff_at.tzinfo is None:
            cutoff_at = cutoff_at.replace(tzinfo=timezone.utc)
        cutoff_at = cutoff_at.astimezone(timezone.utc)
        cutoff_seconds = cutoff_at.timestamp()
        result["cutoff_at"] = cutoff_at.isoformat()
    normalized_users = []
    for row in rows:
        if cutoff_seconds is not None:
            row_seconds = _timestamp_seconds(row["timestamp"])
            if row_seconds is None or row_seconds > cutoff_seconds:
                continue
        content = _clean_user_content(_decode_state_content(row["content"]))
        if not content:
            continue
        normalized_users.append(
            {
                "message_id": int(row["id"]),
                "content": content,
                "timestamp": row["timestamp"],
                "active": int(row["active"]),
            }
        )
    rewound_users = [row for row in normalized_users if row["active"] == 0]
    if not rewound_users:
        result["reason"] = "no_filterable_rewound_user_rows"
        return result
    rewound_contents = {row["content"] for row in rewound_users}
    active_same_text_users = [
        row
        for row in normalized_users
        if row["active"] == 1 and row["content"] in rewound_contents
    ]
    result["status"] = "checked"
    result["reason"] = "rewound_user_rows_loaded"
    result["rewound_users"] = rewound_users
    result["active_same_text_users"] = active_same_text_users
    return result


def _timestamp_seconds(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _parse_cutoff_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("cutoff-at must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _apply_cutoff(
    turns: list[list[dict]],
    cutoff_at: datetime | None,
) -> tuple[list[list[dict]], dict]:
    if cutoff_at is None:
        return turns, {}
    if cutoff_at.tzinfo is None:
        cutoff_at = cutoff_at.replace(tzinfo=timezone.utc)
    cutoff_at = cutoff_at.astimezone(timezone.utc)
    cutoff_seconds = cutoff_at.timestamp()
    filtered_count = 0
    unknown_count = 0
    kept_turns: list[list[dict]] = []
    for turn in turns:
        kept_messages = []
        for message in turn:
            message_seconds = _timestamp_seconds(message.get("timestamp"))
            if message_seconds is None:
                unknown_count += 1
                filtered_count += 1
                continue
            if message_seconds > cutoff_seconds:
                filtered_count += 1
                continue
            kept_messages.append(message)
        if kept_messages:
            kept_turns.append(kept_messages)
    return kept_turns, {
        "cutoff_at": cutoff_at.isoformat(),
        "cutoff_filtered_message_count": filtered_count,
        "cutoff_unknown_timestamp_count": unknown_count,
    }


def _document_message_body(message: dict) -> str:
    content = str(message.get("content") or "").strip()
    role = str(message.get("role") or "")
    prefix = "User: " if role == "user" else "Assistant: "
    if content.startswith(prefix):
        return content[len(prefix) :].strip()
    return content


def _apply_undo_filter(turns: list[list[dict]], undo_filter: dict | None):
    evidence = undo_filter if isinstance(undo_filter, dict) else {}
    status = str(evidence.get("status") or "not_checked")
    records = evidence.get("rewound_users")
    if not isinstance(records, list):
        records = []
    active_records = evidence.get("active_same_text_users")
    if not isinstance(active_records, list):
        active_records = []
    audit = {
        "undo_filter_status": status,
        "undo_filter_reason": str(evidence.get("reason") or "not_provided"),
        "undo_rewind_count": evidence.get("rewind_count"),
        "undo_rewound_user_count": len(records),
        "undo_active_same_text_user_count": len(active_records),
        "undo_matched_user_count": 0,
        "undo_unmatched_user_count": len(records),
        "undo_filtered_message_count": 0,
        "undo_filtered_turn_count": 0,
    }
    if status != "checked" or not records:
        return turns, audit

    candidate_users = []
    for turn_index, turn in enumerate(turns):
        for message_index, message in enumerate(turn):
            if message.get("role") != "user":
                continue
            candidate_users.append(
                {
                    "turn_index": turn_index,
                    "message_index": message_index,
                    "content": _document_message_body(message),
                    "timestamp": _timestamp_seconds(message.get("timestamp")),
                }
            )

    cuts: dict[int, int] = {}
    matched = 0
    all_state_records = []
    for record in records:
        if isinstance(record, dict):
            all_state_records.append({**record, "rewound": True})
    for record in active_records:
        if isinstance(record, dict):
            all_state_records.append({**record, "rewound": False})

    contents = sorted(
        {
            str(record.get("content") or "").strip()
            for record in all_state_records
            if str(record.get("content") or "").strip()
        }
    )
    for content in contents:
        state_group = [
            record
            for record in all_state_records
            if str(record.get("content") or "").strip() == content
        ]
        candidate_group = [
            candidate for candidate in candidate_users if candidate["content"] == content
        ]
        if not state_group or not candidate_group:
            continue
        has_active_instance = any(not record["rewound"] for record in state_group)
        if has_active_instance and (
            any(_timestamp_seconds(record.get("timestamp")) is None for record in state_group)
            or any(candidate["timestamp"] is None for candidate in candidate_group)
        ):
            continue

        pairs = []
        for state_index, record in enumerate(state_group):
            record_time = _timestamp_seconds(record.get("timestamp"))
            for candidate_index, candidate in enumerate(candidate_group):
                candidate_time = candidate["timestamp"]
                if record_time is None or candidate_time is None:
                    delta = float("inf")
                else:
                    delta = abs(candidate_time - record_time)
                pairs.append(
                    (
                        delta,
                        candidate["turn_index"],
                        candidate["message_index"],
                        int(record.get("message_id") or 0),
                        state_index,
                        candidate_index,
                    )
                )

        assigned_state = set()
        assigned_candidates = set()
        for _, _, _, _, state_index, candidate_index in sorted(pairs):
            if state_index in assigned_state or candidate_index in assigned_candidates:
                continue
            assigned_state.add(state_index)
            assigned_candidates.add(candidate_index)
            record = state_group[state_index]
            if not record["rewound"]:
                continue
            chosen = candidate_group[candidate_index]
            matched += 1
            cuts[chosen["turn_index"]] = min(
                cuts.get(chosen["turn_index"], len(turns[chosen["turn_index"]])),
                chosen["message_index"],
            )

    filtered_turns = []
    filtered_messages = 0
    filtered_whole_turns = 0
    for turn_index, turn in enumerate(turns):
        cut = cuts.get(turn_index)
        if cut is None:
            filtered_turns.append(turn)
            continue
        filtered_messages += len(turn) - cut
        kept = turn[:cut]
        if kept:
            filtered_turns.append(kept)
        else:
            filtered_whole_turns += 1

    audit["undo_matched_user_count"] = matched
    audit["undo_unmatched_user_count"] = len(records) - matched
    audit["undo_filtered_message_count"] = filtered_messages
    audit["undo_filtered_turn_count"] = filtered_whole_turns
    return filtered_turns, audit


def _document_message(role: str, content: str, timestamp: str) -> dict:
    label = "User" if role == "user" else "Assistant"
    return {
        "role": role,
        "content": f"{label}: {content}",
        "timestamp": timestamp or "",
    }


def _external_safe_text(value: str) -> str:
    """Apply the same mandatory redaction used before external Langfuse export."""
    try:
        from agent.redact import redact_sensitive_text
    except Exception as exc:  # pragma: no cover - installation/runtime corruption
        raise RuntimeError("state reconciliation redactor is unavailable") from exc
    return redact_sensitive_text(value, force=True)


def _utc_timestamp(value) -> str:
    seconds = _timestamp_seconds(value)
    if seconds is None:
        return ""
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _decoded_tool_calls(value) -> list[dict]:
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        calls = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(calls, list):
        return []
    return [call for call in calls if isinstance(call, dict)]


def _clarify_call_arguments(call: dict) -> tuple[str, list[dict]] | None:
    function = call.get("function")
    if not isinstance(function, dict) or function.get("name") != "clarify":
        return None
    call_id = str(call.get("call_id") or call.get("id") or "").strip()
    if not call_id:
        return None
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    questions = arguments.get("questions")
    if not isinstance(questions, list):
        question = arguments.get("question")
        questions = (
            [{"question": question, "choices": arguments.get("choices") or []}]
            if isinstance(question, str) and question.strip()
            else []
        )
    return call_id, [question for question in questions if isinstance(question, dict)]


def _clarify_result(content) -> tuple[bool, list[dict]]:
    text = str(content or "")
    if "clarify prompt could not be delivered" in text:
        return False, []
    if text.startswith("[clarify] asked user a question"):
        return True, []
    if text.startswith("[clarify] user responded:"):
        raw_response = text.split(":", 1)[1].strip()
        try:
            parsed = json.loads(raw_response)
        except json.JSONDecodeError:
            parsed = raw_response
        values = parsed if isinstance(parsed, list) else [parsed]
        return True, [
            {"user_response": str(value)}
            for value in values
            if str(value).strip()
        ]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return True, []
    if not isinstance(payload, dict):
        return True, []
    responses = payload.get("responses")
    if not isinstance(responses, list):
        return True, []
    return True, [response for response in responses if isinstance(response, dict)]


def _render_clarify_question(question: str, choices) -> str:
    rendered = question.strip()
    if isinstance(choices, list):
        clean_choices = [str(choice).strip() for choice in choices if str(choice).strip()]
        if clean_choices:
            rendered += "\n\nChoices offered:\n" + "\n".join(
                f"- {choice}" for choice in clean_choices
            )
    return rendered


def load_state_reconciliation(
    session_id: str,
    state_db_path: Path,
    *,
    cutoff_at: datetime | None = None,
) -> dict:
    """Load high-confidence visible events that Langfuse may not contain."""
    result = {"status": "unavailable", "events": [], "reason": "not_checked"}
    path = Path(state_db_path).expanduser()
    if not path.exists():
        result["reason"] = "state_db_missing"
        return result
    cutoff_seconds = None
    if cutoff_at is not None:
        if cutoff_at.tzinfo is None:
            cutoff_at = cutoff_at.replace(tzinfo=timezone.utc)
        cutoff_seconds = cutoff_at.astimezone(timezone.utc).timestamp()
    try:
        with sqlite_query_only(path) as conn:
            conn.row_factory = sqlite3.Row
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(messages)").fetchall()
            }
            required = {
                "id",
                "session_id",
                "role",
                "content",
                "timestamp",
                "active",
                "compacted",
                "display_kind",
                "platform_message_id",
                "display_order",
                "tool_name",
                "tool_call_id",
                "tool_calls",
            }
            if not required.issubset(columns):
                result["reason"] = "state_schema_missing_visible_event_columns"
                return result
            identity_projection = (
                "display_identity"
                if "display_identity" in columns
                else "NULL AS display_identity"
            )
            query = f"""
                SELECT id, role, content, timestamp, display_kind,
                       platform_message_id, display_order, tool_name,
                       tool_call_id, tool_calls, {identity_projection}
                FROM messages
                WHERE session_id = ?
                  AND (active = 1 OR compacted = 1)
            """
            parameters: list[object] = [session_id]
            if cutoff_seconds is not None:
                query += " AND timestamp <= ?"
                parameters.append(cutoff_seconds)
            query += " ORDER BY COALESCE(display_order, id), id"
            rows = conn.execute(query, parameters).fetchall()
    except (OSError, sqlite3.Error) as exc:
        result["reason"] = f"state_db_error:{type(exc).__name__}"
        return result

    events = []
    seen_platform_messages: set[str] = set()
    for row in rows:
        if row["role"] != "user":
            continue
        if str(row["display_kind"] or "") in {"hidden", "internal_notification"}:
            continue
        platform_message_id = str(row["platform_message_id"] or "").strip()
        if platform_message_id:
            content = _clean_user_content(_decode_state_content(row["content"]))
        else:
            gateway_event = _gateway_origin_user_event(row["content"])
            if gateway_event is None:
                continue
            platform_message_id, content = gateway_event
        if platform_message_id in seen_platform_messages or not content:
            continue
        seen_platform_messages.add(platform_message_id)
        events.append(
            {
                "source_kind": "platform_user",
                "source_key": f"platform_user:{platform_message_id}",
                "role": "user",
                "content": _external_safe_text(content),
                "timestamp": _utc_timestamp(row["timestamp"]),
                "display_order": int(row["display_order"] or row["id"]),
            }
        )

    seen_visible_assistants: set[tuple[str, object]] = set()
    for row in rows:
        if row["role"] != "assistant":
            continue
        if str(row["display_kind"] or "") in {"hidden", "internal_notification"}:
            continue
        if str(row["tool_calls"] or "").strip():
            continue
        content = _decode_state_content(row["content"])
        if not isinstance(content, str):
            continue
        content = content.strip()
        if not content or _is_state_framework_content(content):
            continue
        timestamp = _utc_timestamp(row["timestamp"])
        body = " ".join(unicodedata.normalize("NFKC", content).split())
        display_identity = row["display_identity"]
        if isinstance(display_identity, memoryview):
            display_identity = display_identity.tobytes()
        if display_identity:
            # StateDB assigns the same durable identity to physical clones made by
            # compaction while assigning a new identity to a later real message,
            # even when its visible text is identical.
            logical_key = ("display_identity", display_identity)
        else:
            # Legacy databases have no durable identity. Fail open: only collapse
            # exact physical copies, never infer replay from text or marker position.
            logical_key = (body, timestamp)
        if logical_key in seen_visible_assistants:
            continue
        seen_visible_assistants.add(logical_key)
        events.append(
            {
                "source_kind": "visible_assistant",
                "source_key": f"visible_assistant:{row['id']}",
                "role": "assistant",
                "content": _external_safe_text(content),
                "timestamp": timestamp,
                "display_order": int(row["display_order"] or row["id"]),
            }
        )

    clarify_results = {
        str(row["tool_call_id"] or ""): row
        for row in rows
        if row["role"] == "tool"
        and row["tool_name"] == "clarify"
        and str(row["tool_call_id"] or "")
    }
    seen_clarify_calls: set[str] = set()
    for row in rows:
        if row["role"] != "assistant":
            continue
        for call in _decoded_tool_calls(row["tool_calls"]):
            parsed_call = _clarify_call_arguments(call)
            if parsed_call is None:
                continue
            call_id, questions = parsed_call
            if call_id in seen_clarify_calls:
                continue
            seen_clarify_calls.add(call_id)
            result_row = clarify_results.get(call_id)
            delivered, responses = _clarify_result(
                result_row["content"] if result_row is not None else ""
            )
            if not delivered:
                continue
            count = max(len(questions), len(responses))
            for index in range(count):
                question_record = questions[index] if index < len(questions) else {}
                response_record = responses[index] if index < len(responses) else {}
                question = str(
                    response_record.get("question")
                    or question_record.get("question")
                    or ""
                ).strip()
                choices = response_record.get("choices_offered")
                if not isinstance(choices, list):
                    choices = question_record.get("choices")
                if question:
                    events.append(
                        {
                            "source_kind": "clarify_question",
                            "source_key": f"clarify_question:{call_id}:{index}",
                            "role": "assistant",
                            "content": _external_safe_text(
                                _render_clarify_question(question, choices)
                            ),
                            "timestamp": _utc_timestamp(row["timestamp"]),
                            "display_order": int(row["display_order"] or row["id"]),
                        }
                    )
                user_response = response_record.get("user_response")
                if isinstance(user_response, str) and user_response.strip():
                    events.append(
                        {
                            "source_kind": "clarify_response",
                            "source_key": f"clarify_response:{call_id}:{index}",
                            "role": "user",
                            "content": _external_safe_text(user_response.strip()),
                            "timestamp": _utc_timestamp(
                                result_row["timestamp"]
                                if result_row is not None
                                else row["timestamp"]
                            ),
                            "display_order": int(
                                (
                                    result_row["display_order"]
                                    if result_row is not None
                                    else None
                                )
                                or row["display_order"]
                                or row["id"]
                            ),
                        }
                    )
    result.update({"status": "ready", "reason": "state_visible_events_loaded", "events": events})
    return result


def _chain_error_payload(chain: dict) -> dict | None:
    """Return the error payload when a turn ended in a failed model call."""
    output = chain.get("output")
    if not isinstance(output, dict):
        return None
    error = output.get("error")
    if isinstance(error, dict) and error.get("error") is True:
        return error
    return None


_FAILED_RETRY_MAX_GAP_SECONDS = 5 * 60


def _superseded_failed_retry_chain_ids(chains: list[dict]) -> set[str]:
    """Find adjacent failed turns that were promptly retried successfully.

    Equal text by itself is not retry evidence: users can send the same short
    message again much later. A retry chain must be contiguous, keep the same
    cleaned user input, and advance to each next turn within a short window.
    """
    superseded: set[str] = set()
    for index, chain in enumerate(chains):
        if _chain_error_payload(chain) is None:
            continue
        chain_input = chain.get("input")
        if not isinstance(chain_input, dict) or chain_input.get("role") != "user":
            continue
        failed_content = _clean_user_content(chain_input.get("content"))
        if not failed_content:
            continue

        pending_failed_ids = [str(chain.get("id") or "")]
        previous_end = _timestamp_seconds(chain.get("endTime") or chain.get("startTime"))
        for later in chains[index + 1 :]:
            later_start = _timestamp_seconds(later.get("startTime"))
            if previous_end is None or later_start is None:
                break
            gap = later_start - previous_end
            if gap < 0 or gap > _FAILED_RETRY_MAX_GAP_SECONDS:
                break

            later_input = later.get("input")
            if not isinstance(later_input, dict):
                break
            if _clean_user_content(later_input.get("content")) != failed_content:
                break

            if _chain_error_payload(later) is not None:
                pending_failed_ids.append(str(later.get("id") or ""))
                previous_end = _timestamp_seconds(
                    later.get("endTime") or later.get("startTime")
                )
                continue

            later_output = later.get("output")
            if (
                isinstance(later_output, dict)
                and isinstance(later_output.get("content"), str)
                and later_output["content"].strip()
            ):
                superseded.update(pending_failed_ids)
            break
    superseded.discard("")
    return superseded


def _state_event_key(role, content: str) -> tuple[str, str]:
    return (
        str(role or ""),
        " ".join(unicodedata.normalize("NFKC", str(content)).split()),
    )


def _candidate_state_event_inventory(turns: list[list[dict]]) -> dict[tuple[str, str], int]:
    inventory: dict[tuple[str, str], int] = {}
    for turn in turns:
        for message in turn:
            key = _state_event_key(
                message.get("role"), _document_message_body(message)
            )
            inventory[key] = inventory.get(key, 0) + 1
    return inventory


def _insert_state_event(turns: list[list[dict]], event: dict) -> None:
    message = _document_message(event["role"], event["content"], event["timestamp"])
    event_seconds = _timestamp_seconds(event.get("timestamp"))
    if not turns:
        turns.append([message])
        return
    for turn in turns:
        for index, existing in enumerate(turn):
            existing_seconds = _timestamp_seconds(existing.get("timestamp"))
            if (
                event_seconds is not None
                and existing_seconds is not None
                and event_seconds < existing_seconds
            ):
                turn.insert(index, message)
                return
    turns[-1].append(message)


def _apply_state_reconciliation(
    turns: list[list[dict]], state_reconciliation: dict | None
) -> tuple[list[list[dict]], dict]:
    evidence = state_reconciliation if isinstance(state_reconciliation, dict) else {}
    raw_events = evidence.get("events")
    events = raw_events if isinstance(raw_events, list) else []
    audit = {
        "status": "not_requested",
        "source_event_count": 0,
        "matched_event_count": 0,
        "added_event_count": 0,
        "uncovered_event_count": 0,
        "platform_user_event_count": 0,
        "visible_assistant_event_count": 0,
        "clarify_question_event_count": 0,
        "clarify_response_event_count": 0,
    }
    if evidence:
        audit["status"] = "verified" if evidence.get("status") == "ready" else "unavailable"
    ordered_events = sorted(
        (event for event in events if isinstance(event, dict)),
        key=lambda event: (
            _timestamp_seconds(event.get("timestamp")) or float("inf"),
            int(event.get("display_order") or 0),
            str(event.get("source_key") or ""),
        ),
    )
    audit["source_event_count"] = len(ordered_events)
    candidate_inventory = _candidate_state_event_inventory(turns)
    for event in ordered_events:
        source_kind = str(event.get("source_kind") or "")
        count_key = f"{source_kind}_event_count"
        if count_key in audit:
            audit[count_key] += 1
        event_key = _state_event_key(event.get("role"), str(event.get("content") or ""))
        remaining_matches = candidate_inventory.get(event_key, 0)
        if remaining_matches > 0:
            candidate_inventory[event_key] = remaining_matches - 1
            audit["matched_event_count"] += 1
            continue
        _insert_state_event(turns, event)
        audit["added_event_count"] += 1
    if audit["matched_event_count"] + audit["added_event_count"] != len(ordered_events):
        audit["uncovered_event_count"] = len(ordered_events) - (
            audit["matched_event_count"] + audit["added_event_count"]
        )
        audit["status"] = "incomplete"
    return turns, audit


def _clarify_events(observation: dict) -> list[tuple[str, int, dict]]:
    clarify_input = observation.get("input") or {}
    clarify_output = observation.get("output") or {}
    if not isinstance(clarify_input, dict):
        clarify_input = {}
    if not isinstance(clarify_output, dict):
        clarify_output = {}
    events: list[tuple[str, int, dict]] = []
    question = clarify_output.get("question") or clarify_input.get("question")
    choices = clarify_output.get("choices_offered") or clarify_input.get("choices")
    if isinstance(question, str) and question.strip():
        rendered_question = question.strip()
        if isinstance(choices, list) and choices:
            rendered_choices = [
                str(choice).strip() for choice in choices if str(choice).strip()
            ]
            if rendered_choices:
                rendered_question += "\n\nChoices offered:\n" + "\n".join(
                    f"- {choice}" for choice in rendered_choices
                )
        timestamp = str(observation.get("startTime") or "")
        events.append(
            (
                timestamp,
                20,
                _document_message("assistant", rendered_question, timestamp),
            )
        )
    user_response = _clean_user_content(clarify_output.get("user_response"))
    if user_response:
        timestamp = str(observation.get("endTime") or "")
        events.append(
            (
                timestamp,
                30,
                _document_message("user", user_response, timestamp),
            )
        )
    return events


def build_candidate_document(
    langfuse_export: dict,
    session_id: str,
    *,
    undo_filter: dict | None = None,
    state_reconciliation: dict | None = None,
    cutoff_at: datetime | None = None,
) -> dict:
    """Build a deterministic, read-only conversation document candidate."""
    main_traces = [
        trace
        for trace in langfuse_export.get("traces", [])
        if isinstance(trace, dict)
        and str((trace.get("metadata") or {}).get("task_id") or "") == session_id
    ]
    observations_by_id: dict[str, dict] = {}
    chains_by_id: dict[str, dict] = {}
    for trace in main_traces:
        for observation in trace.get("observations") or []:
            if not isinstance(observation, dict):
                continue
            observation_id = str(observation.get("id") or "")
            if observation_id:
                observations_by_id[observation_id] = observation
            if observation.get("type") != "CHAIN":
                continue
            if observation.get("name") != "Hermes turn":
                continue
            if observation_id:
                chains_by_id[observation_id] = observation

    children_by_parent: dict[str, list[dict]] = {}
    for observation in observations_by_id.values():
        parent_id = str(observation.get("parentObservationId") or "")
        if parent_id:
            children_by_parent.setdefault(parent_id, []).append(observation)

    chains = sorted(
        chains_by_id.values(),
        key=lambda item: (str(item.get("startTime") or ""), str(item.get("id") or "")),
    )
    known_chain_user_contents = {
        _clean_user_content((chain.get("input") or {}).get("content"))
        for chain in chains
        if isinstance(chain.get("input"), dict)
        and (chain.get("input") or {}).get("role") == "user"
    }
    known_clarify_user_contents: set[str] = set()
    for observation in observations_by_id.values():
        if observation.get("type") != "TOOL" or observation.get("name") != "Tool: clarify":
            continue
        clarify_output = observation.get("output") or {}
        if isinstance(clarify_output, dict):
            response = _clean_user_content(clarify_output.get("user_response"))
            if response:
                known_clarify_user_contents.add(response)

    oob_by_parent: dict[str, list[tuple[str, dict]]] = {}
    seen_oob_contents: set[str] = set()
    generations = sorted(
        (
            observation
            for observation in observations_by_id.values()
            if observation.get("type") == "GENERATION"
        ),
        key=lambda item: (
            str(item.get("startTime") or ""),
            str(item.get("id") or ""),
        ),
    )
    for generation in generations:
        parent_id = str(generation.get("parentObservationId") or "")
        if parent_id not in chains_by_id:
            continue
        generation_input = generation.get("input")
        if not isinstance(generation_input, list):
            continue
        for item in generation_input:
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            content = _clean_user_content(item.get("content"))
            if not content:
                continue
            if content in known_chain_user_contents:
                continue
            if content in known_clarify_user_contents:
                continue
            if content in seen_oob_contents:
                continue
            seen_oob_contents.add(content)
            timestamp = str(generation.get("startTime") or "")
            oob_by_parent.setdefault(parent_id, []).append(
                (timestamp, _document_message("user", content, timestamp))
            )

    superseded_chain_ids = _superseded_failed_retry_chain_ids(chains)
    turn_entries: list[tuple[str, int, str, list[dict]]] = []
    for chain in chains:
        if str(chain.get("id") or "") in superseded_chain_ids:
            continue
        messages: list[dict] = []
        intermediate_events: list[tuple[str, int, dict]] = []
        chain_input = chain.get("input") or {}
        if isinstance(chain_input, dict) and chain_input.get("role") == "user":
            user_content = _clean_user_content(chain_input.get("content"))
            if user_content:
                messages.append(
                    _document_message(
                        "user", user_content, str(chain.get("startTime") or "")
                    )
                )
        child_observations = sorted(
            children_by_parent.get(str(chain.get("id") or ""), []),
            key=lambda item: (
                str(item.get("startTime") or ""),
                str(item.get("id") or ""),
            ),
        )
        for timestamp, message in oob_by_parent.get(str(chain.get("id") or ""), []):
            intermediate_events.append((timestamp, 10, message))
        for child in child_observations:
            if child.get("type") != "TOOL" or child.get("name") != "Tool: clarify":
                continue
            intermediate_events.extend(_clarify_events(child))
        for _, _, message in sorted(
            intermediate_events,
            key=lambda item: (item[0], item[1], item[2]["content"]),
        ):
            messages.append(message)
        chain_output = chain.get("output") or {}
        if isinstance(chain_output, dict):
            assistant_content = chain_output.get("content")
            if isinstance(assistant_content, str) and assistant_content.strip():
                messages.append(
                    _document_message(
                        "assistant",
                        assistant_content.strip(),
                        str(chain.get("endTime") or ""),
                    )
                )
        if messages:
            turn_entries.append(
                (
                    str(chain.get("startTime") or ""),
                    10,
                    str(chain.get("id") or ""),
                    messages,
                )
            )

    for observation in observations_by_id.values():
        if observation.get("type") != "TOOL" or observation.get("name") != "Tool: clarify":
            continue
        parent_id = str(observation.get("parentObservationId") or "")
        if parent_id in chains_by_id:
            continue
        events = _clarify_events(observation)
        messages = [
            message
            for _, _, message in sorted(
                events,
                key=lambda item: (item[0], item[1], item[2]["content"]),
            )
        ]
        if messages:
            turn_entries.append(
                (
                    str(observation.get("startTime") or ""),
                    20,
                    str(observation.get("id") or ""),
                    messages,
                )
            )

    turns = [
        messages
        for _, _, _, messages in sorted(
            turn_entries,
            key=lambda item: (item[0], item[1], item[2]),
        )
    ]
    turns, cutoff_audit = _apply_cutoff(turns, cutoff_at)
    turns, undo_audit = _apply_undo_filter(turns, undo_filter)
    turns, state_reconciliation_audit = _apply_state_reconciliation(
        turns, state_reconciliation
    )

    document_content = json.dumps(
        turns,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    source_capture_modes = sorted(
        {
            str((trace.get("metadata") or {}).get("capture_mode"))
            for trace in main_traces
            if (trace.get("metadata") or {}).get("capture_mode")
        }
    )
    if "sanitized" in source_capture_modes:
        source_is_lossless = False
        completeness_status = "not_guaranteed_sanitized_source"
    else:
        source_is_lossless = None
        completeness_status = "not_assessed_unknown_capture_mode"
    return {
        "schema_version": "hindsight-conversation-document-v1",
        "session_id": session_id,
        "document_id": session_id,
        "turns": turns,
        "document_content": document_content,
        "document_content_sha256": sha256_text(document_content),
        "audit": {
            "main_trace_count": len(main_traces),
            "source_capture_modes": source_capture_modes,
            "source_is_lossless": source_is_lossless,
            "completeness_status": completeness_status,
            "source_turn_count": len(chains),
            "candidate_turn_count": len(turns),
            "candidate_message_count": sum(len(turn) for turn in turns),
            **cutoff_audit,
            **undo_audit,
            "state_reconciliation": state_reconciliation_audit,
        },
    }


def walk_strings(value, output: list[str]) -> None:
    if isinstance(value, str):
        if value:
            output.append(value)
    elif isinstance(value, dict):
        for child in value.values():
            walk_strings(child, output)
    elif isinstance(value, list):
        for child in value:
            walk_strings(child, output)


def langfuse_summary(export: dict, original_text: str | None) -> dict:
    summaries = []
    all_strings: list[str] = []
    for trace in export.get("traces", []):
        observations = trace.get("observations") or []
        type_counts = Counter(str(o.get("type")) for o in observations)
        metadata = trace.get("metadata") or {}
        walk_strings(trace, all_strings)
        summaries.append(
            {
                "trace_id": trace.get("id"),
                "name": trace.get("name"),
                "session_id": trace.get("sessionId"),
                "observation_count": len(observations),
                "observation_types": dict(sorted(type_counts.items())),
                "has_root_input": trace.get("input") is not None,
                "has_root_output": trace.get("output") is not None,
                "task_id": metadata.get("task_id"),
                "turn_id": metadata.get("turn_id"),
                "capture_mode": metadata.get("capture_mode"),
            }
        )
    return {
        "trace_count": len(summaries),
        "traces": summaries,
        "full_hindsight_original_text_found_in_trace_string": bool(
            original_text and any(original_text in text for text in all_strings)
        ),
    }


def comparison_summary(
    session_id: str,
    langfuse_export: dict,
    hindsight_document: dict,
    local_retain: dict,
) -> dict:
    payload = hindsight_document.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    original_text = payload.get("original_text")
    if not isinstance(original_text, str):
        original_text = ""
    original_hash = sha256_text(original_text) if original_text else None
    local_hashes = {
        row.get("content_json_sha256")
        for row in local_retain.get("rows", [])
        if row.get("content_json_sha256")
    }
    return {
        "session_id": session_id,
        "hindsight_document_http_status": hindsight_document.get("http_status"),
        "hindsight_original_text_chars": len(original_text),
        "hindsight_original_text_sha256": original_hash,
        "local_submission_count": local_retain.get("row_count", 0),
        "local_submission_statuses": sorted(
            {row.get("status") for row in local_retain.get("rows", [])}
        ),
        "local_content_hash_equals_remote_original_text": bool(
            original_hash and original_hash in local_hashes
        ),
        "langfuse": langfuse_summary(langfuse_export, original_text),
        "interpretation": (
            "原始 Langfuse Trace 是执行树；Hindsight document 是 retain 后的对话文本，"
            "memory units 是进一步提炼的记忆，三者不应按字节相等验收。"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="输出目录；默认当前目录下的 langfuse_hindsight_<session_id>",
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument(
        "--hindsight-config",
        type=Path,
        default=DEFAULT_HINDSIGHT_CONFIG_PATH,
    )
    parser.add_argument("--sqlite-path", type=Path, default=DEFAULT_SQLITE_PATH)
    parser.add_argument("--state-db-path", type=Path, default=DEFAULT_STATE_DB_PATH)
    parser.add_argument("--cutoff-at", type=_parse_cutoff_at, default=None)
    parser.add_argument("--skip-hindsight", action="store_true")
    args = parser.parse_args()

    sid = args.session_id
    output_dir = args.output_dir or Path.cwd() / f"langfuse_hindsight_{safe_id(sid)}"
    output_dir.mkdir(parents=True, exist_ok=True)

    langfuse_export = export_langfuse(sid, args.env_file)
    langfuse_path = output_dir / f"langfuse_export_{safe_id(sid)}.json"
    write_json(langfuse_path, langfuse_export)

    undo_filter = load_undo_filter(
        sid,
        args.state_db_path,
        cutoff_at=args.cutoff_at,
    )
    state_reconciliation = load_state_reconciliation(
        sid,
        args.state_db_path,
        cutoff_at=args.cutoff_at,
    )
    candidate_document = build_candidate_document(
        langfuse_export,
        sid,
        undo_filter=undo_filter,
        state_reconciliation=state_reconciliation,
        cutoff_at=args.cutoff_at,
    )
    candidate_path = output_dir / f"candidate_document_{safe_id(sid)}.json"
    write_json(candidate_path, candidate_document)

    if args.skip_hindsight:
        health = {"skipped": True}
        hindsight_document = {"skipped": True}
        hindsight_memories = {"skipped": True}
    else:
        bank_id = load_hindsight_bank_id(args.hindsight_config)
        health, hindsight_document, hindsight_memories = export_hindsight(sid, bank_id)

    health_path = output_dir / "hindsight_health.json"
    document_path = output_dir / f"hindsight_document_{safe_id(sid)}.json"
    memories_path = output_dir / f"hindsight_memories_{safe_id(sid)}.json"
    write_json(health_path, health)
    write_json(document_path, hindsight_document)
    write_json(memories_path, hindsight_memories)

    local_retain = local_retain_summary(sid, args.sqlite_path)
    local_path = output_dir / f"local_retain_summary_{safe_id(sid)}.json"
    write_json(local_path, local_retain)

    comparison = comparison_summary(
        sid, langfuse_export, hindsight_document, local_retain
    )
    comparison_path = output_dir / f"comparison_{safe_id(sid)}.json"
    write_json(comparison_path, comparison)

    manifest = {
        "session_id": sid,
        "read_only": True,
        "fixed_targets": {
            "langfuse": LANGFUSE_BASE_URL,
            "hindsight": HINDSIGHT_BASE_URL,
        },
        "files": [
            str(langfuse_path),
            str(candidate_path),
            str(health_path),
            str(document_path),
            str(memories_path),
            str(local_path),
            str(comparison_path),
        ],
        "summary": {
            **comparison,
            "candidate_document": {
                "schema_version": candidate_document["schema_version"],
                "document_content_sha256": candidate_document[
                    "document_content_sha256"
                ],
                **candidate_document["audit"],
            },
        },
    }
    manifest_path = output_dir / "manifest.json"
    write_json(manifest_path, manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
