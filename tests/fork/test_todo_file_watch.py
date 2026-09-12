"""Discovery is not processing: pending files survive until explicit acknowledgement."""
import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.macos_only


def watcher():
    spec = importlib.util.find_spec("scripts.todo_file_watch")
    assert spec is not None, "incremental watcher is not implemented"
    from scripts import todo_file_watch
    return todo_file_watch


def test_recursive_baseline_pending_retry_and_ack(tmp_path):
    module = watcher()
    root = tmp_path / "待办"
    root.mkdir()
    (root / "旧文件.txt").write_text("已有事项")
    state = tmp_path / "state.json"
    baseline = module.initialize(root, state)
    assert baseline["baseline_files"] == 1
    assert module.scan(root, state)["pending"] == []
    nested = root / "子目录" / "新增子目录"
    nested.mkdir(parents=True)
    new = nested / "新任务.txt"
    new.write_text("新任务")
    (root / ".DS_Store").write_bytes(b"ignore")
    (root / "~$lock.xlsx").write_bytes(b"ignore")
    (root / "draft.xlsx~").write_bytes(b"ignore")
    result = module.scan(root, state)
    assert result["new_count"] == 1
    item = result["pending"][0]
    assert item["path"] == str(new)
    assert module.scan(root, state)["pending"] == [item]
    # Restart/repeated initialize must never mark pending as a new baseline.
    assert module.initialize(root, state)["initialized"] is False
    assert module.scan(root, state)["pending"] == [item]
    with pytest.raises(ValueError):
        module.acknowledge(root, state, [item["id"]], "")
    module.acknowledge(root, state, [item["id"]], "提醒事项 task-1 已读回；待用户确认期限")
    assert module.scan(root, state)["pending"] == []
    (root / "旧文件.txt").write_text("旧文件修改不当新增")
    assert module.scan(root, state)["pending"] == []
    assert new.read_text() == "新任务"  # Never modifies business files.


def test_real_cli_needs_only_stdlib_and_lock_contention_does_not_write(tmp_path):
    import json
    import subprocess
    import sys
    module = watcher()
    root = tmp_path / "待办"
    root.mkdir()
    state = tmp_path / "state.json"
    command = [sys.executable, "-S", module.__file__, "init", "--root", str(root), "--state", str(state)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["initialized"]
    before = state.read_bytes()
    command[3] = "scan"
    with module._state_lock(state):
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        assert result.returncode == 1
        assert json.loads(result.stdout)["ok"] is False
        assert state.read_bytes() == before
    assert subprocess.run(command, capture_output=True, text=True, timeout=10).returncode == 0


def test_move_within_root_is_not_new_and_pending_uses_latest_path(tmp_path):
    module = watcher()
    root = tmp_path / "待办"
    root.mkdir()
    original = root / "existing.txt"
    original.write_text("old")
    state = tmp_path / "state.json"
    module.initialize(root, state)
    original.rename(root / "renamed.txt")
    assert module.scan(root, state)["pending"] == []
    new = root / "new.txt"
    new.write_text("new")
    item = module.scan(root, state)["pending"][0]
    renamed = root / "moved.txt"
    new.rename(renamed)
    result = module.scan(root, state)
    assert result["new_count"] == 0
    assert len(result["pending"]) == 1
    assert result["pending"][0]["path"] == str(renamed)
    assert result["pending"][0]["id"] == item["id"]


def test_failed_reads_pending_updates_and_missing_state_never_advance_silently(tmp_path, monkeypatch):
    module = watcher()
    root = tmp_path / "待办"
    root.mkdir()
    state = tmp_path / "state.json"
    with pytest.raises(FileNotFoundError):
        module.scan(root, state)
    module.initialize(root, state)
    new = root / "new.txt"
    new.write_text("v1")
    original = module._fingerprint

    def fail(path):
        raise OSError("暂时读取失败")

    monkeypatch.setattr(module, "_fingerprint", fail)
    result = module.scan(root, state)
    assert not result["ok"] and result["errors"]
    monkeypatch.setattr(module, "_fingerprint", original)
    first = module.scan(root, state)["pending"][0]
    new.write_text("v2")
    with pytest.raises(ValueError, match="changed"):
        module.acknowledge(root, state, [first["id"]], "stale record")
    second = module.scan(root, state)["pending"][0]
    assert second["id"] != first["id"]
    with pytest.raises(ValueError, match="superseded"):
        module.acknowledge(root, state, [first["id"]], "stale record")
    assert module.scan(root, state)["pending"] == [second]
    before = state.read_bytes()
    state.write_text("broken json")
    with pytest.raises(ValueError):
        module.initialize(root, state)
    assert state.read_text() == "broken json"
    state.write_bytes(before)


@pytest.mark.macos_only
def test_symlinks_are_not_traversed_and_missing_pending_can_be_recorded(tmp_path):
    module = watcher()
    root = tmp_path / "待办"
    root.mkdir()
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "outside.txt").write_text("outside")
    (root / "outside").symlink_to(outside, target_is_directory=True)
    (root / "alias.txt").symlink_to(outside / "outside.txt")
    state = tmp_path / "state.json"
    assert module.initialize(root, state)["baseline_files"] == 0
    new = root / "new.txt"
    new.write_text("new")
    item = module.scan(root, state)["pending"][0]
    new.unlink()
    assert module.scan(root, state)["pending"][0]["available"] is False
    module.acknowledge(root, state, [item["id"]], "已移出待办；已记录需用户确认是否完成", allow_missing=True)
    assert module.scan(root, state)["pending"] == []
