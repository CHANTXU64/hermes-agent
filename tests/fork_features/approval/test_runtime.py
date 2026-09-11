"""Approval host capabilities without depending on the mutable gate implementation."""
import importlib
import importlib.util
from types import SimpleNamespace


def runtime_module():
    spec = importlib.util.find_spec("fork_features.approval.runtime")
    assert spec is not None, "Approval evidence and model adapters need a standalone boundary"
    return importlib.import_module(spec.name)


def test_script_evidence_uses_executing_backend_and_is_bounded(tmp_path):
    runtime = runtime_module()
    from fork_features.approval.policy import MAX_SCRIPT_BYTES
    path = tmp_path / "source with spaces.py"
    path.write_bytes(b"x" * (MAX_SCRIPT_BYTES + 20))
    assert len(runtime.read_local_script(str(path))) == MAX_SCRIPT_BYTES + 1
    assert runtime.read_local_script(str(tmp_path / "absent")) is None
    commands = []

    def execute(command):
        commands.append(command)
        return {"returncode": 0, "output": "remote source"}

    assert runtime.read_remote_script(SimpleNamespace(execute=execute), str(path)) == "remote source"
    assert commands == [f"head -c {MAX_SCRIPT_BYTES + 1} < '{path}'"]
    failed = SimpleNamespace(execute=lambda command: {"returncode": 1, "output": "partial"})
    assert runtime.read_remote_script(failed, str(path)) is None


def test_structured_call_uses_official_timeout_and_preserves_overrides(monkeypatch):
    runtime = runtime_module()
    calls = []
    monkeypatch.setattr("agent.auxiliary_client._get_task_timeout", lambda task: 37)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", lambda **kw: calls.append(kw) or "result")
    assert runtime.call_approval_llm(messages=[]) == "result"
    assert calls[-1] == {"messages": [], "timeout": 37, "task": "approval", "temperature": 0, "max_tokens": 256}
    runtime.call_approval_llm(timeout=19, max_tokens=128)
    assert calls[-1]["timeout"] == 19
    assert calls[-1]["max_tokens"] == 128
