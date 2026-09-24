#!/usr/bin/env python3
"""Memory Tool - persistent curated memory (MEMORY.md = agent notes, USER.md = user
profile). Both enter the system prompt as a FROZEN snapshot at session start;
mid-session writes hit disk but never change the prompt (prefix cache intact).
Single `memory` tool: add/replace/remove or a batch `operations` list."""

import copy
import json
import logging
from contextvars import ContextVar
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Dict, Any, List, Optional, Tuple

from utils import is_truthy_value
from tools.registry import no_cache_check_fn

# fcntl is Unix-only; Windows uses msvcrt. MemoryStore reads both lazily from
# this module (tests patch ``memory_tool.fcntl``).
msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt  # noqa: F401
    except ImportError:
        pass

logger = logging.getLogger(__name__)

# One tool-definition pass must use ONE config decision for availability and the
# dynamic target schema: the check_fn result flows to the immediately following
# dynamic_schema_overrides call; ContextVar isolates concurrent profile builds.
_memory_surface_flags: ContextVar[Optional[Tuple[bool, bool]]] = ContextVar("memory_surface_flags", default=None)


def get_memory_dir() -> Path:
    """Profile-scoped memories dir, resolved per call (HERMES_HOME may switch after import)."""
    return get_hermes_home() / "memories"


from tools.memory_tool_store import (  # noqa: E402,F401  (re-exports)
    ENTRY_DELIMITER, MEMORY_BLOCK_HEADERS, MemoryStore, MemoryStoreTransaction, _scan_memory_content)


def load_on_disk_store() -> "MemoryStore":
    """Fresh on-disk MemoryStore with configured limits/flags for contexts with no live
    agent (gateway, Desktop, ``/memory``) so approvals enforce the SAME caps as
    ``agent_init``. Falls back to defaults if config can't load; never raises."""
    try:
        from hermes_cli.config import load_config
        config = load_config() or {}
        mem_cfg = get_builtin_memory_config(config)
        memory_enabled, user_profile_enabled = get_builtin_memory_store_flags(config)
        store = MemoryStore(int(mem_cfg.get("memory_char_limit", 2200)), int(mem_cfg.get("user_char_limit", 1375)),
                            memory_enabled=memory_enabled, user_profile_enabled=user_profile_enabled)
    except Exception:
        store = MemoryStore()  # config optional — fall back to defaults rather than break /memory
    store.load_from_disk()
    return store


def _gate_or_stage(summary: str, detail: str, payload: Dict[str, Any]) -> Optional[str]:
    """JSON tool-result string when the write must NOT proceed (blocked or staged
    for approval), None to proceed. Fails open if the gate module can't load."""
    try:
        from tools import write_approval as wa
    except Exception:
        return None
    decision = wa.evaluate_gate(wa.MEMORY, inline_summary=summary, inline_detail=detail)
    if decision.allow:
        return None
    if decision.blocked:
        return tool_error(decision.message, success=False)
    record = wa.stage_write(wa.MEMORY, payload, summary=f"{summary}: {detail[:120]}", origin=wa.current_origin())
    return json.dumps({"success": True, "staged": True, "pending_id": record["id"], "message": decision.message},
                      ensure_ascii=False)


# action -> (store call, gate (summary, detail) text) for the live tool path and staged replay.
_STORE_ACTIONS = {
    "add": (lambda store, target, content, old_text: store.add(target, content),
            lambda label, content, old_text: (f"add to {label}", content or "")),
    "replace": (lambda store, target, content, old_text: store.replace(target, old_text, content),
                lambda label, content, old_text: (f"replace in {label}",
                                                  f"entry matching: {old_text}\nwhole entry becomes: {content}")),
    "remove": (lambda store, target, content, old_text: store.remove(target, old_text),
               lambda label, content, old_text: (f"remove from {label}", old_text or ""))}


def _batch_op_line(op: Dict[str, Any]) -> str:
    op = op or {}
    act, content, old = op.get("action", "?"), op.get("content") or op.get("new_text") or "", op.get("old_text", "")
    if act == "remove":
        return f"- remove: {old}"
    # Whole-entry contract (#117952): the approver must not read this as a span patch.
    return (f"- replace entry matching '{old}' -> whole entry becomes: {content}" if act == "replace"
            else f"- {act}: {content}")


def _apply_write_gate(action: str, target: str, content: Optional[str], old_text: Optional[str],
                      operations: Optional[List[Dict[str, Any]]] = None,
                      governance: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Gate one mutating op, or (``operations`` set) a whole batch as a single unit."""
    label = "user profile" if target == "user" else "memory"
    extra = dict(governance or {})
    if operations is not None:
        return _gate_or_stage(f"apply {len(operations)} op(s) to {label}",
                              "\n".join(_batch_op_line(op) for op in operations),
                              {"action": "batch", "target": target, "operations": operations, **extra})
    return _gate_or_stage(*_STORE_ACTIONS[action][1](label, content, old_text),
                          {"action": action, "target": target, "content": content, "old_text": old_text, **extra})


def _validate_single_op(store, action, target, content, old_text) -> Optional[str]:
    """Validate BEFORE the gate so an invalid write is rejected now, not at approve time.
    Missing ``old_text`` is recoverable (it can't be schema-required — needs a combinator
    the Codex backend rejects): return the inventory plus a retry instruction."""
    if action == "add" and not content:
        return tool_error("Content is required for 'add' action.", success=False)
    if action in ("replace", "remove") and not old_text:
        replace_hint = (" For 'replace', content is the COMPLETE new entry -- the whole "
                        "matched entry is overwritten, not just the old_text span."
                        if action == "replace" else "")
        return json.dumps({
            "success": False,
            "error": (f"'{action}' needs old_text -- a short unique substring of the entry "
                      f"to {action}. None was provided. Reissue the {action} with old_text "
                      f"set to part of one of the current_entries below.{replace_hint}"),
            "current_entries": store._entries_for(target), "usage": store._usage(target)}, ensure_ascii=False)
    if action == "replace" and not content:
        return tool_error("content is required for 'replace' action.", success=False)
    return None


def _build_memory_governance():
    """Build the Fork policy service against the active profile path."""
    from fork_features.memory_audit import MemoryAuditSink
    from fork_features.memory_governance import MemoryGovernance
    from tools.memory_tool_store import _read_failed_error

    return MemoryGovernance(
        MemoryAuditSink(
            get_memory_dir(),
            read_entries=MemoryStore.read_entries,
        ),
        read_failed_error=_read_failed_error,
    )


_BG_DELETE_ACTIONS = ("replace", "remove")


def _background_delete_gate(action, operations, target="memory", content=None, old_text=None) -> Optional[str]:
    """Fail-closed operation gate for unattended background-review forks (#105921): ``add``
    stays available (it is all any review prompt asks for), while ``replace``/``remove`` —
    single or inside a batch — are never applied unattended. The op is staged in the pending
    store instead of merely denied: the fork's own review summary is never published back, so
    a plain denial would drop the consolidation request with no surfacing path at all. A
    staging failure fails closed to a plain denial."""
    from tools.skill_provenance import is_unattended_review

    if not is_unattended_review():
        return None
    hit = action in _BG_DELETE_ACTIONS or any(
        isinstance(op, dict) and op.get("action") in _BG_DELETE_ACTIONS for op in (operations or []))
    if not hit:
        return None
    payload = ({"action": "batch", "target": target, "operations": operations}
               if operations is not None else
               {"action": action, "target": target, "content": content, "old_text": old_text})
    detail = ("; ".join(_batch_op_line(op) for op in operations) if operations is not None
              else _batch_op_line({"action": action, "content": content, "old_text": old_text}))
    try:
        from tools import write_approval as wa
        record = wa.stage_write(
            wa.MEMORY, payload,
            summary=(f"background review consolidation ({'batch' if operations is not None else action} "
                     f"on {target}): {detail}")[:200],
            origin=wa.current_origin())
        return json.dumps({
            "success": True, "staged": True, "proposal_staged": True, "pending_id": record["id"],
            "message": ("Background review may not delete memory entries unattended. The proposed "
                        f"{'batch' if operations is not None else action} was staged for your approval — "
                        "review it with /memory pending (approve to apply, discard to drop)."),
        }, ensure_ascii=False)
    except Exception:
        logger.warning("Failed to stage background-review consolidation; denying", exc_info=True)
        return tool_error(
            "Background review may not delete memory entries ('replace'/'remove', including in a "
            "batch); 'add' is still available.", success=False)


def memory_tool(action: str = None, target: str = "memory", content: str = None, old_text: str = None,
                reason: str = None, evidence: str = None, change_type: str = None,
                deletion_type: str = None, loss_note: str = None, related_skill: str = None,
                new_text: str = None, operations: Optional[List[Dict[str, Any]]] = None,
                store: Optional[MemoryStore] = None) -> str:
    """Tool entry point; returns a JSON string. Single op (action + content/old_text)
    or batch (``operations``, atomic against the final budget). ``new_text``
    aliases ``content`` -- for 'replace' both mean the COMPLETE new entry (the
    whole matched entry is overwritten; old_text only locates it)."""
    if store is None:
        return tool_error("Memory is not available. It may be disabled in config or this environment.", success=False)
    if content is None and new_text is not None:
        content = new_text
    # Strict providers send JSON null for optional fields; treat as omitted.
    target = "memory" if target is None else target
    target_error = _memory_target_error(store, target)
    if target_error is not None:
        return json.dumps(target_error)
    governance = _build_memory_governance()
    if action == "history":
        return json.dumps(governance.history(store, target, old_text or ""), ensure_ascii=False)
    if operations:
        if not isinstance(operations, list):
            return tool_error("operations must be a list of {action, content?, old_text?} objects.", success=False)
        denied = _background_delete_gate(action, operations, target)
        if denied is not None:
            return denied
        normalized_aliases = []
        for raw_operation in operations:
            operation_alias = dict(raw_operation or {})
            if operation_alias.get("content") is None and operation_alias.get("new_text") is not None:
                operation_alias["content"] = operation_alias["new_text"]
            normalized_aliases.append(operation_alias)
        try:
            governed_operations = governance.normalize_operations(normalized_aliases)
        except ValueError as exc:
            return tool_error(str(exc), success=False)
        gate_result = _apply_write_gate("batch", target, None, None, operations=governed_operations)
        if gate_result is not None:
            return gate_result
        return json.dumps(
            governance.apply(store, target, governed_operations, batch=True), ensure_ascii=False,
        )
    if action not in {"add", "replace", "remove"}:
        return tool_error(f"Unknown action '{action}'. Use: add, replace, remove, history", success=False)
    invalid = _validate_single_op(store, action, target, content, old_text)
    if invalid is not None:
        return invalid
    denied = _background_delete_gate(action, None, target, content, old_text)
    if denied is not None:
        return denied
    operation = {
        "action": action, "content": content, "old_text": old_text, "reason": reason,
        "evidence": evidence, "change_type": change_type, "deletion_type": deletion_type,
        "loss_note": loss_note, "related_skill": related_skill,
    }
    try:
        operation = governance.normalize_operation(operation)
    except ValueError as exc:
        return tool_error(str(exc), success=False)
    gate_result = _apply_write_gate(
        action, target, content, old_text, governance=governance.metadata(operation),
    )
    if gate_result is not None:
        return gate_result
    return json.dumps(governance.apply(store, target, [operation]), ensure_ascii=False)


def get_builtin_memory_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Normalized ``memory`` config section ({} when missing/malformed → flags default to
    enabled). ``agent_init`` reads the same section so availability and store cannot diverge."""
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly
            config = load_config_readonly()
        except Exception:
            logger.debug("Could not read memory config for availability", exc_info=True)
            return {}
    section = config.get("memory") if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


def get_builtin_memory_store_flags(config: Optional[Dict[str, Any]] = None) -> Tuple[bool, bool]:
    """Return ``(memory_enabled, user_profile_enabled)`` from resolved config."""
    section = get_builtin_memory_config(config)
    return tuple(is_truthy_value(section.get(k), default=True) for k in ("memory_enabled", "user_profile_enabled"))


@no_cache_check_fn
def check_memory_requirements() -> bool:
    """Snapshot store flags and report whether the built-in tool is available."""
    _memory_surface_flags.set(None)
    flags = get_builtin_memory_store_flags()
    _memory_surface_flags.set(flags)
    return flags[0] or flags[1]


def _memory_target_error(store: "MemoryStore", target: str) -> Optional[Dict[str, Any]]:
    """Return a shared validation error for an invalid or disabled target."""
    if target not in {"memory", "user"}:
        from tools.registry import _bound_error_text
        return {"success": False,
                "error": _bound_error_text(f"Invalid memory target '{target}'. Use 'memory' or 'user'.")}
    if store.target_enabled(target):
        return None
    label = "USER.md" if target == "user" else "MEMORY.md"
    return {"success": False, "error": f"Built-in {label} writes are disabled in memory config.", "target": target}


def apply_memory_pending(payload: Dict[str, Any], store: "MemoryStore") -> Dict[str, Any]:
    """Replay a staged write against the store, bypassing the gate (/memory approve)."""
    action, target = payload.get("action"), payload.get("target", "memory")
    target_error = _memory_target_error(store, target)
    if target_error is not None:
        return target_error
    if action == "batch":
        return _build_memory_governance().apply(
            store, target, payload.get("operations") or [], batch=True,
        )
    if action in {"add", "replace", "remove"}:
        operation = {
            key: payload.get(key)
            for key in (
                "action", "content", "old_text", "reason", "evidence",
                "change_type", "deletion_type", "loss_note", "related_skill",
            )
        }
        return _build_memory_governance().apply(store, target, [operation])
    return {"success": False, "error": f"Unknown staged action '{action}'."}


from fork_features.memory_governance import build_memory_schema

MEMORY_SCHEMA = build_memory_schema()
_memory_properties = MEMORY_SCHEMA["parameters"]["properties"]
_memory_properties["new_text"] = {
    "type": "string",
    "description": "Alias for content; content wins when both are supplied.",
}
_ops = _memory_properties.get("operations", {}).get("items", {}).get("properties")
if isinstance(_ops, dict):
    _ops["new_text"] = {
        "type": "string",
        "description": "Alias for content in a batch operation.",
    }


# Schema text when only one built-in store is enabled: (target description, TARGETS replacement).
_SINGLE_TARGET_TEXT = {
    ("memory",): ("The enabled built-in store: 'memory' for personal notes.",
                  "TARGET: only 'memory' is enabled for personal notes (environment, conventions, "
                  "tool quirks, lessons)."),
    ("user",): ("The enabled built-in store: 'user' for user profile.",
                "TARGET: only 'user' is enabled for user profile facts (name, role, preferences, style).")}


def _build_memory_schema_overrides() -> Dict[str, Any]:
    """Narrow the advertised target surface using the availability snapshot."""
    flags = _memory_surface_flags.get() or get_builtin_memory_store_flags()
    _memory_surface_flags.set(None)
    targets = [t for t, on in zip(("memory", "user"), flags) if on]
    parameters = copy.deepcopy(MEMORY_SCHEMA["parameters"])
    target_schema, description = parameters["properties"]["target"], MEMORY_SCHEMA["description"]
    target_schema["enum"] = targets
    if narrowed := _SINGLE_TARGET_TEXT.get(tuple(targets)):
        target_schema["description"], replacement = narrowed
        description = description.replace(
            "TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your "
            "notes (environment, conventions, tool quirks, lessons).", replacement)
    return {"description": description, "parameters": parameters}


from tools.registry import registry, tool_error  # noqa: E402  (registration at import time)

registry.register(
    name="memory",
    toolset="memory",
    schema=MEMORY_SCHEMA,
    handler=lambda args, **kw: memory_tool(
        action=args.get("action", ""), target=args.get("target", "memory"), store=kw.get("store"),
        **{k: args.get(k) for k in (
            "content", "old_text", "new_text", "operations",
            "reason", "evidence", "change_type", "deletion_type", "loss_note", "related_skill",
        )}),
    check_fn=check_memory_requirements,
    emoji="🧠",
    dynamic_schema_overrides=_build_memory_schema_overrides)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from contextlib import contextmanager  # noqa: F401,E402
import time  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    'atomic_write_text': ('utils', 'atomic_write_text'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
