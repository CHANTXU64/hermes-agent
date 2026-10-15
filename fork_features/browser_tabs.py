"""Explicit tab management for the agent-browser backend.

The driver owns tab identity and selection. IDs include a driver-session namespace:
a stale short ID must never silently address a different page after a reconnect.
"""
from __future__ import annotations

import functools
import hashlib
import json
import re
import threading
import weakref
from typing import Any

NATIVE_ID = re.compile(r"t[1-9][0-9]*\Z")
_locks = weakref.WeakValueDictionary()
_locks_guard = threading.Lock()


def task_lock(task_id: str):
    from hermes_constants import hermes_home_key
    key = (hermes_home_key(), task_id)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


def serialize_navigation(fn):
    @functools.wraps(fn)
    def call(url, task_id=None, tab_id=None):
        with task_lock(task_id or "default"):
            return fn(url, task_id=task_id, tab_id=tab_id)
    return call


def public_id(info: dict, native_id: str) -> str:
    """Namespace IDs by the actual daemon lifetime, even if its name is reused."""
    identity = info["session_name"] + ":" + info["_tab_daemon_identity"]
    namespace = hashlib.sha256(identity.encode()).hexdigest()[:12]
    return f"{namespace}:{native_id}"


def resolve_id(tab_id: str | None, info: dict, tabs: list[dict]) -> str:
    for tab in tabs:
        native_id = tab.get("tabId", "")
        if NATIVE_ID.fullmatch(native_id) and tab_id == public_id(info, native_id):
            return native_id
    raise ValueError("Unknown or expired tab_id. List browser_tabs again; no other tab was selected.")


def driver_tabs(task_id: str) -> tuple[dict, list[dict]]:
    from tools import browser_tool_session as session
    from fork_features.browser_daemon import daemon_identity
    info = session._get_session_info(task_id)
    try:
        identity, _ = daemon_identity(info)
    except RuntimeError:
        # Only a read may bootstrap a missing driver. Never bootstrap a mutation.
        bootstrap = session._run_browser_command(task_id, "tab", ["list"])
        if not bootstrap.get("success"):
            raise RuntimeError(bootstrap.get("error", "Could not start browser daemon"))
        info = session._get_session_info(task_id)
        identity, _ = daemon_identity(info)
    result = session._run_browser_command(task_id, "tab", ["list"],
        expected_session_name=info["session_name"], expected_daemon_identity=identity)
    if session._get_session_info(task_id).get("session_name") != info.get("session_name"):
        raise RuntimeError("Browser control session changed while listing; list browser_tabs again.")
    if not result.get("success"):
        raise RuntimeError(result.get("error", "Could not list browser tabs"))
    tabs = result.get("data", {}).get("tabs")
    if not isinstance(tabs, list) or any(not NATIVE_ID.fullmatch(str(t.get("tabId", ""))) for t in tabs):
        raise RuntimeError("This browser driver does not expose stable tab IDs.")
    return dict(info, _tab_daemon_identity=identity), tabs


def list_tabs(task_id: str) -> dict[str, Any]:
    from fork_features.browser_tabs_chrome import selection_states
    info, tabs = driver_tabs(task_id)
    states = selection_states(info, tabs)
    return {"success": True, "selection_note": "agent_active is this task’s control target. chrome_active is a read-only observation of the selected tab in each Chrome window, not OS focus; null means unknown (including duplicate URLs, minimized windows or unavailable CDP).", "tabs": [
        {"id": public_id(info, tab["tabId"]),
         "title": tab.get("title", ""), "url": tab.get("url", ""),
         "agent_active": tab.get("active", False),
         **states.get(tab["tabId"], {"chrome_active": None, "window_id": None})}
        for tab in tabs
    ]}


def select_tab(task_id: str, tab_id: str | None, *, for_navigation: bool = False) -> dict:
    """Resolve and pin the tab before selecting it.

    Navigation callers must validate the destination before selecting and check
    redirects afterwards. They are leaving the old document, not accessing it;
    only a standalone switch applies policy to that document's current URL.
    Identity, existence and daemon-generation checks apply to both operations.
    """
    from tools import browser_tool as bt, browser_tool_session as session
    try:
        info, tabs = driver_tabs(task_id)
        native_id = resolve_id(tab_id, info, tabs)
        if not for_navigation:
            target = next(t for t in tabs if t["tabId"] == native_id)
            error = bt._url_policy_error(target.get("url", ""), auto_local=bt._is_local_sidecar_key(task_id))
            if error:
                return error
        result = session._run_browser_command(task_id, "tab", [native_id], expected_session_name=info["session_name"],
            expected_daemon_identity=info["_tab_daemon_identity"])
        if not result.get("success"):
            return {"success": False, "error": result.get("error", "Tab selection failed")}
        if result.get("data", {}).get("tabId") != native_id:
            return {"success": False, "error": "Driver did not confirm the requested tab."}
        return {"success": True, "daemon_identity": info["_tab_daemon_identity"]}
    except (ValueError, RuntimeError) as exc:
        return {"success": False, "error": str(exc)}


def _new_tab(task_id: str, url: str | None) -> dict:
    from tools import browser_tool as bt, browser_tool_session as session
    if url is not None:
        url, error = bt._secret_url_error_normalized(url)
        if error:
            return error
        error = bt._url_policy_error(url, auto_local=bt._is_local_sidecar_key(task_id))
        if error:
            return error
    info, _ = driver_tabs(task_id)
    result = session._run_browser_command(task_id, "tab", ["new"], expected_session_name=info["session_name"],
            expected_daemon_identity=info["_tab_daemon_identity"])
    if not result.get("success"):
        return result
    native = result.get("data", {}).get("tabId", "")
    if not NATIVE_ID.fullmatch(native):
        return {"success": False, "error": "Driver created a tab without returning its stable ID; not retried."}
    tab_id = public_id(info, native)
    if url is not None:
        result = json.loads(bt.browser_navigate(url, task_id=task_id, tab_id=tab_id))
        result["tab_id"] = tab_id
        return result
    return {"success": True, "tab_id": tab_id, "url": "about:blank"}


def _close_tab(task_id: str, tab_id: str | None) -> dict:
    from tools import browser_tool_session as session
    info, tabs = driver_tabs(task_id)
    native_id = resolve_id(tab_id, info, tabs)
    result = session._run_browser_command(task_id, "tab", ["close", native_id], expected_session_name=info["session_name"],
            expected_daemon_identity=info["_tab_daemon_identity"])
    if not result.get("success"):
        return result
    _, remaining = driver_tabs(task_id)
    if any(t["tabId"] == native_id for t in remaining):
        return {"success": False, "error": "Tab closure was not confirmed; not retried."}
    return {"success": True, "closed": True, "tab_id": tab_id}


def _browser_tabs(action: str = "list", tab_id: str | None = None,
                 url: str | None = None, task_id: str | None = None) -> str:
    from tools import browser_tool as bt
    key = bt._last_session_key(task_id or "default")
    try:
        if bt._is_camofox_mode():
            raise ValueError("Explicit tabs require the agent-browser backend; Camofox is not supported.")
        if action == "list":
            result = list_tabs(key)
        elif action == "new":
            result = _new_tab(key, url)
        elif action == "switch":
            result = select_tab(key, tab_id)
            if result.get("success"):
                result = {"success": True, "tab_id": tab_id}
        elif action == "close":
            result = _close_tab(key, tab_id)
        else:
            raise ValueError("Unsupported tab action")
        if result.get("success") and action in {"new", "switch"}:
            bt._last_active_session_key[task_id or "default"] = key
        return bt._dumps(result)
    except (ValueError, RuntimeError) as exc:
        return json.dumps({"success": False, "error": str(exc)})


def browser_tabs(action="list", tab_id=None, url=None, task_id=None):
    with task_lock(task_id or "default"):
        return _browser_tabs(action, tab_id, url, task_id)


SCHEMA = {
    "name": "browser_tabs",
    "description": "List browser tabs with short IDs, titles, URLs, the agent's selected tab and Chrome window selection (null means unknown). IDs are scoped to this browser control session; list again after a reconnect. Listing does not navigate. New creates and selects a tab; optional url navigates it. Switch selects without reloading. Close closes only the named tab; list again afterward before further page actions.",
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["list", "new", "switch", "close"], "default": "list"},
        "tab_id": {"type": "string", "description": "Exact short ID from browser_tabs; required for switch and close."},
        "url": {"type": "string", "description": "Optional initial URL for a new tab; defaults to about:blank."},
    }, "required": []},
}
