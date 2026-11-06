"""The shared-browser gate must not execute before explicit target selection.

These stdlib tests also run while other parts of the upgrade are unmerged.
The full host/harness tests remain a separate acceptance requirement.
"""
import ast
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
from types import ModuleType, SimpleNamespace
from typing import Optional
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class TestBrowserTargetSelection(unittest.TestCase):
    def build(self, code, tab_id=None):
        path = ROOT / "fork_features" / "browser_target_selection.py"
        self.assertTrue(path.is_file(), "shared browser has no explicit-target gate")
        spec = importlib.util.spec_from_file_location("selection_under_test", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.prepare_browser_code(code, tab_id)

    def test_missing_target_returns_tabs_without_running_payload(self):
        namespace = {
            "list_tabs": lambda: [{"targetId": "keep", "title": "Keep me", "url": "https://example.org/keep"}],
            "payload_ran": False,
        }
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(self.build("payload_ran = True"), namespace)
        self.assertFalse(namespace["payload_ran"])
        result = json.loads(out.getvalue())
        self.assertTrue(result["selection_required"])
        self.assertEqual(result["tabs"][0]["targetId"], "keep")

    def browser(self):
        ns = {"selected": "keep", "events": []}
        tabs = [{"targetId": "keep"}, {"targetId": "chosen"}]
        def switch(target):
            ns["events"].append(("switch", target))
            ns["selected"] = target
        ns.update(list_tabs=lambda: tabs, switch_tab=switch,
                  current_tab=lambda: {"targetId": ns["selected"]})
        return ns

    def test_explicit_existing_target_selected_before_payload(self):
        ns = self.browser()
        exec(self.build("events.append(('payload', selected))", "chosen"), ns)
        self.assertEqual(ns["events"], [("switch", "chosen"), ("payload", "chosen")])

    def test_unknown_or_invalid_target_never_runs_payload(self):
        for target in ("gone", "", " ", 4, {"targetId": "chosen"}):
            with self.subTest(target=target):
                ns = self.browser()
                with self.assertRaises((ValueError, RuntimeError)):
                    exec(self.build("events.append(('payload', selected))", target), ns)
                self.assertEqual(ns["events"], [])

    def test_new_explicitly_creates_a_fresh_target_before_payload(self):
        ns = self.browser()
        def create(method, **params):
            self.assertEqual(method, "Target.createTarget")
            self.assertEqual(params, {"url": "about:blank", "background": True})
            ns["events"].append(("create", "fresh"))
            return {"targetId": "fresh"}
        ns["cdp"] = create
        exec(self.build("events.append(('payload', selected))", "new"), ns)
        self.assertEqual(ns["events"], [("create", "fresh"), ("switch", "fresh"), ("payload", "fresh")])

    def test_switch_error_or_wrong_confirmation_never_runs_payload(self):
        for kind in ("raises", "wrong"):
            with self.subTest(kind=kind):
                ns = self.browser()
                def switch(target):
                    if kind == "raises":
                        raise RuntimeError("switch failed")
                ns["switch_tab"] = switch
                with self.assertRaises(RuntimeError):
                    exec(self.build("events.append(('payload', selected))", "chosen"), ns)
                self.assertEqual(ns["events"], [])

    def test_create_error_or_missing_target_never_runs_payload(self):
        for result in ("raises", {}, {"targetId": None}, {"targetId": ""}):
            with self.subTest(result=result):
                ns = self.browser()
                def create(*args, **kwargs):
                    if result == "raises":
                        raise RuntimeError("create failed")
                    return result
                ns["cdp"] = create
                with self.assertRaises(RuntimeError):
                    exec(self.build("events.append(('payload', selected))", "new"), ns)
                self.assertEqual(ns["events"], [])

    def host(self, payload, tab_id=None, *, session="", private=False):
        """Exercise the real host function with route/CLI I/O isolated.

        Compile that exact AST node while unrelated merge conflicts prevent full
        package import. This seam test does not replace later normal imports or
        a real browser-harness/Chrome smoke.
        """
        tree = ast.parse((ROOT / "tools/browser_use_cli.py").read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "browser_exec")
        ns = self.browser()
        def run_cli(cmd, code, env, timeout):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                exec(code, ns)
            return subprocess.CompletedProcess(cmd, 0, out.getvalue(), "")
        redactor = ModuleType("agent.redact")
        redactor.__dict__["redact_sensitive_text"] = lambda value, **kw: value
        registry = ModuleType("tools.registry")
        registry.__dict__["tool_error"] = lambda message: {"error": message}
        registry.__dict__["tool_result"] = lambda result: result
        shot = ModuleType("tools.browser_use_shot")
        shot.__dict__["ACTIVATE_NEVER"] = "never"
        shot.__dict__["harness_command"] = lambda cmd, **kw: cmd
        g = dict(Optional=Optional, json=json, re=re, time=__import__("time"), subprocess=subprocess,
                 _DEFAULT_TIMEOUT_S=300, _STDERR_CAP_CHARS=4000, _SESSION_RE=re.compile(r"^[A-Za-z0-9_-]+$"),
                 _URL_RE=re.compile(r"https?://"), _PRIVATE_BROWSER_SENTINEL="private",
                 _BOT_DESKTOP_BROWSER_SENTINEL="desktop", _CLOUD_BROWSER_SENTINEL="cloud",
                 _exec_local_engine=SimpleNamespace(set=lambda value: None),
                 _code_url_verdict=lambda code: SimpleNamespace(blocked=None, local_sidecar=False),
                 _backend_cache_key=lambda task, name: name, _exec_route_is_sidecar={},
                 _find_cli=lambda: ["isolated-cli"], _base_subprocess_env=dict,
                 _route_exec_current=lambda *a: ({"private": "1"} if private else {}, None, False, 0),
                 _workspace_dir=lambda task: None, _read_browser_cfg=dict,
                 is_legacy_browser_use_cloud_config=lambda cfg: False, _clamp_timeout=int,
                 _attach_vault_supervisor=lambda *a: None, _driven_daemons_lock=threading.RLock(),
                 _driven_daemons={}, daemon_identity=lambda env: ("isolated",), hermes_home_key=lambda: "isolated",
                 fit_default_socket_dir=lambda env: None, _OWN_DURING_CALL_S=0.5,
                 _own_harness_daemon=lambda env: None, _own_harness_daemon_until_owned=lambda env: None,
                 _native_screenshot_result=lambda result, shot: None,
                 _run_cli_killing_process_group=run_cli, _reload_if_republished=lambda *a: None,
                 _take_real_profile_note=lambda key: {}, _find_screenshot=lambda *a: None,
                 _OWN_TAB_PREAMBLE="")
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<actual-browser-exec>", "exec"), g)
        kwargs = {"session": session}
        if tab_id is not None:
            kwargs["tab_id"] = tab_id
        scheduler = ModuleType("agent.periodic_scheduler")
        scheduler.__dict__["schedule"] = lambda fn, interval: SimpleNamespace(cancel=lambda wait=None: None)
        with patch.dict(sys.modules, {"agent.redact": redactor, "tools.registry": registry,
                                      "tools.browser_use_shot": shot, "agent.periodic_scheduler": scheduler}):
            result = g["browser_exec"](payload, **kwargs)
        return result, ns

    def test_host_default_shared_missing_target_does_not_run_payload(self):
        result, ns = self.host("events.append(('payload', selected))")
        self.assertEqual(ns["events"], [], "host must not bypass selection on its default session")
        self.assertTrue(json.loads(result["output"])["selection_required"])

    def test_host_named_shared_also_requires_selection(self):
        result, ns = self.host("events.append(('payload', selected))", session="task-name")
        self.assertEqual(ns["events"], [])
        self.assertTrue(json.loads(result["output"])["selection_required"])

    def test_host_selected_target_is_used(self):
        result, ns = self.host("events.append(('payload', selected))", "chosen")
        self.assertTrue(result["success"])
        self.assertEqual(ns["events"], [("switch", "chosen"), ("payload", "chosen")])

    def test_host_private_browser_keeps_official_default_behavior(self):
        result, ns = self.host("events.append(('payload', selected))", private=True)
        self.assertTrue(result["success"])
        self.assertEqual(ns["events"], [("payload", "keep")])

    def test_registry_handler_forwards_the_explicit_target(self):
        tree = ast.parse((ROOT / "tools/browser_use_cli.py").read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_handle_browser_exec")
        import contextvars
        calls = []
        g = {"_exec_local_engine": contextvars.ContextVar("test_engine", default=None),
             "_DEFAULT_TIMEOUT_S": 300, "browser_exec": lambda **kw: calls.append(kw) or kw}
        metrics = ModuleType("hermes_cli.observability.shared_metrics_loop")
        metrics.__dict__["record_browser_call"] = lambda call, *args: call()
        cloud = ModuleType("tools.browser_session_cloud")
        cloud.__dict__["browser_backend_name"] = lambda **kw: "test"
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<actual-browser-handler>", "exec"), g)
        with patch.dict(sys.modules, {metrics.__name__: metrics, cloud.__name__: cloud}):
            g["_handle_browser_exec"]({"code": "pass", "tab_id": "chosen"}, task_id="task")
        self.assertEqual(calls[0].get("tab_id"), "chosen")

    def test_tool_schema_exposes_explicit_target(self):
        tree = ast.parse((ROOT / "tools/browser_use_cli.py").read_text())
        assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "BROWSER_EXEC_SCHEMA" for t in n.targets))
        ns = {"_HEADER_BASE": "", "_HELPERS_DIGEST": "", "_DEFAULT_TIMEOUT_S": 300, "_MAX_TIMEOUT_S": 1800}
        exec(compile(ast.Module(body=[assignment], type_ignores=[]), "<actual-browser-schema>", "exec"), ns)
        self.assertIn("tab_id", ns["BROWSER_EXEC_SCHEMA"]["parameters"]["properties"])


if __name__ == "__main__":
    unittest.main()
