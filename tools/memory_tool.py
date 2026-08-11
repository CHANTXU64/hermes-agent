#!/usr/bin/env python3
"""
Memory Tool Module - Persistent Curated Memory

Provides bounded, file-backed memory that persists across sessions. Two stores:
  - MEMORY.md: agent's personal notes and observations (environment facts, project
    conventions, tool quirks, things learned)
  - USER.md: what the agent knows about the user (preferences, communication style,
    expectations, workflow habits)

Both are injected into the system prompt as a frozen snapshot at session start.
Mid-session writes update files on disk immediately (durable) but do NOT change
the system prompt -- this preserves the prefix cache for the entire session.
The snapshot refreshes on the next session start.

Entry delimiter: § (section sign). Entries can be multiline.
Character limits (not tokens) because char counts are model-independent.

Design:
- Single `memory` tool with action parameter: add, replace, remove
- replace/remove use short unique substring matching (not full text or IDs)
- Behavioral guidance lives in the tool schema description
- Frozen snapshot pattern: system prompt is stable, tool responses show live state
"""

import json
import logging
import time
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Dict, Any, List, Optional, Tuple

from utils import atomic_write_text

# fcntl is Unix-only; on Windows use msvcrt for file locking
msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        pass

logger = logging.getLogger(__name__)

# Where memory files live — resolved dynamically so profile overrides
# (HERMES_HOME env var changes) are always respected.  The old module-level
# constant was cached at import time and could go stale if a profile switch
# happened after the first import.
def get_memory_dir() -> Path:
    """Return the profile-scoped memories directory."""
    return get_hermes_home() / "memories"

# Stable header prefixes for the system-prompt memory blocks rendered by
# MemoryStore._render_block. Exported so compression's prompt-retention check
# (agent/conversation_compression.py) can detect a leftover block for a
# target whose entries have since been emptied — keep in lockstep with
# _render_block below.
MEMORY_BLOCK_HEADERS = {
    "memory": "MEMORY (your personal notes)",
    "user": "USER PROFILE (who the user is)",
}

ENTRY_DELIMITER = "\n§\n"
MEMORY_CHANGELOG_FILENAME = "MEMORY_CHANGELOG.jsonl"
MEMORY_CHANGELOG_SCHEMA_VERSION = 1
MEMORY_HISTORY_MAX_CHARS = 8000


def get_memory_changelog_path() -> Path:
    """Return the profile-scoped built-in memory JSONL path."""
    return get_memory_dir() / MEMORY_CHANGELOG_FILENAME


def _jsonl_text(records: List[Dict[str, Any]]) -> str:
    """Serialize records as one compact JSON object per line."""
    if not records:
        return ""
    return "\n".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        for record in records
    ) + "\n"


def _baseline_records() -> List[Dict[str, Any]]:
    """Create an honest structured baseline for pre-governance entries."""
    created = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    records: List[Dict[str, Any]] = []
    for target, filename in (("memory", "MEMORY.md"), ("user", "USER.md")):
        entries = MemoryStore._read_file(get_memory_dir() / filename)
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


def initialize_memory_changelog() -> Path:
    """Create the profile JSONL once, preserving existing entries as baseline."""
    path = get_memory_changelog_path()
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, _jsonl_text(_baseline_records()), tmp_prefix=".memlog_")
    return path


def _normalize_governance_operation(op: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize and validate audit metadata for one public memory mutation."""
    normalized = dict(op or {})
    action = str(normalized.get("action") or "").strip()
    reason = str(normalized.get("reason") or "").strip()
    evidence = str(normalized.get("evidence") or "").strip()
    if not reason:
        raise ValueError(f"{action or 'memory'} requires reason: why this durable change is justified.")
    if not evidence:
        raise ValueError(f"{action or 'memory'} requires evidence: the user statement or verified fact supporting it.")

    if action == "remove":
        deletion_type = str(normalized.get("deletion_type") or "").strip()
        if deletion_type not in {"safe", "expired", "forced_capacity"}:
            raise ValueError(
                "remove requires deletion_type: safe, expired, or forced_capacity."
            )
        if deletion_type == "forced_capacity" and not str(
            normalized.get("loss_note") or ""
        ).strip():
            raise ValueError(
                "forced_capacity removal requires loss_note describing useful meaning that may be lost."
            )

    default_type = {"add": "add", "replace": "replace", "remove": "delete"}.get(
        action, action or "unknown"
    )
    normalized["change_type"] = str(
        normalized.get("change_type") or default_type
    ).strip()
    if normalized["change_type"] not in {
        "add",
        "replace",
        "merge",
        "compress",
        "migrate",
        "delete",
    }:
        raise ValueError(
            "change_type must be add, replace, merge, compress, migrate, or delete."
        )
    normalized["reason"] = reason
    normalized["evidence"] = evidence
    return normalized


def _trace_governance_changes(
    before_entries: List[str], operations: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Recover the exact before/after entry for each successful operation."""
    working = list(before_entries)
    traces: List[Dict[str, Any]] = []
    for op in operations:
        action = op.get("action")
        content = (op.get("content") or "").strip()
        old_text = (op.get("old_text") or "").strip()
        before = None
        after = None
        if action == "add":
            if content in working:
                continue
            working.append(content)
            after = content
        elif action in {"replace", "remove"}:
            matches = [i for i, entry in enumerate(working) if old_text in entry]
            if not matches:
                continue
            index = matches[0]
            before = working[index]
            if action == "replace":
                working[index] = content
                after = content
            else:
                working.pop(index)
        traces.append({**op, "before": before, "after": after})
    return traces


def _format_change_records(
    target: str, traces: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    transaction_id = f"TXN-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
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


# ---------------------------------------------------------------------------
# Memory content scanning — lightweight check for injection/exfiltration
# in content that gets injected into the system prompt.
#
# Patterns live in ``tools/threat_patterns.py`` — the single source of truth
# shared with the context-file scanner and the tool-result delimiter system.
# Memory uses the "strict" scope (broadest pattern set) because:
#  - memory entries are user-curated; the user can rewrite a flagged entry
#  - memory enters the system prompt as a FROZEN snapshot, so a poisoned
#    entry persists for the entire session and across sessions until
#    explicitly removed.
# ---------------------------------------------------------------------------

from tools.threat_patterns import first_threat_message as _first_threat_message


def _scan_memory_content(content: str) -> Optional[str]:
    """Scan memory content for injection/exfil patterns. Returns error string if blocked."""
    return _first_threat_message(content, scope="strict")


def _drift_error(path: "Path", bak_path: str) -> Dict[str, Any]:
    """Build the error dict returned when external drift is detected.

    The on-disk memory file contains content that wouldn't round-trip
    through the tool's parser/serializer — flushing would discard the
    appended/edited content from a patch tool, shell append, manual edit,
    or sister-session write. We refuse the mutation, point the operator at
    the .bak.<ts> snapshot we took, and tell them what to do next.
    """
    return {
        "success": False,
        "error": (
            f"Refusing to write {path.name}: file on disk has content that "
            f"wouldn't round-trip through the memory tool (likely added by "
            f"the patch tool, a shell append, a manual edit, or a "
            f"concurrent session). A snapshot was saved to {bak_path}. "
            f"Resolve the drift first — either rewrite the file as a clean "
            f"§-delimited list of entries, or move the extra content out — "
            f"then retry. This guard exists to prevent silent data loss "
            f"(issue #26045)."
        ),
        "drift_backup": bak_path,
        "remediation": (
            "Open the .bak file, integrate the missing entries into the "
            "memory tool one at a time via memory(action=add, content=...), "
            "then remove or rewrite the original file to a clean state."
        ),
    }


# Sentinel returned by ``_reload_target`` when the target file EXISTS but could
# not be read. Distinct from a drift-backup path (``str``) and from a clean
# reload (``None``): the caller must abort the mutation rather than persist over
# an unreadable file.
_READ_FAILED = object()


def _read_failed_error(path: "Path") -> Dict[str, Any]:
    """Build the error dict returned when the on-disk memory file is unreadable.

    A file that exists but cannot be read is NOT an empty store. Reading it as
    ``[]`` and then persisting would rewrite the whole file from an empty entry
    list — wiping the user's memory. We refuse the write so nothing is lost.
    """
    return {
        "success": False,
        "error": (
            f"Refusing to write {path.name}: the file exists on disk but could "
            f"not be read right now (temporarily locked by another program, a "
            f"permission change, invalid/corrupt text encoding, or a filesystem "
            f"error). Treating an unreadable file as empty and saving would wipe "
            f"existing memory, so the write is refused. Nothing was changed — "
            f"retry in a moment."
        ),
    }


class MemoryStore:
    """
    Bounded curated memory with file persistence. One instance per AIAgent.

    Maintains two parallel states:
      - _system_prompt_snapshot: frozen at load time, used for system prompt injection.
        Never mutated mid-session. Keeps prefix cache stable.
      - memory_entries / user_entries: live state, mutated by tool calls, persisted to disk.
        Tool responses always reflect this live state.
    """

    # After this many failed consolidation attempts (overflow / zero-match) in
    # ONE turn, stop instructing the model to "retry in this turn" and return a
    # terminal "save skipped" result so a fragile replace/add can't loop the
    # turn to budget exhaustion and suppress the user's reply (issue #42405).
    _MAX_CONSOLIDATION_FAILURES_PER_TURN = 3

    def __init__(self, memory_char_limit: int = 2200, user_char_limit: int = 1375):
        self.memory_entries: List[str] = []
        self.user_entries: List[str] = []
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        # Frozen snapshot for system prompt -- set once at load_from_disk()
        self._system_prompt_snapshot: Dict[str, str] = {"memory": "", "user": ""}
        # Per-turn counter of failed at-capacity consolidation attempts; reset
        # at each turn boundary by reset_consolidation_failures() (#42405).
        self._consolidation_failures = 0

    def reset_consolidation_failures(self) -> None:
        """Reset the per-turn consolidation-failure counter (call at turn start)."""
        self._consolidation_failures = 0

    def _consolidation_failure(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Count an at-capacity consolidation failure and degrade gracefully.

        Under the per-turn cap, return ``response`` unchanged (it already tells
        the model how to self-correct + retry in this turn). Once the cap is
        exceeded, drop the retry instruction and return a TERMINAL result so the
        model stops looping memory calls and proceeds to answer the user — a
        failed memory side effect must never block the turn's reply (#42405).
        """
        self._consolidation_failures += 1
        if self._consolidation_failures <= self._MAX_CONSOLIDATION_FAILURES_PER_TURN:
            return response
        return {
            "success": False,
            "done": True,
            "error": (
                f"Memory consolidation failed {self._consolidation_failures} times "
                "this turn. Stop retrying memory calls — leave memory unchanged for "
                "now and continue with your reply to the user. The fact can be saved "
                "in a later turn."
            ),
        }

    def load_from_disk(self):
        """Load entries from MEMORY.md and USER.md, capture system prompt snapshot.

        The frozen snapshot is what enters the system prompt. We scan each
        entry for injection/promptware patterns at snapshot-build time —
        ANY hit replaces the entry text in the snapshot with a placeholder
        like ``[BLOCKED: …]``, so a poisoned-on-disk memory file (supply
        chain, compromised tool, sister-session write) cannot inject into
        the system prompt.

        The live ``memory_entries`` / ``user_entries`` lists keep the
        original text so the user can still SEE poisoned entries via
        see poisoned entries by inspecting the source files directly, and remove them — silently dropping them would hide the attack from the user.

        Scanning is deterministic from disk bytes, so the snapshot remains
        stable for the entire session (prefix-cache invariant holds).
        """
        mem_dir = get_memory_dir()
        mem_dir.mkdir(parents=True, exist_ok=True)

        self.memory_entries = self._read_file(mem_dir / "MEMORY.md")
        self.user_entries = self._read_file(mem_dir / "USER.md")

        # Deduplicate entries (preserves order, keeps first occurrence)
        self.memory_entries = list(dict.fromkeys(self.memory_entries))
        self.user_entries = list(dict.fromkeys(self.user_entries))

        # Sanitize entries for the system-prompt snapshot only.  Live state
        # (memory_entries / user_entries) keeps the raw text so the user
        # can see + remove poisoned entries via the memory tool.
        sanitized_memory = self._sanitize_entries_for_snapshot(self.memory_entries, "MEMORY.md")
        sanitized_user = self._sanitize_entries_for_snapshot(self.user_entries, "USER.md")

        # Capture frozen snapshot for system prompt injection
        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", sanitized_memory),
            "user": self._render_block("user", sanitized_user),
        }

    @staticmethod
    def _sanitize_entries_for_snapshot(entries: List[str], filename: str) -> List[str]:
        """Return ``entries`` with any threat-matching entry replaced by a placeholder.

        Each entry is scanned with the shared threat-pattern library at the
        ``"strict"`` scope (same as memory writes).  On match, the entry is
        replaced in the returned list with ``"[BLOCKED: <filename> entry
        contained threat pattern: <ids>. Removed from system prompt.]"`` —
        the placeholder enters the snapshot, the original entry stays in
        live state for the user to inspect and delete.

        Empty or already-block-marker entries pass through unchanged.
        """
        from tools.threat_patterns import scan_for_threats

        sanitized: List[str] = []
        for entry in entries:
            if not entry or entry.startswith("[BLOCKED:"):
                sanitized.append(entry)
                continue
            findings = scan_for_threats(entry, scope="strict")
            if findings:
                logger.warning(
                    "Memory entry from %s blocked at load time: %s",
                    filename, ", ".join(findings),
                )
                sanitized.append(
                    f"[BLOCKED: {filename} entry contained threat pattern(s): "
                    f"{', '.join(findings)}. Removed from system prompt; "
                    f"use memory(action=remove) "
                    f"to delete the original.]"
                )
            else:
                sanitized.append(entry)
        return sanitized

    @staticmethod
    @contextmanager
    def _file_lock(path: Path):
        """Acquire an exclusive file lock for read-modify-write safety.

        Uses a separate .lock file so the memory file itself can still be
        atomically replaced via os.replace().
        """
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        if fcntl is None and msvcrt is None:
            yield
            return

        fd = open(lock_path, "a+", encoding="utf-8")
        try:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            if fcntl:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except (OSError, IOError):
                    pass
            elif msvcrt:
                try:
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, IOError):
                    pass
            fd.close()

    @staticmethod
    def _path_for(target: str) -> Path:
        mem_dir = get_memory_dir()
        if target == "user":
            return mem_dir / "USER.md"
        return mem_dir / "MEMORY.md"

    def _reload_target(self, target: str, *, skip_drift: bool = False):
        """Re-read entries from disk into in-memory state.

        Called under file lock to get the latest state before mutating.
        Returns the backup path if external drift was detected (the on-disk
        file contains content that wouldn't round-trip through our
        parser/serializer, OR an entry larger than the store's char limit).
        When drift is detected the caller must abort the mutation —
        flushing would discard the un-roundtrippable content.
        Returns ``None`` on clean reload.

        Returns the ``_READ_FAILED`` sentinel when the file EXISTS but could not
        be read. The caller MUST abort: the on-disk entries are unknown, so
        overwriting from an assumed-empty view would wipe them. This is the real
        exposure behind ``add`` — it skips the drift guard because appending is
        safe, but that reasoning only holds when the reload actually saw the
        file. A failed read reported as ``[]`` turned ``add`` into a full-file
        rewrite down to a single entry.

        When *skip_drift* is True the round-trip / entry-size check is
        bypassed.  Used by the ``add`` action which appends without
        rewriting, so existing content is never clobbered.
        """
        path = self._path_for(target)
        raw, read_ok = self._read_raw_checked(path)
        if not read_ok:
            # Leave in-memory entries untouched and tell the caller to abort;
            # persisting over an unreadable file would destroy it.
            return _READ_FAILED
        # Derive BOTH the drift check and the entry parse from the same raw
        # snapshot. The drift guard used to re-read the file itself and treat
        # a failed second read as "no drift" — so a read failure between the
        # checked reload and the drift check let replace/remove/apply_batch
        # rewrite the file from a stale view, silently discarding whatever an
        # external writer had just added. One read, one snapshot, no window.
        bak = None if skip_drift else self._detect_external_drift(target, raw)
        fresh = self._parse_entries(raw)
        fresh = list(dict.fromkeys(fresh))  # deduplicate
        self._set_entries(target, fresh)
        return bak

    def save_to_disk(self, target: str):
        """Persist entries to the appropriate file. Called after every mutation."""
        get_memory_dir().mkdir(parents=True, exist_ok=True)
        self._write_file(self._path_for(target), self._entries_for(target))

    def _entries_for(self, target: str) -> List[str]:
        if target == "user":
            return self.user_entries
        return self.memory_entries

    def _set_entries(self, target: str, entries: List[str]):
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        if not entries:
            return 0
        return len(ENTRY_DELIMITER.join(entries))

    def _char_limit(self, target: str) -> int:
        if target == "user":
            return self.user_char_limit
        return self.memory_char_limit

    def add(
        self, target: str, content: str, *, _lock_held: bool = False
    ) -> Dict[str, Any]:
        """Append a new entry. Returns error if it would exceed the char limit."""
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        # Scan for injection/exfiltration before accepting
        scan_error = _scan_memory_content(content)
        if scan_error:
            return {"success": False, "error": scan_error}

        lock = nullcontext() if _lock_held else self._file_lock(self._path_for(target))
        with lock:
            # Re-read from disk under lock to pick up writes from other sessions.
            # For add (append-only), we skip the drift guard — appending never
            # clobbers existing content, so round-trip mismatches from prior
            # tool-written entries in the same session are harmless.  The drift
            # guard remains active for replace/remove where full-file rewrite
            # would discard un-roundtrippable content (issue #26045).
            #
            # But "append never clobbers" only holds when the reload actually
            # read the file. add rewrites the WHOLE file from the parsed
            # entries, so a file that exists but read as empty (transient lock,
            # permission blip, I/O error) would be rewritten down to just the
            # new entry — wiping every prior memory. Refuse instead.
            if self._reload_target(target, skip_drift=True) is _READ_FAILED:
                return _read_failed_error(self._path_for(target))

            entries = self._entries_for(target)
            limit = self._char_limit(target)

            # Reject exact duplicates
            if content in entries:
                return self._success_response(target, "Entry already exists (no duplicate added).")

            # Calculate what the new total would be
            new_entries = entries + [content]
            new_total = len(ENTRY_DELIMITER.join(new_entries))

            if new_total > limit:
                current = self._char_count(target)
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"Memory at {current:,}/{limit:,} chars. "
                        f"Adding this entry ({len(content)} chars) would exceed the limit. "
                        f"Consolidate now: use 'replace' to merge overlapping entries into "
                        f"shorter ones or 'remove' stale or less important entries (see "
                        f"current_entries below), then retry this add — all in this turn."
                    ),
                    "current_entries": entries,
                    "usage": f"{current:,}/{limit:,}",
                })

            entries.append(content)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry added.")

    def replace(
        self,
        target: str,
        old_text: str,
        new_content: str,
        *,
        _lock_held: bool = False,
    ) -> Dict[str, Any]:
        """Find entry containing old_text substring, replace it with new_content."""
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {"success": False, "error": "new_content cannot be empty. Use 'remove' to delete entries."}

        # Scan replacement content for injection/exfiltration
        scan_error = _scan_memory_content(new_content)
        if scan_error:
            return {"success": False, "error": scan_error}

        lock = nullcontext() if _lock_held else self._file_lock(self._path_for(target))
        with lock:
            bak = self._reload_target(target)
            if bak is _READ_FAILED:
                return _read_failed_error(self._path_for(target))
            if bak:
                return _drift_error(self._path_for(target), bak)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return self._consolidation_failure({
                    "success": False,
                    "error": f"No entry matched '{old_text}'. Check current_entries below and retry with the exact text of the entry you want to replace.",
                    "current_entries": entries,
                })

            if len(matches) > 1:
                # If all matches are identical (exact duplicates), operate on the first one
                unique_texts = {e for _, e in matches}
                if len(unique_texts) > 1:
                    previews = self._previews([e for _, e in matches])
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }
                # All identical -- safe to replace just the first

            idx = matches[0][0]
            limit = self._char_limit(target)

            # Check that replacement doesn't blow the budget
            test_entries = entries.copy()
            test_entries[idx] = new_content
            new_total = len(ENTRY_DELIMITER.join(test_entries))

            if new_total > limit:
                current = self._char_count(target)
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"Replacement would put memory at {new_total:,}/{limit:,} chars. "
                        f"Shorten the new content, or 'remove' other stale or less important "
                        f"entries to make room (see current_entries below), then retry — all "
                        f"in this turn."
                    ),
                    "current_entries": entries,
                    "usage": f"{current:,}/{limit:,}",
                })

            entries[idx] = new_content
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry replaced.")

    def remove(
        self, target: str, old_text: str, *, _lock_held: bool = False
    ) -> Dict[str, Any]:
        """Remove the entry containing old_text substring."""
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        lock = nullcontext() if _lock_held else self._file_lock(self._path_for(target))
        with lock:
            bak = self._reload_target(target)
            if bak is _READ_FAILED:
                return _read_failed_error(self._path_for(target))
            if bak:
                return _drift_error(self._path_for(target), bak)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return self._consolidation_failure({
                    "success": False,
                    "error": f"No entry matched '{old_text}'. Check current_entries below and retry with the exact text of the entry you want to remove.",
                    "current_entries": entries,
                })

            if len(matches) > 1:
                # If all matches are identical (exact duplicates), remove the first one
                unique_texts = {e for _, e in matches}
                if len(unique_texts) > 1:
                    previews = self._previews([e for _, e in matches])
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }
                # All identical -- safe to remove just the first

            idx = matches[0][0]
            entries.pop(idx)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry removed.")

    def apply_batch(
        self,
        target: str,
        operations: List[Dict[str, Any]],
        *,
        _lock_held: bool = False,
    ) -> Dict[str, Any]:
        """Apply a sequence of add/replace/remove ops to one target atomically.

        All operations are validated and applied against the FINAL budget --
        intermediate overflow is irrelevant. This lets the model free space
        (remove/replace) and add new entries in a SINGLE tool call instead of
        the multi-turn consolidate-then-retry dance that re-sends the whole
        conversation context several times.

        Semantics: all-or-nothing. If any op is malformed, doesn't match, or
        the net result would exceed the char limit, NOTHING is written and an
        error is returned describing the first failure plus the live state.
        """
        if not operations:
            return {"success": False, "error": "operations list is empty."}

        # Scan every add/replace content for injection/exfil BEFORE touching
        # disk -- a single poisoned op rejects the whole batch.
        for i, op in enumerate(operations):
            act = (op or {}).get("action")
            new_content = (op or {}).get("content")
            if act in {"add", "replace"} and new_content:
                scan_error = _scan_memory_content(new_content)
                if scan_error:
                    return {"success": False, "error": f"Operation {i + 1}: {scan_error}"}

        lock = nullcontext() if _lock_held else self._file_lock(self._path_for(target))
        with lock:
            bak = self._reload_target(target)
            if bak is _READ_FAILED:
                return _read_failed_error(self._path_for(target))
            if bak:
                return _drift_error(self._path_for(target), bak)

            # Work on a copy; only commit if the whole batch validates.
            working: List[str] = list(self._entries_for(target))
            limit = self._char_limit(target)

            for i, op in enumerate(operations):
                op = op or {}
                act = op.get("action")
                content = (op.get("content") or "").strip()
                old_text = (op.get("old_text") or "").strip()
                pos = f"Operation {i + 1} ({act or 'unknown'})"

                if act == "add":
                    if not content:
                        return self._batch_error(target, f"{pos}: content is required.")
                    if content in working:
                        continue  # idempotent -- skip duplicate, don't fail the batch
                    working.append(content)

                elif act == "replace":
                    if not old_text:
                        return self._batch_error(target, f"{pos}: old_text is required.")
                    if not content:
                        return self._batch_error(
                            target,
                            f"{pos}: content is required (use action='remove' to delete).",
                        )
                    matches = [j for j, e in enumerate(working) if old_text in e]
                    if not matches:
                        return self._batch_error(target, f"{pos}: no entry matched '{old_text}'.")
                    if len({working[j] for j in matches}) > 1:
                        return self._batch_error(
                            target,
                            f"{pos}: '{old_text}' matched multiple distinct entries -- be more specific.",
                        )
                    working[matches[0]] = content

                elif act == "remove":
                    if not old_text:
                        return self._batch_error(target, f"{pos}: old_text is required.")
                    matches = [j for j, e in enumerate(working) if old_text in e]
                    if not matches:
                        return self._batch_error(target, f"{pos}: no entry matched '{old_text}'.")
                    if len({working[j] for j in matches}) > 1:
                        return self._batch_error(
                            target,
                            f"{pos}: '{old_text}' matched multiple distinct entries -- be more specific.",
                        )
                    working.pop(matches[0])

                else:
                    return self._batch_error(
                        target,
                        f"{pos}: unknown action. Use add, replace, or remove.",
                    )

            # Budget check against the FINAL state only.
            new_total = len(ENTRY_DELIMITER.join(working)) if working else 0
            if new_total > limit:
                current = self._char_count(target)
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"After applying all {len(operations)} operations, memory would be at "
                        f"{new_total:,}/{limit:,} chars -- over the limit. Remove or shorten more "
                        f"entries in the same batch (see current_entries below), then retry."
                    ),
                    "current_entries": self._entries_for(target),
                    "usage": f"{current:,}/{limit:,}",
                })

            # Commit.
            self._set_entries(target, working)
            self.save_to_disk(target)

        return self._success_response(target, f"Applied {len(operations)} operation(s).")

    def _batch_error(self, target: str, message: str) -> Dict[str, Any]:
        """Build a batch-abort error that reports live (uncommitted) state."""
        current = self._char_count(target)
        limit = self._char_limit(target)
        return self._consolidation_failure({
            "success": False,
            "error": message + " No operations were applied (batch is all-or-nothing).",
            "current_entries": self._entries_for(target),
            "usage": f"{current:,}/{limit:,}",
        })

    def format_for_system_prompt(self, target: str) -> Optional[str]:
        """
        Return the frozen snapshot for system prompt injection.

        This returns the state captured at load_from_disk() time, NOT the live
        state. Mid-session writes do not affect this. This keeps the system
        prompt stable across all turns, preserving the prefix cache.

        Returns None if the snapshot is empty (no entries at load time).
        """
        block = self._system_prompt_snapshot.get(target, "")
        return block if block else None

    # -- Internal helpers --

    @staticmethod
    def _previews(entries: List[str], width: int = 80) -> List[str]:
        """Truncated one-line previews of entries for error feedback."""
        return [e[:width] + ("..." if len(e) > width else "") for e in entries]

    def _success_response(self, target: str, message: str = None) -> Dict[str, Any]:
        # A successful write means the consolidation loop made progress, so the
        # per-turn failure budget resets (the cap counts consecutive failures,
        # not lifetime ones within a turn) (#42405).
        self._consolidation_failures = 0
        entries = self._entries_for(target)
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        # The success response is intentionally TERMINAL: it confirms the write
        # landed and tells the model to stop. We do NOT echo the full entries
        # list here -- dumping it invites the model to "find more to fix" and
        # re-issue the same operations (observed thrash: the correct batch on
        # call 1, then 5 redundant repeats). Entries are only shown on the
        # error/over-budget paths, where the model genuinely needs them to
        # decide what to consolidate.
        resp = {
            "success": True,
            "done": True,
            "target": target,
            "usage": f"{pct}% — {current:,}/{limit:,} chars",
            "entry_count": len(entries),
        }
        if message:
            resp["message"] = message
        resp["note"] = "Write saved. This update is complete — do not repeat it."
        return resp

    def _render_block(self, target: str, entries: List[str]) -> str:
        """Render a system prompt block with header and usage indicator."""
        if not entries:
            return ""

        limit = self._char_limit(target)
        content = ENTRY_DELIMITER.join(entries)
        current = len(content)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        if target == "user":
            header = f"{MEMORY_BLOCK_HEADERS['user']} [{pct}% — {current:,}/{limit:,} chars]"
        else:
            header = f"{MEMORY_BLOCK_HEADERS['memory']} [{pct}% — {current:,}/{limit:,} chars]"

        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"

    @staticmethod
    def _read_raw_checked(path: Path) -> Tuple[str, bool]:
        """Read a memory file's raw text, distinguishing unreadable from empty.

        Returns ``(raw, read_ok)``. ``read_ok`` is False ONLY when the file
        EXISTS but could not be read — an absent file is a clean ``("", True)``.
        Invalid UTF-8 counts as unreadable too: the bytes on disk hold content
        we cannot faithfully round-trip, so a rewrite would corrupt or discard
        it just like a failed read. Read-modify-write callers must treat
        ``read_ok=False`` as "abort" rather than "empty store", or a transient
        read failure would let them persist over — and wipe — the on-disk
        memory (issue #26045 is about the same class: never rewrite a file
        from a view that isn't the real one).

        No file locking needed: _write_file uses atomic rename, so readers
        always see either the previous complete file or the new complete file.
        """
        if not path.exists():
            return "", True
        try:
            # utf-8-sig strips a leading UTF-8 BOM (Notepad-edited memory
            # files on Windows) and is byte-identical to utf-8 otherwise.
            # Plain utf-8 kept U+FEFF glued to the first entry, corrupting
            # matching/dedup for that entry forever (#10878 / PR #10888).
            # Decode errors stay STRICT on purpose: errors="replace" would
            # hand read-modify-write callers a lossy view that a subsequent
            # save persists over the real bytes — the wipe class documented
            # above. Undecodable bytes must surface as read_ok=False.
            return path.read_text(encoding="utf-8-sig"), True
        except (OSError, IOError, UnicodeDecodeError):
            return "", False

    @staticmethod
    def _parse_entries(raw: str) -> List[str]:
        """Split raw memory-file text into stripped, non-empty entries."""
        if not raw.strip():
            return []
        # Use ENTRY_DELIMITER for consistency with _write_file. Splitting by "§"
        # alone would incorrectly split entries that contain "§" in their content.
        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
        return [e for e in entries if e]

    @staticmethod
    def _read_entries_checked(path: Path) -> Tuple[List[str], bool]:
        """Read + parse a memory file, distinguishing unreadable from empty.

        Returns ``(entries, read_ok)`` — see ``_read_raw_checked`` for the
        ``read_ok`` contract.
        """
        raw, read_ok = MemoryStore._read_raw_checked(path)
        if not read_ok:
            return [], False
        return MemoryStore._parse_entries(raw), True

    @staticmethod
    def _read_file(path: Path) -> List[str]:
        """Read a memory file and split into entries (empty list on any error).

        Retained for read-only callers (``load_from_disk``) that build in-memory
        state without persisting; a failed read degrading to ``[]`` there is
        harmless because nothing is written back. Read-modify-write paths use
        ``_read_raw_checked`` so they can refuse to overwrite an unreadable
        file — see ``_reload_target``.
        """
        return MemoryStore._read_entries_checked(path)[0]

    def _detect_external_drift(self, target: str, raw: str) -> Optional[str]:
        """Return a backup-path string if on-disk content shows external drift.

        *raw* is the file content already read by the caller's checked read
        (``_read_raw_checked``). Drift detection MUST operate on that same
        snapshot — an earlier version re-read the file here and treated a
        failed second read as "no drift", which let a mutation proceed from a
        stale first snapshot and rewrite away content an external writer added
        between the two reads.

        The memory file is supposed to be a list of small entries the tool
        wrote, joined by §. Detect drift via two signals:

        1. Round-trip mismatch — re-parsing and re-serializing the file
           doesn't produce identical bytes (rare; would catch oddly-encoded
           delimiters).
        2. Entry-size overflow — any single parsed entry exceeds the
           store's whole-file char limit. The tool budgets the ENTIRE store
           against that limit; no single tool-written entry can exceed it.
           When we see one entry larger than the limit, an external writer
           (patch tool, shell append, manual edit, sister session) appended
           free-form content into what the tool will treat as one entry.
           Flushing would then truncate that entry to the model's new
           content, discarding the appended bytes — issue #26045.

        Returns the absolute path of the .bak file when drift was found and
        backed up; returns None when the file looks tool-shaped.

        Note: this is an INSTANCE method (not static) because we need the
        per-target char_limit for signal #2.
        """
        path = self._path_for(target)
        if not raw.strip():
            return None

        parsed = [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]
        roundtrip = ENTRY_DELIMITER.join(parsed)

        char_limit = self._char_limit(target)
        max_entry_len = max((len(e) for e in parsed), default=0)

        drift_detected = (raw.strip() != roundtrip) or (max_entry_len > char_limit)
        if not drift_detected:
            return None

        # Drift confirmed — snapshot the file so the operator can recover
        # whatever the external writer added, then return the .bak path so
        # the caller can refuse the mutation.
        ts = int(time.time())
        bak_path = path.with_suffix(path.suffix + f".bak.{ts}")
        try:
            bak_path.write_text(raw, encoding="utf-8")
        except (OSError, IOError):
            return str(bak_path) + " (BACKUP FAILED — file unchanged on disk)"
        return str(bak_path)

    @staticmethod
    def _write_file(path: Path, entries: List[str]):
        """Write entries to a memory file using atomic temp-file + rename.

        Previous implementation used open("w") + flock, but "w" truncates the
        file *before* the lock is acquired, creating a race window where
        concurrent readers see an empty file. Atomic rename avoids this:
        readers always see either the old complete file or the new one.
        """
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        try:
            atomic_write_text(path, content, tmp_prefix=".mem_")
        except (OSError, IOError) as e:
            raise RuntimeError(f"Failed to write memory file {path}: {e}")


def load_on_disk_store() -> "MemoryStore":
    """Build a fresh on-disk :class:`MemoryStore`, honoring configured char limits.

    Use this from any context that has no live agent (the messaging gateway, the
    Desktop GUI, the bare CLI ``/memory`` handler) but still needs to read or
    apply approved memory writes. Mirrors how the live agent constructs its store
    in ``agent/agent_init.py`` — including the user's ``memory.memory_char_limit``
    / ``memory.user_char_limit`` overrides — so an approval applied without a live
    agent enforces the SAME caps as one applied with one.

    Falls back to the built-in defaults if config can't be loaded, so this can
    never raise on a missing/unreadable config.
    """
    memory_char_limit = 2200
    user_char_limit = 1375
    try:
        from hermes_cli.config import load_config

        mem_cfg = (load_config() or {}).get("memory", {}) or {}
        memory_char_limit = int(mem_cfg.get("memory_char_limit", memory_char_limit))
        user_char_limit = int(mem_cfg.get("user_char_limit", user_char_limit))
    except Exception:
        pass  # config optional — fall back to defaults rather than break /memory

    store = MemoryStore(
        memory_char_limit=memory_char_limit,
        user_char_limit=user_char_limit,
    )
    store.load_from_disk()
    return store


def _append_governance_records(
    changelog_path: Path, target: str, traces: List[Dict[str, Any]]
) -> None:
    """Atomically append structured mutation records to the JSONL log."""
    if not traces:
        return
    # Refuse to extend a structurally corrupt log; the caller will roll back
    # the already-written memory transaction.
    _load_changelog_records(changelog_path)
    raw = changelog_path.read_text(encoding="utf-8")
    if raw and not raw.endswith("\n"):
        raw += "\n"
    updated = raw + _jsonl_text(_format_change_records(target, traces))
    atomic_write_text(changelog_path, updated, tmp_prefix=".memlog_")


def _load_changelog_records(path: Path) -> List[Dict[str, Any]]:
    """Parse the JSONL log without exposing unrelated records to the model."""
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
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


def _related_history_records(
    records: List[Dict[str, Any]], target: str, current_entry: str
) -> List[Dict[str, Any]]:
    """Follow the current entry's producers backward without crossing reuse boundaries."""
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


def _history_record_for_model(record: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
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
            metadata[key] = value[:120] + "…[field truncated]" if (
                isinstance(value, str) and len(value) > 120
            ) else value
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


def _bounded_history(
    records: List[Dict[str, Any]], max_chars: int = MEMORY_HISTORY_MAX_CHARS
) -> tuple[List[Dict[str, Any]], bool]:
    prepared: List[Dict[str, Any]] = []
    truncated = False
    for record in records:
        safe_record, field_truncated = _history_record_for_model(record)
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


def _memory_history(store: "MemoryStore", target: str, old_text: str) -> Dict[str, Any]:
    """Return only the bounded audit lineage for one current entry."""
    old_text = old_text.strip()
    if not old_text:
        return {"success": False, "error": "old_text is required for history."}
    entries, read_ok = MemoryStore._read_entries_checked(store._path_for(target))
    if not read_ok:
        return _read_failed_error(store._path_for(target))
    matches = [entry for entry in entries if old_text in entry]
    if not matches:
        return {
            "success": False,
            "error": f"No current entry matched '{old_text}'.",
            "current_entries": entries,
        }
    if len(set(matches)) > 1:
        return {
            "success": False,
            "error": f"Multiple current entries matched '{old_text}'. Be more specific.",
            "matches": store._previews(matches),
        }
    current_entry = matches[0]
    try:
        records = _load_changelog_records(get_memory_changelog_path())
    except (OSError, IOError, UnicodeDecodeError, ValueError) as exc:
        return {"success": False, "error": f"Could not read memory history: {exc}"}
    related = _related_history_records(records, target, current_entry)
    history, truncated = _bounded_history(related)
    return {
        "success": True,
        "target": target,
        "current_entry": current_entry,
        "history": history,
        "matched_records": len(related),
        "returned_records": len(history),
        "truncated": truncated,
        "max_chars": MEMORY_HISTORY_MAX_CHARS,
        "note": "Read-only result. Use this evidence before changing the existing entry.",
    }


def _apply_governed_mutation(
    store: "MemoryStore", target: str, operations: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Apply one public memory transaction and journal its exact semantic delta.

    The changelog lock serializes MEMORY and USER transactions through this
    public tool path. If journaling fails after the memory write, restore the
    before-state while still holding that governance lock rather than leaving an
    untracked successful mutation.
    """
    try:
        normalized = [_normalize_governance_operation(op) for op in operations]
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    changelog_path = get_memory_changelog_path()
    with store._file_lock(changelog_path):
        try:
            initialize_memory_changelog()
        except Exception as exc:
            return {
                "success": False,
                "error": f"Memory unchanged because its change log could not be initialized: {exc}",
            }

        target_path = store._path_for(target)
        with store._file_lock(target_path):
            before, read_ok = MemoryStore._read_entries_checked(target_path)
            if not read_ok:
                return {
                    "success": False,
                    "error": (
                        "Memory unchanged because its current contents could not be "
                        "read safely for rollback."
                    ),
                }
            if len(normalized) == 1:
                op = normalized[0]
                action = op.get("action")
                if action == "add":
                    result = store.add(
                        target, op.get("content") or "", _lock_held=True
                    )
                elif action == "replace":
                    result = store.replace(
                        target,
                        op.get("old_text") or "",
                        op.get("content") or "",
                        _lock_held=True,
                    )
                elif action == "remove":
                    result = store.remove(
                        target, op.get("old_text") or "", _lock_held=True
                    )
                else:
                    return {
                        "success": False,
                        "error": f"Unknown action '{action}'. Use: add, replace, remove",
                    }
            else:
                result = store.apply_batch(target, normalized, _lock_held=True)

            if not result.get("success"):
                return result

            # The target lock stays held through journaling. This makes the
            # before/write/log transaction indivisible for every in-process
            # MemoryStore writer that honors the shared lock file.
            after = list(store._entries_for(target))
            if before == after:
                return result
            traces = _trace_governance_changes(before, normalized)
            try:
                _append_governance_records(changelog_path, target, traces)
            except Exception as exc:
                # Manual/non-cooperating writers can ignore the lock. Never
                # restore a stale snapshot over newer bytes in that case.
                current, read_ok = MemoryStore._read_entries_checked(target_path)
                if not read_ok or current != after:
                    if read_ok:
                        store._set_entries(target, current)
                    return {
                        "success": False,
                        "error": (
                            "CRITICAL: the memory write succeeded and its change log "
                            "failed, but the memory file changed again before rollback. "
                            "The newer file was preserved and may contain an unlogged "
                            f"mutation. Original log error: {exc}"
                        ),
                    }
                try:
                    store._write_file(target_path, before)
                    store._set_entries(target, before)
                except Exception as rollback_exc:
                    return {
                        "success": False,
                        "error": (
                            "CRITICAL: the memory write succeeded, its change log failed, "
                            f"and rollback also failed ({rollback_exc}). The memory file may "
                            f"have changed without an audit record. Original log error: {exc}"
                        ),
                    }
                return {
                    "success": False,
                    "error": (
                        "Memory write was rolled back because the change log could "
                        f"not be updated: {exc}"
                    ),
                }
            return result


def _apply_write_gate(action: str, target: str, content: Optional[str],
                      old_text: Optional[str], governance: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Evaluate the memory write gate. Returns a JSON tool-result string when
    the write should NOT proceed normally (blocked or staged), or None when the
    caller should perform the real write.

    Only the mutating actions (add/replace/remove) are gated.
    """
    if action not in {"add", "replace", "remove"}:
        return None

    try:
        from tools import write_approval as wa
    except Exception:
        # If the gate module can't load, fail open (current behaviour) rather
        # than blocking all memory writes.
        return None

    # Build a small inline summary/detail for the foreground approval prompt.
    label = "user profile" if target == "user" else "memory"
    if action == "add":
        summary = f"add to {label}"
        detail = content or ""
    elif action == "replace":
        summary = f"replace in {label}"
        detail = f"old: {old_text}\nnew: {content}"
    else:  # remove
        summary = f"remove from {label}"
        detail = old_text or ""

    decision = wa.evaluate_gate(wa.MEMORY, inline_summary=summary, inline_detail=detail)

    if decision.allow:
        return None

    if decision.blocked:
        return tool_error(decision.message, success=False)

    # stage
    payload = {
        "action": action,
        "target": target,
        "content": content,
        "old_text": old_text,
    }
    payload.update(governance or {})
    record = wa.stage_write(
        wa.MEMORY, payload,
        summary=f"{summary}: {detail[:120]}",
        origin=wa.current_origin(),
    )
    return json.dumps(
        {"success": True, "staged": True, "pending_id": record["id"],
         "message": decision.message},
        ensure_ascii=False,
    )


def _apply_batch_write_gate(target: str, operations: List[Dict[str, Any]]) -> Optional[str]:
    """Evaluate the write gate for a batch of memory operations.

    Returns a JSON tool-result string when the batch should NOT proceed
    (blocked or staged), or None when the caller should perform the real
    batch write. The whole batch is gated as a single unit.
    """
    try:
        from tools import write_approval as wa
    except Exception:
        return None

    label = "user profile" if target == "user" else "memory"
    summary = f"apply {len(operations)} op(s) to {label}"
    detail_lines = []
    for op in operations:
        op = op or {}
        act = op.get("action", "?")
        if act == "remove":
            detail_lines.append(f"- remove: {op.get('old_text', '')}")
        elif act == "replace":
            detail_lines.append(f"- replace: {op.get('old_text', '')} -> {op.get('content', '')}")
        else:
            detail_lines.append(f"- {act}: {op.get('content', '')}")
    detail = "\n".join(detail_lines)

    decision = wa.evaluate_gate(wa.MEMORY, inline_summary=summary, inline_detail=detail)

    if decision.allow:
        return None

    if decision.blocked:
        return tool_error(decision.message, success=False)

    payload = {"action": "batch", "target": target, "operations": operations}
    record = wa.stage_write(
        wa.MEMORY, payload,
        summary=f"{summary}: {detail[:120]}",
        origin=wa.current_origin(),
    )
    return json.dumps(
        {"success": True, "staged": True, "pending_id": record["id"],
         "message": decision.message},
        ensure_ascii=False,
    )


def _missing_old_text_error(store: "MemoryStore", target: str, action: str) -> str:
    """Build a recoverable error for a replace/remove call that arrived without
    ``old_text``.

    ``replace``/``remove`` are inherently targeted -- without ``old_text`` there
    is no entry to act on, so we cannot fulfil the call. But returning a bare
    "old_text is required" is a dead-end: some structured-output clients omit the
    optional ``old_text`` field (it isn't, and can't be, schema-required without
    a top-level combinator the Codex backend rejects -- see
    tests/tools/test_memory_tool_schema.py). So instead we return the current
    entry inventory plus an explicit retry instruction, letting the model reissue
    the call with ``old_text`` set to a unique substring of the entry it means.
    Mirrors the batch path's ``_batch_error`` shape. (issues #43412, #49466)
    """
    entries = store._entries_for(target)
    current = store._char_count(target)
    limit = store._char_limit(target)
    return json.dumps(
        {
            "success": False,
            "error": (
                f"'{action}' needs old_text -- a short unique substring of the entry "
                f"to {action}. None was provided. Reissue the {action} with old_text "
                f"set to part of one of the current_entries below."
            ),
            "current_entries": entries,
            "usage": f"{current:,}/{limit:,}",
        },
        ensure_ascii=False,
    )


def memory_tool(
    action: Optional[str] = None,
    target: str = "memory",
    content: Optional[str] = None,
    old_text: Optional[str] = None,
    reason: Optional[str] = None,
    evidence: Optional[str] = None,
    change_type: Optional[str] = None,
    deletion_type: Optional[str] = None,
    loss_note: Optional[str] = None,
    related_skill: Optional[str] = None,
    operations: Optional[List[Dict[str, Any]]] = None,
    store: Optional[MemoryStore] = None,
) -> str:
    """
    Single entry point for the memory tool. Dispatches to MemoryStore methods.

    Two shapes:
      - Single op: action + (content / old_text).
      - Batch:     operations=[{action, content?, old_text?}, ...] applied
                   atomically against the final char budget in ONE call.

    Returns JSON string with results.
    """
    if store is None:
        return tool_error("Memory is not available. It may be disabled in config or this environment.", success=False)

    # Some strict providers fill optional schema fields with JSON null rather
    # than omitting them.  Treat ``target: null`` as omitted so memory writes
    # still use the documented default store instead of failing validation.
    if target is None:
        target = "memory"

    if target not in {"memory", "user"}:
        return tool_error(f"Invalid target '{target}'. Use 'memory' or 'user'.", success=False)

    if action == "history":
        return json.dumps(
            _memory_history(store, target, old_text or ""),
            ensure_ascii=False,
        )

    # --- Batch path -------------------------------------------------------
    if operations:
        if not isinstance(operations, list):
            return tool_error("operations must be a list of {action, content?, old_text?} objects.", success=False)
        try:
            governed_operations = [
                _normalize_governance_operation(op or {}) for op in operations
            ]
        except ValueError as exc:
            return tool_error(str(exc), success=False)
        gate_result = _apply_batch_write_gate(target, governed_operations)
        if gate_result is not None:
            return gate_result
        result = _apply_governed_mutation(store, target, governed_operations)
        return json.dumps(result, ensure_ascii=False)

    # --- Single-op path ---------------------------------------------------
    # Validate required params BEFORE the gate so an invalid write is rejected
    # immediately instead of being staged and only failing at approve time.
    if action not in {"add", "replace", "remove"}:
        return tool_error(
            f"Unknown action '{action}'. Use: add, replace, remove, history",
            success=False,
        )
    if action == "add" and not content:
        return tool_error("Content is required for 'add' action.", success=False)
    if action == "replace" and (not old_text or not content):
        missing = "old_text" if not old_text else "content"
        if not old_text:
            # The client/model omitted old_text. Replace is inherently targeted
            # -- we can't guess which entry. Return the current inventory plus a
            # retry instruction so the model can reissue with old_text set,
            # instead of hitting a dead-end error. (issues #43412, #49466)
            return _missing_old_text_error(store, target, "replace")
        return tool_error(f"{missing} is required for 'replace' action.", success=False)
    if action == "remove" and not old_text:
        return _missing_old_text_error(store, target, "remove")

    operation = {
        "action": action,
        "content": content,
        "old_text": old_text,
        "reason": reason,
        "evidence": evidence,
        "change_type": change_type,
        "deletion_type": deletion_type,
        "loss_note": loss_note,
        "related_skill": related_skill,
    }
    try:
        operation = _normalize_governance_operation(operation)
    except ValueError as exc:
        return tool_error(str(exc), success=False)

    # Approval gate: when on, stages the write (background/gateway) or prompts
    # inline (interactive CLI); when off (default) passes straight through.
    governance = {
        key: operation.get(key)
        for key in (
            "reason",
            "evidence",
            "change_type",
            "deletion_type",
            "loss_note",
            "related_skill",
        )
        if operation.get(key)
    }
    gate_result = _apply_write_gate(action, target, content, old_text, governance)
    if gate_result is not None:
        return gate_result

    result = _apply_governed_mutation(store, target, [operation])
    return json.dumps(result, ensure_ascii=False)


def check_memory_requirements() -> bool:
    """Memory tool has no external requirements -- always available."""
    return True


def apply_memory_pending(payload: Dict[str, Any], store: "MemoryStore") -> Dict[str, Any]:
    """Replay a staged memory write directly against the store, bypassing the
    write gate. Called by the /memory approve handler.

    Returns the store's result dict.
    """
    action = payload.get("action")
    target = payload.get("target", "memory")
    content = payload.get("content") or ""
    old_text = payload.get("old_text") or ""
    if action == "batch":
        return _apply_governed_mutation(
            store, target, payload.get("operations") or []
        )
    if action in {"add", "replace", "remove"}:
        operation = {
            key: payload.get(key)
            for key in (
                "action",
                "content",
                "old_text",
                "reason",
                "evidence",
                "change_type",
                "deletion_type",
                "loss_note",
                "related_skill",
            )
        }
        return _apply_governed_mutation(store, target, [operation])
    return {"success": False, "error": f"Unknown staged action '{action}'."}
# OpenAI Function-Calling Schema
# =============================================================================

MEMORY_SCHEMA = {
    "name": "memory",
    "description": (
        "Save durable facts to persistent memory that survive across sessions. Memory is "
        "injected into every future turn, so keep entries compact and high-signal.\n\n"
        "HOW: make ALL your changes in ONE call via an 'operations' array (each item: "
        "{action, content?, old_text?, reason, evidence, ...}). Every operation needs a "
        "specific reason and source evidence; the tool records the exact before/after content "
        "in a structured JSONL audit log. The batch applies atomically and the char limit is "
        "checked only on the FINAL result — so a single call can remove/replace stale entries "
        "to free room AND add new ones, even when an add alone would overflow. The response "
        "reports current/limit chars and confirms completion; one batch call finishes the "
        "update, so don't repeat it. Use the bare action/content/old_text fields only for a "
        "single lone change.\n\n"
        "HISTORY: For a pure add, do not read history. Before replacing, merging, compressing, "
        "migrating, or removing an existing entry, call memory(action='history', target=..., "
        "old_text=...) and inspect only its bounded related records; never load the full audit "
        "log into model context.\n\n"
        "WHEN: save proactively when the user states a preference, correction, or personal "
        "detail, or you learn a stable fact about their environment, conventions, or workflow. "
        "Priority: user preferences & corrections > environment facts > procedures. The best "
        "memory stops the user repeating themselves.\n\n"
        "EVIDENCE TYPE: distinguish an actually observed incident or user correction from a "
        "merely preventive concern. Never label an unobserved concern as a lesson, and do not "
        "save generic safety precautions solely because they seem important; normal model "
        "safeguards do not need duplicate memory.\n\n"
        "REPOSITORY RECORDS: do not save implementation designs, architecture notes, or "
        "fork-only behavior already documented in repository docs. Keep only a short "
        "pre-load trigger when it is needed to select the correct Skill before those docs "
        "are read.\n\n"
        "IF FULL: first merge or compress without losing distinct reasons. Then remove entries "
        "that are demonstrably safe or expired. As a last resort, use deletion_type "
        "'forced_capacity' only when the new durable fact is more valuable than every remaining "
        "candidate; provide loss_note so the evicted meaning remains recoverable in the log.\n\n"
        "TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your "
        "notes (environment, conventions, tool quirks, lessons).\n\n"
        "SKIP: trivial/obvious info, easily re-discovered facts, raw data dumps, task progress, "
        "completed-work logs, temporary TODO state (use session_search for those). Reusable "
        "procedures belong in a skill, not memory. Before deleting a possible duplicate, use "
        "skill_view to verify the Skill's actual content and keep any trigger needed before that "
        "Skill normally loads."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "replace", "remove", "history"],
                "description": "Mutation action, or read-only history lookup. Omit when using 'operations'."
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Which memory store: 'memory' for personal notes, 'user' for user profile."
            },
            "content": {
                "type": "string",
                "description": "The entry content. Required for 'add' and 'replace' (single-op shape)."
            },
            "old_text": {
                "type": "string",
                "description": "REQUIRED for 'replace', 'remove', and 'history': a short unique substring identifying the existing entry. Omit only for 'add'."
            },
            "reason": {
                "type": "string",
                "description": "Why this durable change is justified. Required for every mutation."
            },
            "evidence": {
                "type": "string",
                "description": "The explicit user statement or verified fact supporting the change. Required for every mutation."
            },
            "change_type": {
                "type": "string",
                "enum": ["add", "replace", "merge", "compress", "migrate", "delete"],
                "description": "Semantic change kind. Defaults from action; set merge/compress/migrate when applicable."
            },
            "deletion_type": {
                "type": "string",
                "enum": ["safe", "expired", "forced_capacity"],
                "description": "Required for remove: safe, expired, or last-resort forced_capacity."
            },
            "loss_note": {
                "type": "string",
                "description": "Known useful meaning or risk lost. Required for forced_capacity removal."
            },
            "related_skill": {
                "type": "string",
                "description": "Skill actually inspected with skill_view when it carries or relates to this memory."
            },
            "operations": {
                "type": "array",
                "description": (
                    "Batch shape: a list of operations applied atomically in one call "
                    "against the final char budget. Preferred when making multiple changes "
                    "or consolidating to make room. Each item is {action, content?, old_text?}."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                        "content": {"type": "string", "description": "Entry content for add/replace."},
                        "old_text": {"type": "string", "description": "Substring identifying the entry for replace/remove."},
                        "reason": {"type": "string", "description": "Why this operation is justified."},
                        "evidence": {"type": "string", "description": "User statement or verified fact supporting it."},
                        "change_type": {"type": "string", "enum": ["add", "replace", "merge", "compress", "migrate", "delete"]},
                        "deletion_type": {"type": "string", "enum": ["safe", "expired", "forced_capacity"]},
                        "loss_note": {"type": "string", "description": "Required for forced_capacity removal."},
                        "related_skill": {"type": "string", "description": "Skill verified with skill_view, if applicable."},
                    },
                    "required": ["action", "reason", "evidence"],
                },
            },
        },
        "required": ["target"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="memory",
    toolset="memory",
    schema=MEMORY_SCHEMA,
    handler=lambda args, **kw: memory_tool(
        action=args.get("action", ""),
        target=args.get("target", "memory"),
        content=args.get("content"),
        old_text=args.get("old_text"),
        reason=args.get("reason"),
        evidence=args.get("evidence"),
        change_type=args.get("change_type"),
        deletion_type=args.get("deletion_type"),
        loss_note=args.get("loss_note"),
        related_skill=args.get("related_skill"),
        operations=args.get("operations"),
        store=kw.get("store")),
    check_fn=check_memory_requirements,
    emoji="🧠",
)




