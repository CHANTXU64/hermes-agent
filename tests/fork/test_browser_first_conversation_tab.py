"""Fork regression for the first browser navigation in a conversation."""

import json
import threading
import uuid

import tools.browser_tool as browser_tool


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

    monkeypatch.setattr(browser_tool, "_run_browser_command", fake_run)
    # Return a fresh backend session on every invocation, matching per-turn
    # browser cleanup while keeping the Hermes conversation task_id stable.
    monkeypatch.setattr(
        browser_tool,
        "_get_session_info",
        lambda _key: {"_first_nav": True, "features": {"local": True, "proxies": True}},
    )
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser_tool, "_is_local_backend", lambda: True)
    monkeypatch.setattr(browser_tool, "_is_local_sidecar_key", lambda _key: False)
    monkeypatch.setattr(browser_tool, "_navigation_session_key", lambda task_id, _url: task_id)
    monkeypatch.setattr(browser_tool, "_maybe_start_recording", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_tool, "check_website_access", lambda _url: None)
    return calls


def test_description_states_first_conversation_navigation_behavior():
    description = _description("browser_navigate")

    assert (
        "On the first call in a new conversation, it automatically opens a new "
        "tab and switches to it before loading the URL."
    ) in description


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

    monkeypatch.setattr(browser_tool, "_run_browser_command", fake_run)

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

    monkeypatch.setattr(browser_tool, "_run_browser_command", fake_run)

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
