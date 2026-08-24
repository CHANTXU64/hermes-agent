"""Behavior boundaries for direct-entry Smart Approval evidence."""

import pytest

from fork_features.approval.script_evidence import (
    MAX_SCRIPT_BYTES,
    collect_direct_script_evidence,
)


def test_reads_only_the_direct_python_entry_script(tmp_path):
    entry = tmp_path / "entry.py"
    helper = tmp_path / "helper.py"
    entry.write_text("import helper\nprint(helper.VALUE)\n", encoding="utf-8")
    helper.write_text("VALUE = 'implementation detail'\n", encoding="utf-8")

    evidence = collect_direct_script_evidence(
        "python entry.py",
        cwd=str(tmp_path),
        source_kind="shell",
    )

    assert evidence == [
        {
            "path": str(entry),
            "status": "read",
            "content": entry.read_text(encoding="utf-8"),
        }
    ]
    assert all(item["path"] != str(helper) for item in evidence)


def test_copy_then_execute_reads_destination_only(tmp_path):
    source = tmp_path / "source.py"
    destination = tmp_path / "destination.py"
    source.write_text("print('source')\n", encoding="utf-8")
    destination.write_text("print('destination')\n", encoding="utf-8")

    evidence = collect_direct_script_evidence(
        "cp source.py destination.py && python destination.py",
        cwd=str(tmp_path),
        source_kind="shell",
    )

    assert evidence == [
        {
            "path": str(destination),
            "status": "read",
            "content": destination.read_text(encoding="utf-8"),
        }
    ]
    assert all(item["path"] != str(source) for item in evidence)


def test_missing_and_oversized_entries_are_bounded_evidence_gaps(tmp_path):
    oversized = tmp_path / "oversized.py"
    oversized.write_text("x" * (MAX_SCRIPT_BYTES + 1), encoding="utf-8")

    missing = collect_direct_script_evidence(
        "python missing.py",
        cwd=str(tmp_path),
        source_kind="shell",
    )
    too_large = collect_direct_script_evidence(
        "python oversized.py",
        cwd=str(tmp_path),
        source_kind="shell",
    )

    assert missing == [
        {"path": str(tmp_path / "missing.py"), "status": "unreadable", "content": ""}
    ]
    assert too_large == [
        {"path": str(oversized), "status": "unreadable", "content": ""}
    ]


def test_execute_code_evidence_does_not_scan_unreferenced_files(tmp_path):
    unrelated = tmp_path / "unrelated.py"
    unrelated.write_text("delete_everything()\n", encoding="utf-8")

    evidence = collect_direct_script_evidence(
        "print('current code only')",
        cwd=str(tmp_path),
        source_kind="python",
    )

    assert evidence == []


@pytest.mark.parametrize(
    "command",
    [
        "python -W ignore actual.py",
        "python -X dev actual.py",
        "sudo -u root python actual.py",
        "env -u UNUSED python actual.py",
    ],
)
def test_launcher_options_do_not_replace_the_direct_script(command, tmp_path):
    actual = tmp_path / "actual.py"
    actual.write_text("print('actual')\n", encoding="utf-8")

    evidence = collect_direct_script_evidence(
        command,
        cwd=str(tmp_path),
        source_kind="shell",
    )

    assert evidence == [
        {"path": str(actual), "status": "read", "content": "print('actual')\n"}
    ]


@pytest.mark.parametrize(
    "code",
    [
        'subprocess.run(["python", "-W", "ignore", "actual.py"])',
        'subprocess.run(["sudo", "-u", "root", "python", "actual.py"])',
    ],
)
def test_execute_code_launcher_options_keep_the_direct_script(code, tmp_path):
    actual = tmp_path / "actual.py"
    actual.write_text("print('actual')\n", encoding="utf-8")

    evidence = collect_direct_script_evidence(
        code,
        cwd=str(tmp_path),
        source_kind="python",
    )

    assert evidence == [
        {"path": str(actual), "status": "read", "content": "print('actual')\n"}
    ]


def test_duplicate_paths_do_not_consume_the_unique_script_limit(tmp_path):
    repeated = tmp_path / "repeated.py"
    final = tmp_path / "final.py"
    repeated.write_text("print('repeated')\n", encoding="utf-8")
    final.write_text("print('final')\n", encoding="utf-8")
    command = "; ".join(
        ["python repeated.py"] * 4 + ["python final.py"]
    )

    evidence = collect_direct_script_evidence(
        command,
        cwd=str(tmp_path),
        source_kind="shell",
    )

    assert evidence == [
        {
            "path": str(repeated),
            "status": "read",
            "content": "print('repeated')\n",
        },
        {"path": str(final), "status": "read", "content": "print('final')\n"},
    ]
