"""Audit exact Store mutations, including tolerant matches and approval pinning."""

import json
from typing import Any

import pytest

from tools import memory_tool as memory_module
from tools.memory_tool import MemoryStore, apply_memory_pending, memory_tool


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(memory_module, "get_memory_dir", lambda: tmp_path)
    result = MemoryStore(memory_char_limit=2000, user_char_limit=2000)
    result.load_from_disk()
    return result


def change_records(tmp_path):
    records = [json.loads(line) for line in
               (tmp_path / "MEMORY_CHANGELOG.jsonl").read_text().splitlines()]
    return [record for record in records if record["event_type"] == "change"]


@pytest.mark.parametrize("target", ["memory", "user"])
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("action", ["replace", "remove"])
@pytest.mark.parametrize("original,needle", [
    ('Project “Alpha” uses Python', 'Project "Alpha"'),
    ('Project Alpha — Python', 'Alpha - Python'),
    ('Project Alpha\nuses Python', 'Alpha uses'),
])
def test_tolerant_mutation_journals_exact_store_target(
    store, tmp_path, target, batch, action, original, needle,
):
    assert store.add(target, original)["success"]
    assert store.add(target, "Unrelated durable fact")["success"]
    op = dict(action=action, old_text=needle, content="Updated project fact",
              reason="Refresh fixture", evidence="Verified fixture", deletion_type="safe")
    args: dict[str, Any] = {"operations": [op]} if batch else op
    result = json.loads(memory_tool(store=store, target=target, **args))
    assert result["success"] is True
    expected_after = "Updated project fact" if action == "replace" else None
    expected_entries = ([expected_after] if expected_after else []) + ["Unrelated durable fact"]
    assert store.read_target_entries_checked(target) == (expected_entries, True)
    records = change_records(tmp_path)
    assert [(r["target"], r["before"], r["after"]) for r in records] == [
        (target, original, expected_after),
    ]


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("action", ["replace", "remove"])
def test_approval_journals_pinned_target_not_first_overlapping_entry(
    store, tmp_path, batch, action,
):
    for entry in ["New Alpha policy", "Original Alpha policy"]:
        assert store.add("memory", entry)["success"]
    op = dict(action=action, old_text="Alpha", matched_entry="Original Alpha policy",
              content="Updated Alpha policy", reason="Refresh fixture",
              evidence="Verified approval", deletion_type="safe")
    payload = dict(action="batch", operations=[op]) if batch else op
    result = apply_memory_pending(dict(target="memory", **payload), store)
    assert result["success"] is True
    expected_after = "Updated Alpha policy" if action == "replace" else None
    assert store.read_target_entries_checked("memory") == (
        ["New Alpha policy"] + ([expected_after] if expected_after else []), True,
    )
    assert [(r["before"], r["after"]) for r in change_records(tmp_path)] == [
        ("Original Alpha policy", expected_after),
    ]


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("fault", ["missing", "unknown", "wrong_entry"])
def test_invalid_store_receipt_rolls_back_without_a_false_audit(
    store, tmp_path, monkeypatch, batch, fault,
):
    from tools.memory_tool_store import MemoryStoreTransaction

    original_entries = ["Original fact", "Other fact"]
    for entry in original_entries:
        assert store.add("memory", entry)["success"]
    original_apply = MemoryStoreTransaction.apply

    def corrupt_receipt(self, operations, *, batch=False):
        result = original_apply(self, operations, batch=batch)
        field = "replaced_entries" if batch else "replaced_entry"
        if fault == "missing":
            result.pop(field)
        else:
            wrong = "Nonexistent fact" if fault == "unknown" else "Other fact"
            result[field] = {1: wrong} if batch else wrong
        return result

    monkeypatch.setattr(MemoryStoreTransaction, "apply", corrupt_receipt)
    op = dict(action="replace", old_text="Original fact", content="Updated fact",
              reason="Refresh fixture", evidence="Verified fixture")
    args: dict[str, Any] = {"operations": [op]} if batch else op
    result = json.loads(memory_tool(store=store, **args))
    assert result["success"] is False
    assert "rolled back" in result["error"]
    assert store.read_target_entries_checked("memory") == (original_entries, True)
    assert change_records(tmp_path) == []


def test_mixed_batch_receipts_preserve_operation_positions_and_intermediate_values(store, tmp_path):
    original = 'Project “Alpha” uses Python'
    kept = "Unrelated durable fact"
    for entry in [original, kept]:
        assert store.add("memory", entry)["success"]
    operations = [
        {"action": "add", "content": kept},  # duplicate: no event, still consumes position 1
        {"action": "replace", "old_text": 'Project "Alpha"', "new_text": "Intermediate fact"},
        {"action": "remove", "old_text": "Intermediate fact", "deletion_type": "safe"},
        {"action": "add", "content": "New fact"},
        {"action": "replace", "old_text": "New fact", "content": "Final fact"},
    ]
    operations = [{**op, "reason": "Consolidate fixture", "evidence": "Verified fixture"}
                  for op in operations]
    result = json.loads(memory_tool(store=store, operations=operations))
    assert result["success"] is True
    assert result["replaced_entries"] == {"2": original, "5": "New fact"}
    assert result["removed_entries"] == {"3": "Intermediate fact"}
    assert store.read_target_entries_checked("memory") == ([kept, "Final fact"], True)
    records = change_records(tmp_path)
    assert [(r["before"], r["after"]) for r in records] == [
        (original, "Intermediate fact"), ("Intermediate fact", None),
        (None, "New fact"), ("New fact", "Final fact"),
    ]
    assert len({r["transaction_id"] for r in records}) == 1


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("action", ["replace", "remove"])
def test_whole_entry_priority_is_preserved_in_audit(store, tmp_path, batch, action):
    for entry in ["Long Alpha policy", "Alpha"]:
        assert store.add("memory", entry)["success"]
    op = dict(action=action, old_text="Alpha", content="Updated policy",
              reason="Refresh fixture", evidence="Verified fixture", deletion_type="safe")
    args: dict[str, Any] = {"operations": [op]} if batch else op
    result = json.loads(memory_tool(store=store, **args))
    assert result["success"] is True
    expected = "Updated policy" if action == "replace" else None
    assert [(r["before"], r["after"]) for r in change_records(tmp_path)] == [("Alpha", expected)]
    assert store.read_target_entries_checked("memory") == (
        ["Long Alpha policy"] + ([expected] if expected else []), True,
    )


@pytest.mark.parametrize("batch", [False, True])
def test_staged_approval_keeps_audit_target_when_another_entry_gains_overlap(store, tmp_path, batch):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.skill_provenance import (
        reset_current_write_origin, reset_review_attended,
        set_current_write_origin, set_review_attended,
    )

    for entry in ["Unrelated fact", "Original Alpha policy"]:
        assert store.add("memory", entry)["success"]
    op = dict(action="replace", old_text="Alpha", content="Updated policy",
              reason="Refresh fixture", evidence="Verified approval")
    args: dict[str, Any] = {"operations": [op]} if batch else op
    origin, attended = set_current_write_origin("background_review"), set_review_attended(False)
    try:
        staged = json.loads(memory_tool(store=store, **args))
    finally:
        reset_review_attended(attended)
        reset_current_write_origin(origin)
    assert staged["staged"] is True
    assert store.replace("memory", "Unrelated fact", "New Alpha policy")["success"]
    output = handle_pending_subcommand(wa.MEMORY, ["approve", staged["pending_id"]], memory_store=store)
    assert output is not None and "Approved 1" in output
    assert wa.get_pending(wa.MEMORY, staged["pending_id"]) is None
    assert store.read_target_entries_checked("memory") == (["New Alpha policy", "Updated policy"], True)
    assert [(r["before"], r["after"]) for r in change_records(tmp_path)] == [
        ("Original Alpha policy", "Updated policy"),
    ]
