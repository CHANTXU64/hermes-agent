"""Fork-owned JSONL audit sink for built-in memory governance.

This module owns audit persistence and bounded lineage semantics.  It does not
import the host memory tool or know how MEMORY.md / USER.md mutations execute.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from utils import atomic_write_text

MEMORY_CHANGELOG_FILENAME = "MEMORY_CHANGELOG.jsonl"
MEMORY_CHANGELOG_SCHEMA_VERSION = 1
MEMORY_HISTORY_MAX_CHARS = 8000


class MemoryAuditSink:
    """Profile-scoped audit reader and bounded lineage resolver."""

    def __init__(
        self,
        memory_dir: Path,
        *,
        read_entries: Optional[Callable[[Path], List[str]]] = None,
        atomic_writer: Callable[..., None] = atomic_write_text,
    ) -> None:
        self.memory_dir = memory_dir
        self._read_entries = read_entries
        self._atomic_writer = atomic_writer

    @property
    def path(self) -> Path:
        return self.memory_dir / MEMORY_CHANGELOG_FILENAME

    @staticmethod
    def jsonl_text(records: List[Dict[str, Any]]) -> str:
        """Serialize records as one compact JSON object per line."""
        if not records:
            return ""
        return "\n".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            for record in records
        ) + "\n"

    def baseline_records(self) -> List[Dict[str, Any]]:
        """Create an honest structured baseline for pre-governance entries."""
        if self._read_entries is None:
            raise RuntimeError("Memory audit baseline requires a Store reader.")
        created = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        records: List[Dict[str, Any]] = []
        for target, filename in (("memory", "MEMORY.md"), ("user", "USER.md")):
            entries = self._read_entries(self.memory_dir / filename)
            for index, entry in enumerate(entries, 1):
                records.append(
                    {
                        "schema_version": MEMORY_CHANGELOG_SCHEMA_VERSION,
                        "event_type": "baseline",
                        "event_id": f"BASELINE-{target.upper()}-{index:03d}",
                        "timestamp": created,
                        "transaction_id": None,
                        "target": target,
                        "action": "baseline",
                        "change_type": "baseline",
                        "deletion_type": None,
                        "reason": (
                            "Legacy entry already existed when change-log governance was "
                            "introduced; its original source was not reconstructed here."
                        ),
                        "evidence": "Current on-disk entry at baseline creation.",
                        "related_skill": None,
                        "loss_note": None,
                        "before": None,
                        "after": entry,
                    }
                )
        return records

    def initialize(self) -> Path:
        """Create the profile JSONL once, preserving existing entries as baseline."""
        if self.path.exists():
            return self.path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_writer(
            self.path,
            self.jsonl_text(self.baseline_records()),
            tmp_prefix=".memlog_",
        )
        return self.path

    @staticmethod
    def format_change_records(
        target: str, traces: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        transaction_id = (
            f"TXN-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        )
        records: List[Dict[str, Any]] = []
        for trace in traces:
            records.append(
                {
                    "schema_version": MEMORY_CHANGELOG_SCHEMA_VERSION,
                    "event_type": "change",
                    "event_id": (
                        f"MEM-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-"
                        f"{uuid.uuid4().hex[:8]}"
                    ),
                    "timestamp": now,
                    "transaction_id": transaction_id,
                    "target": target,
                    "action": trace.get("action"),
                    "change_type": trace.get("change_type"),
                    "deletion_type": trace.get("deletion_type"),
                    "reason": trace.get("reason"),
                    "evidence": trace.get("evidence"),
                    "related_skill": trace.get("related_skill"),
                    "loss_note": trace.get("loss_note"),
                    "before": trace.get("before"),
                    "after": trace.get("after"),
                }
            )
        return records

    def append(self, target: str, traces: List[Dict[str, Any]]) -> None:
        """Atomically append structured mutation records to the JSONL log."""
        if not traces:
            return
        # Refuse to extend a structurally corrupt log. The policy layer rolls
        # back the already-written memory transaction.
        self.load_records()
        raw = self.path.read_text(encoding="utf-8")
        if raw and not raw.endswith("\n"):
            raw += "\n"
        updated = raw + self.jsonl_text(self.format_change_records(target, traces))
        self._atomic_writer(self.path, updated, tmp_prefix=".memlog_")

    def load_records(self) -> List[Dict[str, Any]]:
        """Parse the JSONL log without exposing unrelated records to the model."""
        if not self.path.exists():
            return []
        raw = self.path.read_text(encoding="utf-8")
        records: List[Dict[str, Any]] = []
        for line_number, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid memory changelog JSON on line {line_number}: {exc.msg}"
                ) from exc
            if not isinstance(record, dict):
                raise ValueError(
                    f"Invalid memory changelog record on line {line_number}: expected an object."
                )
            records.append(record)
        return records

    @staticmethod
    def related_history_records(
        records: List[Dict[str, Any]], target: str, current_entry: str
    ) -> List[Dict[str, Any]]:
        """Follow the current producer backward without crossing reuse boundaries."""
        selected: set[int] = set()
        pending = [(current_entry, len(records))]
        visited: set[tuple[str, int]] = set()
        while pending:
            text, upper_bound = pending.pop()
            if (text, upper_bound) in visited:
                continue
            visited.add((text, upper_bound))

            producer = None
            for index in range(upper_bound - 1, -1, -1):
                record = records[index]
                if record.get("target") == target and record.get("after") == text:
                    producer = index
                    break
            if producer is None:
                continue

            producer_record = records[producer]
            transaction_id = producer_record.get("transaction_id")
            if transaction_id and producer_record.get("change_type") == "merge":
                group = [
                    index
                    for index in range(upper_bound)
                    if records[index].get("target") == target
                    and records[index].get("transaction_id") == transaction_id
                    and records[index].get("change_type") == "merge"
                ]
            else:
                group = [producer]
            selected.update(group)
            earlier_than = min(group)
            for index in group:
                before = records[index].get("before")
                if isinstance(before, str):
                    pending.append((before, earlier_than))
        return [records[index] for index in sorted(selected)]

    @staticmethod
    def history_record_for_model(
        record: Dict[str, Any],
    ) -> tuple[Dict[str, Any], bool]:
        """Threat-scan and field-bound one history record before returning it."""
        from tools.threat_patterns import scan_for_threats

        findings = scan_for_threats(
            json.dumps(record, ensure_ascii=False),
            scope="strict",
        )
        if findings:
            metadata: Dict[str, Any] = {}
            for key in (
                "schema_version",
                "event_type",
                "event_id",
                "timestamp",
                "target",
                "action",
                "change_type",
            ):
                value = record.get(key)
                metadata[key] = (
                    value[:120] + "…[field truncated]"
                    if isinstance(value, str) and len(value) > 120
                    else value
                )
            metadata["blocked"] = (
                "Record contained threat pattern(s): " + ", ".join(findings)
            )
            return metadata, True

        limits = {
            "before": 800,
            "after": 800,
            "reason": 400,
            "evidence": 400,
            "loss_note": 400,
            "related_skill": 200,
        }
        bounded: Dict[str, Any] = {}
        truncated = False
        for key, value in record.items():
            limit = limits.get(key, 120)
            if isinstance(value, str) and len(value) > limit:
                bounded[key] = value[:limit] + "…[field truncated]"
                truncated = True
            else:
                bounded[key] = value

        if len(json.dumps([bounded], ensure_ascii=False)) > MEMORY_HISTORY_MAX_CHARS:
            bounded = {
                key: bounded.get(key)
                for key in (
                    "schema_version",
                    "event_type",
                    "event_id",
                    "timestamp",
                    "target",
                    "action",
                    "change_type",
                )
            }
            bounded["record_truncated"] = True
            truncated = True
        return bounded, truncated

    @classmethod
    def bounded_history(
        cls,
        records: List[Dict[str, Any]],
        max_chars: int = MEMORY_HISTORY_MAX_CHARS,
    ) -> tuple[List[Dict[str, Any]], bool]:
        prepared: List[Dict[str, Any]] = []
        truncated = False
        for record in records:
            safe_record, field_truncated = cls.history_record_for_model(record)
            prepared.append(safe_record)
            truncated = truncated or field_truncated

        if len(json.dumps(prepared, ensure_ascii=False)) <= max_chars:
            return prepared, truncated

        # Preserve the origin plus as many newest changes as fit.
        chosen_indices = [0] if prepared else []
        for index in range(len(prepared) - 1, 0, -1):
            candidate = [prepared[i] for i in sorted(chosen_indices + [index])]
            if len(json.dumps(candidate, ensure_ascii=False)) <= max_chars:
                chosen_indices.append(index)
        bounded = [prepared[index] for index in sorted(set(chosen_indices))]
        return bounded, True

    def history_for(
        self, target: str, current_entry: str
    ) -> tuple[List[Dict[str, Any]], int, bool]:
        records = self.load_records()
        related = self.related_history_records(records, target, current_entry)
        history, truncated = self.bounded_history(related)
        return history, len(related), truncated
