"""Regression cases from the monitor's failure/coverage distinction."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.fork.test_hindsight_retain_integrity import (
    create_accepted_attempt, load_module, isolated_legacy_home,
)


@pytest.mark.parametrize("failure_kind", ["export_failed", "scheduled"])
@pytest.mark.parametrize("verification", ["valid", "older_cutoff", "missing_cutoff", "remote_mismatch", "pending", "partial"])
def test_verified_same_cutoff_save_covers_failed_attempt(tmp_path, failure_kind, verification):
    module = load_module()
    saved_at = datetime(2026, 10, 9, 14, 34, tzinfo=timezone.utc)
    cutoff = saved_at - timedelta(minutes=3)
    sid = "covered-session"
    saved_id = "8a41dfff-47b0-4d71-9cb5-d95215a573df"
    failed_id = "2e9ad449-8a33-4d77-908d-94cdc35d5ad2"
    state, journal, result = create_accepted_attempt(
        module, tmp_path, session_id=sid, attempt_id=saved_id, started_at=saved_at,
    )
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    for event in events:
        if event["event"] == "started":
            event["cutoff_at"] = cutoff.isoformat()
            if verification == "older_cutoff":
                event["cutoff_at"] = (cutoff - timedelta(seconds=1)).isoformat()
            elif verification == "missing_cutoff":
                del event["cutoff_at"]
            elif verification == "partial":
                event["repair_scope"] = "confirmed_missing_only"
    failed_at = saved_at + timedelta(minutes=17)
    for event in (["scheduled"] if failure_kind == "scheduled" else ["started", "export_failed"]):
        events.append({
            "attempt_id": failed_id, "event": event, "recorded_at": failed_at.isoformat(),
            "session_id": sid, "document_id": sid, "remote_expectation": "expected",
            "cutoff_at": cutoff.isoformat(), "due_at": failed_at.isoformat(),
        })
    journal.write_text("".join(json.dumps(e) + "\n" for e in events))
    candidate = json.loads((Path(result["output_dir"]) / f"candidate_document_{sid}.json").read_text())
    before = journal.read_bytes()
    scan = module.scan_attempts(
        journal_path=journal, state_db_path=state, now=failed_at + timedelta(hours=2),
        operation_fetcher=lambda operation_id: {"status": "found", "operation": {
            "id": operation_id, "task_type": "batch_retain",
            "status": "pending" if verification == "pending" else "completed",
            "document_id": sid, "extraction_errors_count": 0,
        }},
        document_fetcher=lambda document_id: {"status": "found", "document": {
            "id": document_id, "original_text": candidate["document_content"] + ("mismatch" if verification == "remote_mismatch" else ""),
        }},
    )
    notice = next(a for a in scan["alerts"] if a.get("attempt_id") == failed_id)
    if verification != "valid":
        assert notice["type"] == ("retain_scheduled_worker_missing" if failure_kind == "scheduled" else "retain_export_failed")
        assert journal.read_bytes() == before
        return
    assert notice["type"] == "retain_save_covered"
    assert notice["severity"] == "info"
    assert notice["original_type"] == ("retain_scheduled_worker_missing" if failure_kind == "scheduled" else "retain_export_failed")
    assert notice["recovered_by_attempt_id"] == saved_id
    assert notice["remote_document_matches_candidate"] is True
    assert journal.read_bytes() == before


@pytest.mark.parametrize("manifest_state", ["not_recorded", "recorded_but_missing"])
def test_legacy_candidate_receipt_is_sufficient_without_optional_manifest(tmp_path, manifest_state):
    module = load_module()
    sid = "legacy-receipt"
    saved_id = "8a41dfff-47b0-4d71-9cb5-d95215a573df"
    saved_at = datetime(2026, 9, 9, 4, 50, tzinfo=timezone.utc)
    state, journal, result = create_accepted_attempt(
        module, tmp_path, session_id=sid, attempt_id=saved_id, started_at=saved_at,
    )
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    succeeded = next(e for e in events if e["event"] == "export_succeeded")
    Path(succeeded["manifest_path"]).unlink()
    if manifest_state == "not_recorded":
        del succeeded["manifest_path"]
    journal.write_text("".join(json.dumps(e) + "\n" for e in events))
    candidate = json.loads((Path(result["output_dir"]) / f"candidate_document_{sid}.json").read_text())
    scan = module.scan_attempts(
        journal_path=journal, state_db_path=state, now=saved_at + timedelta(hours=1),
        operation_fetcher=lambda op: {"status": "found", "operation": {
            "id": op, "task_type": "batch_retain", "status": "completed",
            "document_id": sid, "extraction_errors_count": 0,
        }},
        document_fetcher=lambda doc: {"status": "found", "document": {
            "id": doc, "original_text": candidate["document_content"],
        }},
    )
    if manifest_state == "not_recorded":
        assert scan["alerts"] == []
        assert scan["remote_confirmed_attempts"][0]["attempt_id"] == saved_id
    else:
        assert scan["alerts"][0]["type"] == "retain_export_artifacts_invalid"
