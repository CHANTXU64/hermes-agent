from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path.home() / ".hermes" / "scripts"
CHECKER_PATH = SCRIPTS_DIR / "check-hermes-hindsight.py"
WRAPPER_PATH = SCRIPTS_DIR / "hindsight_monitor_html.py"
pytestmark = pytest.mark.skipif(
    not CHECKER_PATH.exists() or not WRAPPER_PATH.exists(),
    reason="profile-local Hindsight monitor scripts are unavailable",
)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def source_entry(checker, *, role: str, content: str, message_id: int, active: int = 1, compacted: int = 0):
    return checker.Entry(
        role=role,
        content=content,
        canonical=content,
        source="state_db",
        occurrence_id=f"session:{message_id}",
        session_id="session",
        message_id=message_id,
        timestamp=float(message_id),
        active=active,
        compacted=compacted,
    )


def retain_entry(checker, *, role: str, content: str, index: int):
    return checker.Entry(
        role=role,
        content=f"{role.title()}: {content}",
        canonical=content,
        source="retain_db",
        occurrence_id=f"retain:{index}:0",
        active=1,
        document_index=index,
    )


def document_entry(checker, *, role: str, content: str, index: int):
    return checker.Entry(
        role=role,
        content=f"{role.title()}: {content}",
        canonical=content,
        source="hindsight_document",
        occurrence_id=f"document:{index}:0",
        document_index=index,
    )


def test_inactive_compacted_source_explains_existing_document_but_never_becomes_expected():
    checker = load_module("monitor_inactive_expected", CHECKER_PATH)
    historical = source_entry(
        checker,
        role="user",
        content="historical",
        message_id=1,
        active=0,
        compacted=1,
    )

    absent_candidates = checker.compare_entries([historical], [], [])
    saved_candidates = checker.compare_entries(
        [historical],
        [document_entry(checker, role="user", content="historical", index=0)],
        [],
    )

    assert checker.review_candidates(absent_candidates, reviewed_keys=set()) == []
    assert checker.review_candidates(saved_candidates, reviewed_keys=set()) == []


def test_candidate_level_stage_distinguishes_mixed_boundaries_and_local_anchors():
    checker = load_module("monitor_candidate_stage", CHECKER_PATH)
    source = [
        source_entry(checker, role="assistant", content="A", message_id=1),
        source_entry(checker, role="assistant", content="B", message_id=2),
        source_entry(checker, role="assistant", content="C", message_id=3),
    ]
    local = [
        retain_entry(checker, role="assistant", content="A", index=0),
        retain_entry(checker, role="assistant", content="B", index=1),
    ]
    document = [document_entry(checker, role="assistant", content="A", index=0)]
    candidates = checker.compare_entries(source, document, [])

    checker.annotate_candidate_stages(candidates, source, local, document)

    by_message_id = {
        item["source_examples"][0].message_id: item
        for item in candidates
        if item.get("type") == "source_assistant_absent_from_document"
    }
    assert by_message_id[2]["candidate_failure_stage"] == "after_local_retain"
    assert by_message_id[2]["source_count"] == 1
    assert by_message_id[2]["local_retain_count"] == 1
    assert by_message_id[2]["document_count"] == 0
    assert [entry.canonical for entry in by_message_id[2]["local_retain_examples"]] == ["B"]
    assert by_message_id[3]["candidate_failure_stage"] == "before_local_retain"
    assert by_message_id[3]["local_retain_examples"] == []


def test_document_has_no_user_candidate_uses_expected_active_source_boundary():
    checker = load_module("monitor_no_user_stage", CHECKER_PATH)
    source = [source_entry(checker, role="user", content="question", message_id=1)]
    local = [retain_entry(checker, role="user", content="question", index=0)]
    candidates = checker.compare_entries(source, [], [])

    checker.annotate_candidate_stages(candidates, source, local, [])

    no_user = next(item for item in candidates if item.get("type") == "document_has_no_user_entries")
    assert no_user["candidate_failure_stage"] == "after_local_retain"
    assert no_user["source_count"] == 1
    assert no_user["local_retain_count"] == 1
    assert no_user["document_count"] == 0


def test_wrapper_defers_state_and_commits_only_after_successful_delivery(monkeypatch):
    wrapper = load_module("monitor_wrapper_state_success", WRAPPER_PATH)
    checkpoint = {
        "last_checked_at": "2026-07-18T17:00:00",
        "document_audit_completed_at": "2026-07-18T09:00:00+00:00",
    }
    context = {
        "log_alerts": [],
        "health_errors": [],
        "document_audit": {"status": "ok", "documents": [{"document_id": "doc1"}]},
    }
    evidence = [{"index": {"items": [{"candidate_key": "key-a"}]}, "detail": {"items": []}}]
    events: list[tuple] = []
    monkeypatch.setattr(
        wrapper,
        "run_json",
        lambda args: (
            events.append(("checker_args", tuple(args)))
            or {"wakeAgent": True, "state_checkpoint": checkpoint, "context": context}
        ),
    )
    monkeypatch.setattr(wrapper, "collect_evidence", lambda _context: evidence)
    monkeypatch.setattr(wrapper, "review", lambda _context, _evidence: "# report")
    monkeypatch.setattr(wrapper, "deliver", lambda _report: events.append(("deliver",)))
    monkeypatch.setattr(
        wrapper,
        "commit_monitor_state",
        lambda state_checkpoint, keys: events.append(("commit", state_checkpoint, keys)),
    )

    assert wrapper.main() == 0
    assert "--defer-state" in events[0][1]
    assert events[-2:] == [("deliver",), ("commit", checkpoint, {"key-a"})]


def test_wrapper_does_not_commit_checkpoint_after_delivery_failure(monkeypatch):
    wrapper = load_module("monitor_wrapper_state_failure", WRAPPER_PATH)
    checkpoint = {"last_checked_at": "2026-07-18T17:00:00"}
    context = {
        "log_alerts": [],
        "health_errors": [],
        "document_audit": {"status": "ok", "documents": [{"document_id": "doc1"}]},
    }
    evidence = [{"index": {"items": [{"candidate_key": "key-a"}]}, "detail": {"items": []}}]
    commits: list[tuple] = []
    monkeypatch.setattr(
        wrapper,
        "run_json",
        lambda _args: {"wakeAgent": True, "state_checkpoint": checkpoint, "context": context},
    )
    monkeypatch.setattr(wrapper, "collect_evidence", lambda _context: evidence)
    monkeypatch.setattr(wrapper, "review", lambda _context, _evidence: "# report")
    monkeypatch.setattr(wrapper, "deliver", lambda _report: (_ for _ in ()).throw(RuntimeError("mail failed")))
    monkeypatch.setattr(wrapper, "commit_monitor_state", lambda state_checkpoint, keys: commits.append((state_checkpoint, keys)))

    assert wrapper.main() == 1
    assert commits == []


def test_wrapper_commits_after_successful_silent_review(monkeypatch):
    wrapper = load_module("monitor_wrapper_review_silent", WRAPPER_PATH)
    checkpoint = {"last_checked_at": "2026-07-18T17:00:00"}
    context = {
        "log_alerts": [],
        "health_errors": [],
        "document_audit": {"status": "ok", "documents": [{"document_id": "doc1"}]},
    }
    evidence = [{"index": {"items": [{"candidate_key": "key-a"}]}, "detail": {"items": []}}]
    commits: list[tuple] = []
    monkeypatch.setattr(
        wrapper,
        "run_json",
        lambda _args: {"wakeAgent": True, "state_checkpoint": checkpoint, "context": context},
    )
    monkeypatch.setattr(wrapper, "collect_evidence", lambda _context: evidence)
    monkeypatch.setattr(wrapper, "review", lambda _context, _evidence: "[SILENT]")
    monkeypatch.setattr(wrapper, "commit_monitor_state", lambda state_checkpoint, keys: commits.append((state_checkpoint, keys)))

    assert wrapper.main() == 0
    assert commits == [(checkpoint, {"key-a"})]


def test_wrapper_does_not_commit_silent_review_when_direct_alert_exists(monkeypatch):
    wrapper = load_module("monitor_wrapper_review_conflict", WRAPPER_PATH)
    checkpoint = {"last_checked_at": "2026-07-18T17:00:00"}
    context = {
        "log_alerts": [{"signature": "hindsight_failure"}],
        "health_errors": [],
        "document_audit": {"status": "ok", "documents": [{"document_id": "doc1"}]},
    }
    evidence = [{"index": {"items": [{"candidate_key": "key-a"}]}, "detail": {"items": []}}]
    commits: list[tuple] = []
    monkeypatch.setattr(
        wrapper,
        "run_json",
        lambda _args: {"wakeAgent": True, "state_checkpoint": checkpoint, "context": context},
    )
    monkeypatch.setattr(wrapper, "collect_evidence", lambda _context: evidence)
    monkeypatch.setattr(wrapper, "review", lambda _context, _evidence: "[SILENT]")
    monkeypatch.setattr(wrapper, "deliver", lambda _report: None)
    monkeypatch.setattr(wrapper, "commit_monitor_state", lambda state_checkpoint, keys: commits.append((state_checkpoint, keys)))

    assert wrapper.main() == 1
    assert commits == []


def test_wrapper_commits_checkpoint_when_no_agent_review_is_needed(monkeypatch):
    wrapper = load_module("monitor_wrapper_state_silent", WRAPPER_PATH)
    checkpoint = {"last_checked_at": "2026-07-18T17:00:00"}
    commits: list[tuple] = []
    monkeypatch.setattr(
        wrapper,
        "run_json",
        lambda _args: {"wakeAgent": False, "state_checkpoint": checkpoint},
    )
    monkeypatch.setattr(wrapper, "commit_monitor_state", lambda state_checkpoint, keys: commits.append((state_checkpoint, keys)))

    assert wrapper.main() == 0
    assert commits == [(checkpoint, set())]


def test_commit_monitor_state_preserves_fields_prunes_old_keys_and_updates_checkpoint(tmp_path, monkeypatch):
    wrapper = load_module("monitor_wrapper_atomic_state", WRAPPER_PATH)
    state_path = tmp_path / "monitor-state.json"
    now = wrapper.datetime(2026, 7, 18, 8, 0, tzinfo=wrapper.timezone.utc)
    state_path.write_text(
        json.dumps(
            {
                "unrelated": "keep",
                "reviewed_candidate_keys": {
                    "fresh": "2026-07-17T08:00:00+00:00",
                    "expired": "2026-05-01T08:00:00+00:00",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(wrapper, "STATE_PATH", state_path)
    checkpoint = {
        "last_checked_at": "2026-07-18T16:00:00",
        "document_audit_completed_at": "2026-07-18T08:00:00+00:00",
    }

    wrapper.commit_monitor_state(checkpoint, {"new"}, now=now)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["unrelated"] == "keep"
    assert state["last_checked_at"] == checkpoint["last_checked_at"]
    assert state["document_audit_completed_at"] == checkpoint["document_audit_completed_at"]
    assert set(state["reviewed_candidate_keys"]) == {"fresh", "new"}


def test_commit_monitor_state_fails_closed_on_invalid_existing_state(tmp_path, monkeypatch):
    wrapper = load_module("monitor_wrapper_invalid_state", WRAPPER_PATH)
    state_path = tmp_path / "monitor-state.json"
    state_path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(wrapper, "STATE_PATH", state_path)

    with pytest.raises(json.JSONDecodeError):
        wrapper.commit_monitor_state({"last_checked_at": "2026-07-18T16:00:00"}, {"new"})

    assert state_path.read_text(encoding="utf-8") == "{broken"


def test_candidate_key_suppresses_same_reviewed_event():
    checker = load_module("monitor_candidate_dedupe", CHECKER_PATH)
    item = checker.candidate(
        "source_assistant_absent_from_document",
        "assistant",
        source_examples=[source_entry(checker, role="assistant", content="answer", message_id=2)],
    )
    checker.attach_candidate_review_keys("doc1", [item])

    assert checker.review_candidates([item], reviewed_keys=set()) == [item]
    assert checker.review_candidates([item], reviewed_keys={item["candidate_key"]}) == []


def test_candidate_key_changes_when_local_boundary_evidence_changes():
    checker = load_module("monitor_candidate_stage_key", CHECKER_PATH)
    source = [source_entry(checker, role="assistant", content="answer", message_id=2)]
    before = checker.candidate(
        "source_assistant_absent_from_document",
        "assistant",
        source_examples=source,
    )
    before.update(
        {
            "candidate_failure_stage": "before_local_retain",
            "source_count": 1,
            "local_retain_count": 0,
            "document_count": 0,
            "local_retain_examples": [],
        }
    )
    after = dict(before)
    after.update(
        {
            "candidate_failure_stage": "after_local_retain",
            "local_retain_count": 1,
            "local_retain_examples": [retain_entry(checker, role="assistant", content="answer", index=0)],
        }
    )

    assert checker.candidate_review_key("doc1", before) != checker.candidate_review_key("doc1", after)


def test_evidence_index_contains_all_locators_and_local_stage_anchor():
    checker = load_module("monitor_evidence_locators", CHECKER_PATH)
    item = checker.candidate(
        "source_assistant_absent_from_document",
        "assistant",
        source_examples=[
            source_entry(checker, role="assistant", content="same", message_id=2),
            source_entry(checker, role="assistant", content="same", message_id=3),
        ],
        count=2,
    )
    item["local_retain_examples"] = [retain_entry(checker, role="assistant", content="same", index=0)]
    item["candidate_failure_stage"] = "multiple_or_ambiguous"

    public = checker.candidate_index_item(item)

    assert [entry["message_id"] for entry in public["source_locators"]] == [2, 3]
    assert len(public["local_retain_locators"]) == 1
    assert len(public["source_examples"]) == 1
