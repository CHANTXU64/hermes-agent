"""Regression tests for the execute_code approval-bypass cluster.

Covers the canonical fix for issues #4146, #27303, #30882, #33057:

  1. tools.thread_context.propagate_context_to_thread — propagates the agent
     turn's ContextVars AND thread-local approval/sudo callbacks into worker
     threads, and clears the callbacks on teardown.
  2. Both execute_code RPC threads are wrapped with that helper (source guard).
  3. tools.approval.check_execute_code_guard — the entry-point guard decision
     matrix (isolated backends, yolo/off, cron-deny, headless-local,
     gateway approve/deny/timeout/missing-notify, smart mode).
  4. tools.code_execution_env._scrub_child_env — broad HERMES_ prefix dropped,
     operational allowlist kept, DSN/WEBHOOK blocked, passthrough precedence.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import json
import threading
from types import SimpleNamespace

import pytest

from tools import approval as A
import tools.approval_detection as approval_detection
from tools import approval_context
from tools.thread_context import propagate_context_to_thread
from gateway.session_context import clear_session_vars, reset_session_vars, set_session_vars


# ---------------------------------------------------------------------------
# 1. Context + callback propagation helper
# ---------------------------------------------------------------------------

def test_helper_propagates_contextvar_and_approval_callback():
    from tools import terminal_tool as TT

    probe: contextvars.ContextVar[str] = contextvars.ContextVar(
        "cluster_probe", default="unset"
    )
    probe.set("parent-value")
    sentinel = object()
    TT.set_approval_callback(sentinel)
    try:
        seen: dict = {}

        def worker():
            seen["probe"] = probe.get()
            seen["cb"] = TT._get_approval_callback()

        t = threading.Thread(target=propagate_context_to_thread(worker))
        t.start()
        t.join(timeout=5)

        assert seen["probe"] == "parent-value"  # ContextVar propagated
        assert seen["cb"] is sentinel            # thread-local callback propagated
    finally:
        TT.set_approval_callback(None)


def test_helper_clears_callbacks_on_teardown():
    """A recycled worker thread must not retain the propagated callback after
    the wrapped target finishes (mirrors the GHSA-qg5c-hvr5-hjgr teardown)."""
    from tools import terminal_tool as TT

    sentinel = object()
    TT.set_approval_callback(sentinel)
    try:
        seen: dict = {}

        def first():
            seen["during"] = TT._get_approval_callback()

        def second():  # NOT wrapped — runs on the same recycled worker thread
            seen["after"] = TT._get_approval_callback()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(propagate_context_to_thread(first)).result(timeout=5)
            ex.submit(second).result(timeout=5)

        assert seen["during"] is sentinel  # installed for the wrapped target
        assert seen["after"] is None       # cleared on teardown
    finally:
        TT.set_approval_callback(None)


def test_both_rpc_threads_use_propagation_helper():
    """Source guard: every execute_code RPC serving thread must carry the
    cell's approval context, or the gateway approval bypass (#33057) silently
    returns. The remote poll thread wraps its target with
    propagate_context_to_thread; the local session kernel instead rebinds
    authority per cell (``dispatch=`` passed to ``_rpc_server_loop``)."""
    import inspect
    import tools.code_execution_tool as cet
    import tools.code_kernel as ck

    src = inspect.getsource(cet)
    assert "propagate_context_to_thread(_rpc_poll_loop)" in src, (
        "remote file-RPC poll thread is not wrapped with "
        "propagate_context_to_thread — gateway approval routing will be lost."
    )
    kernel_src = inspect.getsource(ck)
    assert "_rpc_server_loop(" in kernel_src and "dispatch=" in kernel_src, (
        "local session-kernel RPC server thread must pass a per-cell "
        "dispatch= to _rpc_server_loop — gateway approval routing will be lost."
    )


# ---------------------------------------------------------------------------
# 3. check_execute_code_guard decision matrix
# ---------------------------------------------------------------------------

@pytest.fixture
def gw_session(monkeypatch, request):
    """A clean gateway session: HERMES_GATEWAY_SESSION set, a bound session
    key, and isolated gateway queues/callbacks. Yields the session_key."""
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    # Force manual mode regardless of host config and disable any process-level
    # yolo inherited from the developer's live environment.
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)

    session_key = "cluster-test-session"
    token = approval_context.set_current_session_key(session_key)
    context_tokens = approval_context.set_current_observability_context(
        turn_id=f"cluster-{request.node.name}", session_id=session_key,
    )
    A.clear_session(session_key)
    with A._lock:
        A._gateway_queues.pop(session_key, None)
        A._gateway_notify_cbs.pop(session_key, None)
        A._permanent_approved.discard("execute_code")
        A._session_approved.get(session_key, set()).discard("execute_code")
    try:
        yield session_key
    finally:
        A.clear_session(session_key)
        approval_context.reset_current_observability_context(context_tokens)
        approval_context.reset_current_session_key(token)
        with A._lock:
            A._gateway_queues.pop(session_key, None)
            A._gateway_notify_cbs.pop(session_key, None)


def _register_resolver(session_key: str, result):
    """Register a gateway notify callback that immediately resolves the most
    recent queued approval entry with *result* (simulating a user response)."""
    def cb(_approval_data):
        with A._lock:
            entries = A._gateway_queues.get(session_key, [])
            if entries:
                entry = entries[-1]
                entry.result = result
                entry.event.set()
    with A._lock:
        A._gateway_notify_cbs[session_key] = cb


def _register_capturing_resolver(session_key: str, result):
    """Resolve immediately and retain the exact approval payload shown."""
    seen = {}

    def cb(approval_data):
        seen["approval_data"] = approval_data
        with A._lock:
            entries = A._gateway_queues.get(session_key, [])
            if entries:
                entries[-1].result = result
                entries[-1].event.set()

    with A._lock:
        A._gateway_notify_cbs[session_key] = cb
    return seen


def test_guard_isolated_backend_approved():
    # Container backends already sandbox the child — no-op approve.
    assert A.check_execute_code_guard("import os", "docker")["approved"] is True


def test_guard_headless_local_approved(monkeypatch):
    # Documented #30882 limitation: no approval surface → preserve auto-run.
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    assert A.check_execute_code_guard("import os", "local")["approved"] is True


def test_guard_cron_deny_blocks(monkeypatch):
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval_context, "_get_cron_approval_mode", lambda: "deny")
    tokens = set_session_vars(cron_session="1")
    try:
        res = A.check_execute_code_guard("import os", "local")
    finally:
        clear_session_vars(tokens)
    assert res["approved"] is False
    assert res["outcome"] == "blocked"


def test_guard_explicit_non_cron_masks_leaked_env(monkeypatch):
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval_context, "_get_cron_approval_mode", lambda: "deny")
    tokens = set_session_vars(cron_session="")
    try:
        res = A.check_execute_code_guard("import os", "local")
    finally:
        clear_session_vars(tokens)
        reset_session_vars()
    assert res["approved"] is True


def test_guard_legacy_env_cron_still_blocks(monkeypatch):
    reset_session_vars()
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval_context, "_get_cron_approval_mode", lambda: "deny")
    res = A.check_execute_code_guard("import os", "local")
    assert res["approved"] is False
    assert res["outcome"] == "blocked"


def test_guard_gateway_user_approves_is_one_shot(gw_session):
    _register_resolver(gw_session, "once")
    res = A.check_execute_code_guard("import os; print(1)", "local")
    assert res["approved"] is True
    assert res.get("user_approved") is True
    # One-shot: approval must NOT persist to future scripts.
    assert A.is_approved(gw_session, "execute_code") is False


def test_guard_session_approval_short_circuits_prompt(gw_session):
    """Once session-approved, execute_code skips the approval prompt (#39275)."""
    # Manually set session approval.
    A.approve_session(gw_session, "execute_code")
    try:
        # Even with a denier registered, the is_approved check short-circuits.
        _register_resolver(gw_session, "deny")
        res = A.check_execute_code_guard("import os", "local")
        assert res["approved"] is True
    finally:
        with A._lock:
            s = A._session_approved.get(gw_session, set())
            s.discard("execute_code")


def test_guard_gateway_missing_notify_is_pending(gw_session):
    # No notify callback registered → backward-compat pending approval.
    res = A.check_execute_code_guard("import os", "local")
    assert res["approved"] is False
    assert res["status"] == "pending_approval"


def test_guard_smart_mode(gw_session, monkeypatch):
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "smart")

    monkeypatch.setattr(A, "_smart_approve", lambda c, d, **_kw: "approve")
    res = A.check_execute_code_guard("import os", "local")
    assert res["approved"] is True and res.get("smart_approved") is True

    # First DENY grants one unchanged retry. The repeat requires a fresh
    # one-shot owner decision; without a live notifier it fails closed.
    monkeypatch.setattr(A, "_smart_approve", lambda c, d, **_kw: "deny")
    res = A.check_execute_code_guard("import os", "local")
    assert res["approved"] is False
    assert res["outcome"] == "auto_denied"
    res = A.check_execute_code_guard("import os", "local")
    assert res["approved"] is False and res["status"] == "blocked"
    assert res["outcome"] == "approval_unavailable"
    assert res["one_shot"] is True

    # escalate → falls through to manual gateway approval
    monkeypatch.setattr(A, "_smart_approve", lambda c, d, **_kw: "escalate")
    _register_resolver(gw_session, "once")
    res = A.check_execute_code_guard("import os", "local")
    assert res["approved"] is True


def test_terminal_smart_deny_owner_override_is_one_operation(gw_session, monkeypatch):
    """A human may override DENY, but a broad UI choice must not be persisted."""
    with A._lock:
        A._permanent_approved.discard("owner-override-test-danger")
        A._session_approved.get(gw_session, set()).discard("owner-override-test-danger")
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(A, "_smart_approve", lambda _command, _description, **_kw: "deny")
    monkeypatch.setattr(
        A,
        "detect_dangerous_command",
        lambda command: (True, "owner-override-test-danger", f"risk:{command}"),
    )
    monkeypatch.setattr(
        approval_detection,
        "detect_dangerous_command",
        lambda command: (True, "owner-override-test-danger", f"risk:{command}"),
    )
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
        raising=False,
    )

    first = A.check_all_command_guards("dangerous /tmp/first", "local")
    assert first["outcome"] == "auto_denied"
    shown = _register_capturing_resolver(gw_session, "always")
    result = A.check_all_command_guards("dangerous /tmp/first", "local")

    assert result["approved"] is True
    assert result["user_approved"] is True
    assert shown["approval_data"]["smart_denied"] is True
    assert shown["approval_data"]["allow_permanent"] is False
    assert A.is_approved(gw_session, "owner-override-test-danger") is False

    _register_resolver(gw_session, "deny")
    changed = A.check_all_command_guards("dangerous /tmp/second", "local")
    assert changed["approved"] is False
    assert changed["outcome"] == "auto_denied"


def test_execute_code_smart_deny_owner_override_is_one_operation(gw_session, monkeypatch):
    """Never persist the coarse execute_code key after overriding smart DENY."""
    with A._lock:
        A._permanent_approved.discard("execute_code")
        A._session_approved.get(gw_session, set()).discard("execute_code")
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(A, "_smart_approve", lambda _command, _description, **_kw: "deny")

    first = A.check_execute_code_guard("print('first')", "local")
    assert first["outcome"] == "auto_denied"
    shown = _register_capturing_resolver(gw_session, "session")
    result = A.check_execute_code_guard("print('first')", "local")

    assert result["approved"] is True
    assert result["user_approved"] is True
    assert shown["approval_data"]["smart_denied"] is True
    assert shown["approval_data"]["allow_permanent"] is False
    assert A.is_approved(gw_session, "execute_code") is False

    _register_resolver(gw_session, "deny")
    changed = A.check_execute_code_guard("print('second')", "local")
    assert changed["approved"] is False
    assert changed["outcome"] == "auto_denied"


def test_smart_escalate_still_persists_session_choice(gw_session, monkeypatch):
    """The DENY restriction must not alter Smart ESCALATE's manual choices."""
    key = "smart-escalate-persistence"
    with A._lock:
        A._session_approved.get(gw_session, set()).discard(key)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(A, "_smart_approve", lambda _command, _description, **_kw: "escalate")
    monkeypatch.setattr(
        A, "detect_dangerous_command",
        lambda command: (True, key, f"risk:{command}"),
    )
    monkeypatch.setattr(
        approval_detection, "detect_dangerous_command",
        lambda command: (True, key, f"risk:{command}"),
    )
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
        raising=False,
    )

    shown = _register_capturing_resolver(gw_session, "session")
    result = A.check_all_command_guards("dangerous escalate", "local")

    assert result["approved"] is True
    assert shown["approval_data"]["allow_permanent"] is True
    assert "smart_denied" not in shown["approval_data"]
    assert A.is_approved(gw_session, key) is True


def test_terminal_smart_deny_without_notifier_is_one_shot_block(gw_session, monkeypatch):
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(A, "_smart_approve", lambda _command, _description, **_kw: "deny")
    monkeypatch.setattr(
        A, "detect_dangerous_command",
        lambda command: (True, "pending-smart-deny", f"risk:{command}"),
    )
    monkeypatch.setattr(
        approval_detection, "detect_dangerous_command",
        lambda command: (True, "pending-smart-deny", f"risk:{command}"),
    )
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
        raising=False,
    )

    first = A.check_all_command_guards("dangerous pending", "local")
    assert first["outcome"] == "auto_denied"
    result = A.check_all_command_guards("dangerous pending", "local")

    assert result["status"] == "blocked"
    assert result["outcome"] == "approval_unavailable"
    assert result["one_shot"] is True
    with A._lock:
        assert gw_session not in A._pending


def test_execute_code_smart_deny_without_notifier_is_one_shot_block(gw_session, monkeypatch):
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(A, "_smart_approve", lambda _command, _description, **_kw: "deny")

    first = A.check_execute_code_guard("print('pending')", "local")
    assert first["outcome"] == "auto_denied"
    result = A.check_execute_code_guard("print('pending')", "local")

    assert result["status"] == "blocked"
    assert result["outcome"] == "approval_unavailable"
    assert result["one_shot"] is True
    with A._lock:
        assert gw_session not in A._pending


def test_terminal_serializes_smart_deny_pending_capabilities(monkeypatch):
    from tools import terminal_tool as terminal_module

    monkeypatch.setattr(
        terminal_module,
        "_check_all_guards",
        lambda *_args, **_kwargs: {
            "approved": False,
            "status": "pending_approval",
            "command": "rm -rf /tmp/example",
            "description": "recursive delete",
            "pattern_key": "rm-rf",
            "smart_denied": True,
            "allow_permanent": False,
        },
    )

    payload = json.loads(terminal_module.terminal_tool(command="rm -rf /tmp/example"))

    assert payload["smart_denied"] is True
    assert payload["allow_permanent"] is False


def test_terminal_public_entry_passes_effective_cwd_and_script_reader_to_guard(
    monkeypatch, tmp_path,
):
    """The production terminal entry must expose the script that will run."""
    from tools import terminal_tool as terminal_module

    script = tmp_path / "entry.py"
    script.write_text("print('terminal evidence')\n", encoding="utf-8")
    captured = {}
    plan = SimpleNamespace(
        config={"env_type": "local"},
        env_type="local",
        cwd=str(tmp_path / "session-cwd"),
        effective_task_id="task",
        effective_timeout=30.0,
        promoted_from_foreground_timeout=None,
    )
    monkeypatch.setattr(terminal_module, "_plan_execution", lambda *a, **k: plan)
    monkeypatch.setattr(terminal_module, "_acquire_env", lambda *a, **k: object())
    monkeypatch.setattr(terminal_module, "_pre_exec_block", lambda *a, **k: None)

    def reject(command, env_type, **kwargs):
        captured.update(command=command, env_type=env_type, **kwargs)
        return {"approved": False, "message": "captured"}

    monkeypatch.setattr(terminal_module, "_check_all_guards_impl", reject)
    result = json.loads(terminal_module.terminal_tool(
        "python entry.py", workdir=str(tmp_path),
    ))

    assert result["status"] == "blocked"
    assert captured["cwd"] == str(tmp_path)
    assert callable(captured["read_script"])
    assert captured["read_script"](str(script)) == "print('terminal evidence')\n"


def test_terminal_remote_entry_reads_script_from_execution_environment(monkeypatch):
    """Remote approval evidence must come from the backend that will execute it."""
    from tools import terminal_tool as terminal_module

    captured = {}
    executed = []
    plan = SimpleNamespace(
        config={"env_type": "ssh"},
        env_type="ssh",
        cwd="/remote/session",
        effective_task_id="task",
        effective_timeout=30.0,
        promoted_from_foreground_timeout=None,
    )

    class RemoteEnv:
        cwd = "/remote/live"

        def execute(self, command, **_kwargs):
            executed.append(command)
            return {"returncode": 0, "output": "echo remote\n"}

    monkeypatch.setattr(terminal_module, "_plan_execution", lambda *a, **k: plan)
    monkeypatch.setattr(terminal_module, "_acquire_env", lambda *a, **k: RemoteEnv())
    monkeypatch.setattr(terminal_module, "_pre_exec_block", lambda *a, **k: None)

    def reject(command, env_type, **kwargs):
        captured.update(command=command, env_type=env_type, **kwargs)
        return {"approved": False, "message": "captured"}

    monkeypatch.setattr(terminal_module, "_check_all_guards_impl", reject)
    result = json.loads(terminal_module.terminal_tool("bash deploy.sh"))

    assert result["status"] == "blocked"
    assert captured["cwd"] == "/remote/session"
    assert callable(captured["read_script"])
    assert captured["read_script"]("/remote/session/deploy.sh") == "echo remote\n"
    assert executed and "head -c 32001" in executed[0]


def test_execute_code_public_entry_passes_effective_cwd_and_script_reader_to_guard(
    monkeypatch, tmp_path,
):
    """The production execute_code entry must expose literal script evidence."""
    from tools import code_execution_tool as code_module
    from tools import terminal_tool as terminal_module

    helper = tmp_path / "helper.py"
    helper.write_text("VALUE = 'execute evidence'\n", encoding="utf-8")
    captured = {}
    monkeypatch.setattr(code_module, "SANDBOX_AVAILABLE", True)
    monkeypatch.setattr(
        terminal_module,
        "_get_env_config",
        lambda: {"env_type": "local", "docker_volumes": []},
    )
    monkeypatch.setattr(
        "tools.process_registry._is_supervised_gateway_process", lambda: False,
    )
    monkeypatch.setattr(code_module, "_get_execution_mode", lambda: "project")
    monkeypatch.setattr(
        code_module, "_resolve_child_cwd", lambda *a, **k: str(tmp_path),
    )

    def reject(code, env_type, **kwargs):
        captured.update(code=code, env_type=env_type, **kwargs)
        return {"approved": False, "message": "captured"}

    monkeypatch.setattr("tools.approval.check_execute_code_guard", reject)
    code = (
        "import importlib.util\n"
        "spec = importlib.util.spec_from_file_location('helper', 'helper.py')\n"
    )
    result = json.loads(code_module.execute_code(code, task_id="task"))

    assert "captured" in result["error"]
    assert captured["cwd"] == str(tmp_path)
    assert callable(captured["read_script"])
    assert captured["read_script"](str(helper)) == "VALUE = 'execute evidence'\n"


def test_execute_code_remote_entry_uses_active_environment_for_script_evidence(
    monkeypatch,
):
    """Remote execute_code must review files from its active backend, not the host."""
    from tools import code_execution_tool as code_module
    from tools import terminal_tool as terminal_module
    from tools import terminal_tool_lifecycle as lifecycle_module

    captured = {}
    executed = []

    class RemoteEnv:
        cwd = "/remote/live"

        def execute(self, command, **_kwargs):
            executed.append(command)
            return {"returncode": 0, "output": "VALUE = 'remote evidence'\n"}

    monkeypatch.setattr(code_module, "SANDBOX_AVAILABLE", True)
    monkeypatch.setattr(
        terminal_module,
        "_get_env_config",
        lambda: {"env_type": "ssh", "cwd": "/remote/config", "docker_volumes": []},
    )
    monkeypatch.setattr(
        "tools.process_registry._is_supervised_gateway_process", lambda: False,
    )
    monkeypatch.setattr(code_module, "_get_execution_mode", lambda: "project")
    monkeypatch.setattr(code_module, "_resolve_child_cwd", lambda *a, **k: "/host/unused")
    monkeypatch.setattr(lifecycle_module, "get_active_env", lambda _task_id: RemoteEnv())

    def reject(code, env_type, **kwargs):
        captured.update(code=code, env_type=env_type, **kwargs)
        return {"approved": False, "message": "captured"}

    monkeypatch.setattr("tools.approval.check_execute_code_guard", reject)
    code = "import runpy\nrunpy.run_path('helper.py')\n"
    result = json.loads(code_module.execute_code(code, task_id="task"))

    assert "captured" in result["error"]
    assert captured["cwd"] == "/remote/live"
    assert callable(captured["read_script"])
    assert captured["read_script"]("/remote/live/helper.py") == (
        "VALUE = 'remote evidence'\n"
    )
    assert executed and "head -c 32001" in executed[0]


def test_guard_session_yolo_bypasses(gw_session):
    A.enable_session_yolo(gw_session)
    try:
        # Even with a denier registered, yolo short-circuits before the prompt.
        _register_resolver(gw_session, "deny")
        assert A.check_execute_code_guard("import os", "local")["approved"] is True
    finally:
        A.disable_session_yolo(gw_session)


# ---------------------------------------------------------------------------
# 4. Env scrubbing (#27303)
# ---------------------------------------------------------------------------

def test_env_scrub_hermes_allowlist_and_secret_blocks():
    from tools.code_execution_env import _scrub_child_env

    env = {
        # operational allowlist → kept
        "HERMES_HOME": "/h", "HERMES_PROFILE": "p",
        "HERMES_CONFIG": "/c.yaml", "HERMES_ENV": "/e",
        "HERMES_DELEGATED_CHILD_CONTEXT": "1",
        # other HERMES_* → dropped (broad prefix removed)
        "HERMES_BASE_URL": "https://x", "HERMES_INTERACTIVE": "1",
        "HERMES_KANBAN_TASK": "t_parent",
        # secret substrings (incl. new DSN/WEBHOOK) → dropped
        "SENTRY_DSN": "https://a@s.io/1", "SLACK_WEBHOOK": "https://h/x",
        "OPENAI_API_KEY": "sk", "GITHUB_TOKEN": "ghp",
        # safe prefix → kept; uncategorized → dropped
        "PATH": "/usr/bin", "RANDOM_X": "y",
    }
    out = _scrub_child_env(env, is_passthrough=lambda _: False, is_windows=False)

    for kept in (
        "HERMES_HOME", "HERMES_PROFILE", "HERMES_CONFIG", "HERMES_ENV",
        "HERMES_DELEGATED_CHILD_CONTEXT", "PATH",
    ):
        assert kept in out, f"{kept} should be kept"
    for dropped in (
        "HERMES_BASE_URL", "HERMES_INTERACTIVE", "HERMES_KANBAN_TASK",
        "SENTRY_DSN", "SLACK_WEBHOOK", "OPENAI_API_KEY", "GITHUB_TOKEN",
        "RANDOM_X",
    ):
        assert dropped not in out, f"{dropped} should be dropped"


def test_env_scrub_passthrough_overrides_secret_block():
    """A skill/config-declared passthrough var is an explicit user opt-in and
    passes even if it matches a secret substring (precedence is intentional)."""
    from tools.code_execution_env import _scrub_child_env

    env = {"MY_SERVICE_DSN": "value"}
    out = _scrub_child_env(env, is_passthrough=lambda k: k == "MY_SERVICE_DSN",
                           is_windows=False)
    assert out.get("MY_SERVICE_DSN") == "value"


# ---------------------------------------------------------------------------
# 5. File-tool sensitive-path refusal (security B1)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 6. Env-scrub diagnosability mitigation (#27303 follow-up)
# ---------------------------------------------------------------------------


def test_env_scrub_no_log_when_nothing_dropped(caplog):
    """No diagnostic noise when there are no dropped HERMES_* vars."""
    import logging

    from tools.code_execution_env import _scrub_child_env

    with caplog.at_level(logging.DEBUG, logger="tools.code_execution_tool"):
        _scrub_child_env(
            {"HERMES_HOME": "/h", "PATH": "/usr/bin"},
            is_passthrough=lambda _: False,
            is_windows=False,
        )
    assert "dropped" not in "\n".join(r.getMessage() for r in caplog.records)
