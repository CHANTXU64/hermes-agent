from __future__ import annotations

import json
import ast
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tools.memory_tool as memory_module
from tools.memory_tool import MemoryStore

from fork_features.memory_governance import MemoryGovernance


def test_history_routes_through_fork_governance_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    store = MemoryStore()
    governance = MagicMock()
    governance.history.return_value = {
        "success": True,
        "target": "memory",
        "current_entry": "durable entry",
        "history": [],
        "matched_records": 0,
        "returned_records": 0,
        "truncated": False,
        "max_chars": 8000,
    }
    monkeypatch.setattr(memory_module, "get_memory_dir", lambda: tmp_path)
    monkeypatch.setattr(
        memory_module,
        "_build_memory_governance",
        lambda: governance,
        raising=False,
    )

    result = json.loads(
        memory_module.memory_tool(
            action="history",
            target="memory",
            old_text="durable",
            store=store,
        )
    )

    assert result["success"] is True
    governance.history.assert_called_once_with(store, "memory", "durable")


def test_mutation_uses_public_store_transaction_after_audit_initialization() -> None:
    events: list[object] = []

    class Transaction:
        path = Path("/tmp/MEMORY.md")

        def read_checked(self):
            events.append("read")
            return ["old durable entry"], True

        def apply(self, operations, *, batch=False):
            events.append(("apply", operations))
            return {"success": True, "message": "Entry replaced."}

        def entries(self):
            return ["new durable entry"]

        def read_current_checked(self):
            return ["new durable entry"], True

        def restore(self, entries):
            events.append(("restore", entries))

        def adopt(self, entries):
            events.append(("adopt", entries))

    class Store:
        @contextmanager
        def transaction(self, target, coordinator_path):
            events.append(("lock", target, coordinator_path))
            yield Transaction()
            events.append("unlock")

    audit = MagicMock()
    audit.path = Path("/tmp/MEMORY_CHANGELOG.jsonl")
    audit.initialize.side_effect = lambda: events.append("initialize")
    audit.append.side_effect = lambda target, traces: events.append(
        ("append", target, traces)
    )
    governance = MemoryGovernance(
        audit,
        read_failed_error=lambda path: {"success": False, "path": str(path)},
    )

    result = governance.apply(
        Store(),
        "memory",
        [
            {
                "action": "replace",
                "old_text": "old",
                "content": "new durable entry",
                "reason": "keep a durable fact current",
                "evidence": "verified replacement",
            }
        ],
    )

    assert result["success"] is True
    assert events[0] == ("lock", "memory", audit.path)
    assert events[1:4] == [
        "initialize",
        "read",
        (
            "apply",
            [
                {
                    "action": "replace",
                    "old_text": "old",
                    "content": "new durable entry",
                    "reason": "keep a durable fact current",
                    "evidence": "verified replacement",
                    "change_type": "replace",
                }
            ],
        ),
    ]
    append_event = events[4]
    assert isinstance(append_event, tuple)
    assert append_event[0:2] == ("append", "memory")
    assert events[-1] == "unlock"


def test_real_store_transaction_holds_coordinator_then_target_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[tuple[str, str]] = []

    @contextmanager
    def recording_lock(path: Path):
        events.append(("enter", path.name))
        try:
            yield
        finally:
            events.append(("exit", path.name))

    monkeypatch.setattr(memory_module, "get_memory_dir", lambda: tmp_path)
    monkeypatch.setattr(
        MemoryStore,
        "_file_lock",
        staticmethod(recording_lock),
    )
    coordinator_path = tmp_path / "MEMORY_CHANGELOG.jsonl"

    with MemoryStore().transaction("memory", coordinator_path) as transaction:
        events.append(("body", transaction.path.name))

    assert events == [
        ("enter", "MEMORY_CHANGELOG.jsonl"),
        ("enter", "MEMORY.md"),
        ("body", "MEMORY.md"),
        ("exit", "MEMORY.md"),
        ("exit", "MEMORY_CHANGELOG.jsonl"),
    ]


def test_high_churn_hosts_consume_fork_memory_policy_seams() -> None:
    repo = Path(__file__).resolve().parents[2]

    memory_source = (repo / "tools/memory_tool.py").read_text(encoding="utf-8")
    memory_tree = ast.parse(memory_source)
    retired_host_definitions = {
        "get_memory_changelog_path",
        "_jsonl_text",
        "_baseline_records",
        "initialize_memory_changelog",
        "_normalize_governance_operation",
        "_trace_governance_changes",
        "_format_change_records",
        "_append_governance_records",
        "_load_changelog_records",
        "_related_history_records",
        "_history_record_for_model",
        "_bounded_history",
        "_memory_history",
        "_apply_governed_mutation",
    }
    assert not retired_host_definitions.intersection(
        node.name
        for node in memory_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    memory_schema = next(
        node
        for node in memory_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "MEMORY_SCHEMA"
            for target in node.targets
        )
    )
    assert isinstance(memory_schema.value, ast.Call)
    assert ast.unparse(memory_schema.value.func) == "build_memory_schema"

    prompt_tree = ast.parse(
        (repo / "agent/prompt_builder.py").read_text(encoding="utf-8")
    )
    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == "fork_features.memory_governance"
        and any(
            alias.name == "MAIN_AGENT_MEMORY_GUIDANCE"
            and alias.asname == "MEMORY_GUIDANCE"
            for alias in node.names
        )
        for node in prompt_tree.body
    )

    background_source = (repo / "agent/background_review.py").read_text(
        encoding="utf-8"
    )
    background_tree = ast.parse(background_source)
    background_defs = {
        node.name
        for node in background_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_encode_governance_data" not in background_defs
    context_builder = next(
        node
        for node in background_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "build_memory_governance_context"
    )
    assert any(
        isinstance(node, ast.Call)
        and ast.unparse(node.func) == "build_review_context"
        for node in ast.walk(context_builder)
    )

    inline_source = (repo / "agent/inline_tool_executors.py").read_text(encoding="utf-8")
    assert "forwarded_memory_kwargs" in inline_source
    assert "store=agent._memory_store" in inline_source

    for relative_path in (
        "fork_features/memory_audit.py",
        "fork_features/memory_governance.py",
    ):
        tree = ast.parse((repo / relative_path).read_text(encoding="utf-8"))
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert "tools.memory_tool" not in imported_modules
        assert "agent.background_review" not in imported_modules
        assert not [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr.startswith("_")
            and isinstance(node.value, ast.Name)
            and node.value.id in {"store", "transaction"}
        ]
