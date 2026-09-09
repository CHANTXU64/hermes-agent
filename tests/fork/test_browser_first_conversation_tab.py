"""Fork regression for the first browser navigation in a conversation."""

import json
import threading
import uuid

import tools.browser_tool as browser_tool
from fork_features import browser_first_navigation


def _description(tool_name):
    return next(
        schema["description"]
        for schema in browser_tool.BROWSER_TOOL_SCHEMAS
        if schema["name"] == tool_name
    )


def _install_navigation_harness(monkeypatch):
    calls = []

    def fake_run(task_id, command, args=None, timeout=None, **kwargs):
        calls.append((task_id, command, list(args or [])))
        if command == "open":
            assert args
            return {
                "success": True,
                "data": {"title": "ok", "url": args[0]},
            }
        if command == "snapshot":
            return {
                "success": True,
                "data": {"snapshot": "page", "refs": {}},
            }
        return {"success": True, "data": {}}

    monkeypatch.setattr(browser_tool._session, "_run_browser_command", fake_run)
    monkeypatch.setattr(
        browser_tool._session,
        "_get_session_info",
        lambda _key: {"_first_nav": True, "features": {"local": True, "proxies": True}},
    )
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser_tool, "_is_local_sidecar_key", lambda _key: False)
    monkeypatch.setattr(browser_tool, "_navigation_session_key", lambda task_id, _url: task_id)
    monkeypatch.setattr(browser_tool, "_maybe_start_recording", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_tool, "_secret_url_error_normalized", lambda url: (url, None))
    monkeypatch.setattr(browser_tool, "_url_policy_error", lambda url, auto_local=False: None)
    return calls


def test_description_states_first_conversation_navigation_behavior():
    description = _description("browser_navigate")

    assert browser_first_navigation.FIRST_NAVIGATION_DESCRIPTION in description


def test_browser_tool_uses_fork_owned_navigation_policy():
    """Core keeps only stable aliases; state and policy live in fork_features."""
    assert (
        browser_tool.serialize_conversation_navigation
        is browser_first_navigation.serialize_conversation_navigation
    )
    assert (
        browser_tool.ensure_first_conversation_tab
        is browser_first_navigation.ensure_first_conversation_tab
    )
    assert not hasattr(browser_tool, "_conversation_tab_initialized")
    assert not hasattr(browser_tool, "_conversation_navigation_locks")


def test_policy_tracks_conversations_independently():
    calls = []

    def fake_run(session_key, command, args=None, timeout=None, **kwargs):
        calls.append((session_key, command, list(args or []), timeout))
        return {"success": True, "data": {}}

    first_task = f"policy-a-{uuid.uuid4().hex}"
    second_task = f"policy-b-{uuid.uuid4().hex}"

    assert browser_first_navigation.ensure_first_conversation_tab(
        task_id=first_task,
        session_key=f"{first_task}::browser",
        run_command=fake_run,
        timeout=17,
    ) is None
    assert browser_first_navigation.ensure_first_conversation_tab(
        task_id=second_task,
        session_key=f"{second_task}::browser",
        run_command=fake_run,
        timeout=19,
    ) is None
    assert browser_first_navigation.ensure_first_conversation_tab(
        task_id=first_task,
        session_key=f"{first_task}::browser",
        run_command=fake_run,
        timeout=23,
    ) is None

    assert calls == [
        (f"{first_task}::browser", "tab", ["new"], 17),
        (f"{second_task}::browser", "tab", ["new"], 19),
    ]


def test_only_first_navigate_in_conversation_opens_and_switches_to_new_tab(monkeypatch):
    calls = _install_navigation_harness(monkeypatch)
    task_id = f"conversation-{uuid.uuid4().hex}"

    first = json.loads(
        browser_tool.browser_navigate("https://example.com/first", task_id=task_id)
    )
    second = json.loads(
        browser_tool.browser_navigate("https://example.com/second", task_id=task_id)
    )

    assert first["success"] is True
    assert second["success"] is True
    navigation_calls = [
        (command, args)
        for called_task_id, command, args in calls
        if called_task_id == task_id and command in {"tab", "open"}
    ]
    assert navigation_calls == [
        ("tab", ["new"]),
        ("open", ["https://example.com/first"]),
        ("open", ["https://example.com/second"]),
    ]


def test_same_conversation_navigations_do_not_overlap(monkeypatch):
    _install_navigation_harness(monkeypatch)
    task_id = f"conversation-{uuid.uuid4().hex}"
    calls = []
    calls_lock = threading.Lock()
    first_open_started = threading.Event()
    second_open_started = threading.Event()
    first_open_overlapped = []
    results = []
    errors = []

    def fake_run(called_task_id, command, args=None, timeout=None, **kwargs):
        with calls_lock:
            calls.append((called_task_id, command, list(args or [])))
        if command == "open":
            assert args
            if args[0].endswith("/first"):
                first_open_started.set()
                first_open_overlapped.append(second_open_started.wait(timeout=0.25))
            else:
                second_open_started.set()
            return {
                "success": True,
                "data": {"title": "ok", "url": args[0]},
            }
        if command == "snapshot":
            return {
                "success": True,
                "data": {"snapshot": "page", "refs": {}},
            }
        return {"success": True, "data": {}}

    monkeypatch.setattr(browser_tool._session, "_run_browser_command", fake_run)

    def navigate(url):
        try:
            results.append(json.loads(browser_tool.browser_navigate(url, task_id=task_id)))
        except BaseException as exc:  # surface worker failures in the main test thread
            errors.append(exc)

    first_thread = threading.Thread(
        target=navigate,
        args=("https://example.com/first",),
    )
    second_thread = threading.Thread(
        target=navigate,
        args=("https://example.com/second",),
    )

    first_thread.start()
    assert first_open_started.wait(timeout=1)
    second_thread.start()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert len(results) == 2
    assert all(result["success"] is True for result in results)
    assert first_open_overlapped == [False]
    navigation_calls = [
        (command, args)
        for called_task_id, command, args in calls
        if called_task_id == task_id and command in {"tab", "open"}
    ]
    assert navigation_calls == [
        ("tab", ["new"]),
        ("open", ["https://example.com/first"]),
        ("open", ["https://example.com/second"]),
    ]


def test_failed_tab_creation_does_not_open_or_mark_conversation(monkeypatch):
    _install_navigation_harness(monkeypatch)
    task_id = f"conversation-{uuid.uuid4().hex}"
    calls = []
    tab_attempts = 0

    def fake_run(called_task_id, command, args=None, timeout=None, **kwargs):
        nonlocal tab_attempts
        calls.append((called_task_id, command, list(args or [])))
        if command == "tab":
            tab_attempts += 1
            if tab_attempts == 1:
                return {"success": False, "error": "tab creation failed"}
            return {"success": True, "data": {}}
        if command == "open":
            assert args
            return {
                "success": True,
                "data": {"title": "ok", "url": args[0]},
            }
        if command == "snapshot":
            return {
                "success": True,
                "data": {"snapshot": "page", "refs": {}},
            }
        return {"success": True, "data": {}}

    monkeypatch.setattr(browser_tool._session, "_run_browser_command", fake_run)

    failed = json.loads(
        browser_tool.browser_navigate("https://example.com/first", task_id=task_id)
    )
    assert failed == {"success": False, "error": "tab creation failed"}
    assert [command for _task, command, _args in calls] == ["tab"]

    retried = json.loads(
        browser_tool.browser_navigate("https://example.com/second", task_id=task_id)
    )
    assert retried["success"] is True
    navigation_calls = [
        (command, args)
        for called_task_id, command, args in calls
        if called_task_id == task_id and command in {"tab", "open"}
    ]
    assert navigation_calls == [
        ("tab", ["new"]),
        ("tab", ["new"]),
        ("open", ["https://example.com/second"]),
    ]
