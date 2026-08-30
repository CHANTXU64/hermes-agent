"""Behavior boundaries for bounded Smart Approval script evidence."""

import os
import subprocess

import pytest

from fork_features.approval.script_evidence import (
    MAX_SCRIPT_BYTES,
    collect_direct_script_evidence,
)


def _init_git(path) -> None:
    subprocess.run(
        ["git", "init", "--quiet", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_reads_direct_entry_without_following_local_python_import(tmp_path):
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


def test_skips_git_tracked_direct_entry_source(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    script = project / "tracked.py"
    script.write_text("print('tracked')\n", encoding="utf-8")
    _init_git(project)
    subprocess.run(
        ["git", "-C", str(project), "add", "tracked.py"],
        check=True,
        capture_output=True,
        text=True,
    )
    read_paths = []

    evidence = collect_direct_script_evidence(
        "python tracked.py",
        cwd=str(project),
        source_kind="shell",
        read_script=lambda path: read_paths.append(path) or "not expected",
    )

    assert evidence == [
        {"path": str(script), "status": "skipped_git_tracked", "content": ""}
    ]
    assert read_paths == []


def test_reads_untracked_direct_entry_inside_git_worktree(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    script = project / "untracked.py"
    script.write_text("print('untracked')\n", encoding="utf-8")
    _init_git(project)

    evidence = collect_direct_script_evidence(
        "python untracked.py",
        cwd=str(project),
        source_kind="shell",
    )

    assert evidence == [
        {
            "path": str(script),
            "status": "read",
            "content": "print('untracked')\n",
        }
    ]


def test_new_git_tracking_takes_effect_without_process_restart(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    script = project / "entry.py"
    script.write_text("print('entry')\n", encoding="utf-8")

    before = collect_direct_script_evidence(
        "python entry.py",
        cwd=str(project),
        source_kind="shell",
    )
    _init_git(project)
    subprocess.run(
        ["git", "-C", str(project), "add", "entry.py"],
        check=True,
        capture_output=True,
        text=True,
    )
    after = collect_direct_script_evidence(
        "python entry.py",
        cwd=str(project),
        source_kind="shell",
    )

    assert before == [
        {"path": str(script), "status": "read", "content": "print('entry')\n"}
    ]
    assert after == [
        {"path": str(script), "status": "skipped_git_tracked", "content": ""}
    ]


def test_reads_untracked_direct_entry_in_other_git_worktree(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    script = project / "untracked.py"
    script.write_text("print('other untracked')\n", encoding="utf-8")
    _init_git(project)
    caller = tmp_path / "caller"
    caller.mkdir()

    evidence = collect_direct_script_evidence(
        f"python {script}",
        cwd=str(caller),
        source_kind="shell",
    )

    assert evidence == [
        {
            "path": str(script),
            "status": "read",
            "content": "print('other untracked')\n",
        }
    ]


def test_oversized_direct_entry_returns_truncated_prefix(tmp_path):
    script = tmp_path / "oversized.py"
    script.write_text("x" * (MAX_SCRIPT_BYTES + 73), encoding="utf-8")

    evidence = collect_direct_script_evidence(
        "python oversized.py",
        cwd=str(tmp_path),
        source_kind="shell",
    )

    assert evidence == [
        {
            "path": str(script),
            "status": "truncated",
            "content": "x" * MAX_SCRIPT_BYTES,
        }
    ]


def test_oversized_remote_reader_result_returns_truncated_prefix(tmp_path):
    script = tmp_path / "remote.py"

    evidence = collect_direct_script_evidence(
        "python remote.py",
        cwd=str(tmp_path),
        source_kind="shell",
        read_script=lambda _path: "y" * (MAX_SCRIPT_BYTES + 73),
    )

    assert evidence == [
        {
            "path": str(script),
            "status": "truncated",
            "content": "y" * MAX_SCRIPT_BYTES,
        }
    ]


def test_reads_static_importlib_file_module_from_execute_code(tmp_path):
    helper = tmp_path / "helper.py"
    helper.write_text("VALUE = 'implementation detail'\n", encoding="utf-8")
    code = """
import importlib.util
import json
from pathlib import Path

module_path = Path("helper.py")
spec = importlib.util.spec_from_file_location("helper", str(module_path))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(json.dumps(module.VALUE))
"""

    evidence = collect_direct_script_evidence(
        code,
        cwd=str(tmp_path),
        source_kind="python",
    )

    assert evidence == [
        {
            "path": str(helper),
            "status": "read",
            "content": helper.read_text(encoding="utf-8"),
        }
    ]


def test_reads_literal_terminal_python_script_from_execute_code(tmp_path):
    script = tmp_path / "audit_hard_standards.py"
    script.write_text("print('audit')\n", encoding="utf-8")
    command = (
        f"/opt/hermes/venv/bin/python {str(script)!r} "
        f"--batch-dir {str(tmp_path / 'batch')!r} "
        f"--output-dir {str(tmp_path / 'hard-audit')!r}"
    )
    code = f"""
from hermes_tools import terminal
result = terminal({command!r}, timeout=180, workdir={str(tmp_path)!r})
print(result['output'])
"""

    evidence = collect_direct_script_evidence(
        code,
        cwd=str(tmp_path),
        source_kind="python",
    )

    assert evidence == [
        {
            "path": str(script),
            "status": "read",
            "content": script.read_text(encoding="utf-8"),
        }
    ]


def test_does_not_guess_nonliteral_terminal_command_script(tmp_path):
    script = tmp_path / "audit_hard_standards.py"
    script.write_text("print('audit')\n", encoding="utf-8")
    code = f"""
from hermes_tools import terminal
target = {str(script)!r}
result = terminal("python " + target)
"""

    evidence = collect_direct_script_evidence(
        code,
        cwd=str(tmp_path),
        source_kind="python",
    )

    assert evidence == []


def test_does_not_expand_standard_or_local_imports(tmp_path):
    entry = tmp_path / "entry.py"
    helper = tmp_path / "helper.py"
    entry.write_text(
        "import json\nimport pathlib\nimport importlib\n"
        "importlib.import_module('helper')\n"
        "from helper import VALUE\nprint(VALUE)\n",
        encoding="utf-8",
    )
    helper.write_text("VALUE = 1\n", encoding="utf-8")

    evidence = collect_direct_script_evidence(
        "python entry.py",
        cwd=str(tmp_path),
        source_kind="shell",
    )

    paths = [item["path"] for item in evidence]
    assert paths == [str(entry)]
    assert all("site-packages" not in path for path in paths)


def test_local_imports_do_not_start_dependency_traversal(tmp_path):
    entry = tmp_path / "entry.py"
    helper = tmp_path / "helper.py"
    nested = tmp_path / "nested.py"
    entry.write_text("import helper\n", encoding="utf-8")
    helper.write_text("import nested\nVALUE = nested.VALUE\n", encoding="utf-8")
    nested.write_text("VALUE = 1\n", encoding="utf-8")

    evidence = collect_direct_script_evidence(
        "python entry.py",
        cwd=str(tmp_path),
        source_kind="shell",
    )

    assert [item["path"] for item in evidence] == [str(entry)]


def test_does_not_expand_local_package_or_relative_imports(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    entry = tmp_path / "entry.py"
    init = package / "__init__.py"
    helper = package / "helper.py"
    entry.write_text("import package.helper\n", encoding="utf-8")
    init.write_text("from .helper import VALUE\n", encoding="utf-8")
    helper.write_text("VALUE = 1\n", encoding="utf-8")

    evidence = collect_direct_script_evidence(
        "python entry.py",
        cwd=str(tmp_path),
        source_kind="shell",
    )

    assert [item["path"] for item in evidence] == [str(entry)]


def test_reads_explicit_dynamic_path_in_task_temp_root(tmp_path):
    task_root = tmp_path.parent / "hindsight-document-candidate-validation"
    task_root.mkdir()
    outside = task_root / "outside_dynamic_module.py"
    content = "VALUE = 'outside'\n"
    outside.write_text(content, encoding="utf-8")
    code = f"""
import importlib.util
spec = importlib.util.spec_from_file_location("outside", {str(outside)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
"""

    try:
        evidence = collect_direct_script_evidence(
            code,
            cwd=str(tmp_path),
            source_kind="python",
        )
    finally:
        outside.unlink()

    assert evidence == [
        {
            "path": str(outside),
            "status": "read",
            "content": content,
        }
    ]


def test_reads_untracked_explicit_path_outside_task_temp_root(tmp_path):
    external_root = tmp_path.parent / "arbitrary-external-project"
    external_root.mkdir()
    script = external_root / "helper.py"
    script.write_text("VALUE = 'external'\\n", encoding="utf-8")
    code = f"""
import runpy
runpy.run_path({str(script)!r})
"""
    read_paths = []

    evidence = collect_direct_script_evidence(
        code,
        cwd=str(tmp_path),
        source_kind="python",
        read_script=lambda path: read_paths.append(path) or "print('not read')\\n",
    )

    assert evidence == [
        {"path": str(script), "status": "read", "content": "print('not read')\\n"}
    ]
    assert read_paths == [str(script)]


@pytest.mark.parametrize(
    "forbidden_path",
    [
        "/usr/lib/python3.13/pathlib.py",
        "/opt/python/lib/python3.13/site-packages/vendor.py",
        "/opt/python/lib/python3.13/dist-packages/vendor.py",
    ],
)
def test_reads_untracked_explicit_external_python_paths(tmp_path, forbidden_path):
    read_paths = []
    code = f"""
import importlib.util
spec = importlib.util.spec_from_file_location("forbidden", {forbidden_path!r})
"""

    evidence = collect_direct_script_evidence(
        code,
        cwd=str(tmp_path),
        source_kind="python",
        read_script=lambda path: read_paths.append(path) or "print('not read')\\n",
    )

    assert evidence == [
        {"path": forbidden_path, "status": "read", "content": "print('not read')\\n"}
    ]
    assert read_paths == [forbidden_path]


def test_skips_git_tracked_hermes_implementation_path(tmp_path):
    hermes_source = os.path.realpath(
        os.path.join(os.path.dirname(__file__), "../../../tools/approval.py")
    )
    read_paths = []
    code = f"""
import runpy
runpy.run_path({hermes_source!r})
"""

    evidence = collect_direct_script_evidence(
        code,
        cwd=str(tmp_path),
        source_kind="python",
        read_script=lambda path: read_paths.append(path) or "print('not read')\\n",
    )

    assert evidence == [
        {"path": hermes_source, "status": "skipped_git_tracked", "content": ""}
    ]
    assert read_paths == []


def test_skips_symlink_to_git_tracked_hermes_implementation(tmp_path):
    hermes_source = os.path.realpath(
        os.path.join(os.path.dirname(__file__), "../../../tools/approval.py")
    )
    task_link = tmp_path / "task_entry.py"
    task_link.symlink_to(hermes_source)
    read_paths = []
    code = f"""
import runpy
runpy.run_path({str(task_link)!r})
"""

    evidence = collect_direct_script_evidence(
        code,
        cwd=str(tmp_path),
        source_kind="python",
        read_script=lambda path: read_paths.append(path) or "print('not read')\\n",
    )

    assert evidence == [
        {
            "path": str(task_link),
            "status": "skipped_git_tracked",
            "content": "",
        }
    ]
    assert read_paths == []


def test_reads_untracked_symlink_target_outside_cwd(tmp_path):
    external_root = tmp_path.parent / "arbitrary-external-project-symlink"
    external_root.mkdir()
    external = external_root / "external.py"
    external.write_text("print('external')\\n", encoding="utf-8")
    link = tmp_path / "entry.py"
    link.symlink_to(external)
    read_paths = []
    code = f"""
import runpy
runpy.run_path({str(link)!r})
"""

    evidence = collect_direct_script_evidence(
        code,
        cwd=str(tmp_path),
        source_kind="python",
        read_script=lambda path: read_paths.append(path) or "print('not read')\\n",
    )

    assert evidence == [
        {"path": str(link), "status": "read", "content": "print('not read')\\n"}
    ]
    assert read_paths == [str(link)]


def test_skips_tracked_explicit_path_in_other_git_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    script = project / "audit.py"
    script.write_text("print('project')\\n", encoding="utf-8")
    outer = tmp_path / "outer"
    outer.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(project)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(project), "add", "audit.py"],
        check=True,
        capture_output=True,
        text=True,
    )
    code = f"""
import runpy
runpy.run_path({str(script)!r})
"""

    evidence = collect_direct_script_evidence(
        code,
        cwd=str(outer),
        source_kind="python",
    )

    assert evidence == [
        {
            "path": str(script),
            "status": "skipped_git_tracked",
            "content": "",
        }
    ]


def test_static_path_binding_respects_call_order(tmp_path):
    actual = tmp_path / "actual.py"
    later = tmp_path / "later.py"
    actual.write_text("print('actual')\\n", encoding="utf-8")
    later.write_text("print('later')\\n", encoding="utf-8")
    code = f"""
import runpy
target = {str(actual)!r}
runpy.run_path(target)
target = {str(later)!r}
"""

    evidence = collect_direct_script_evidence(
        code,
        cwd=str(tmp_path),
        source_kind="python",
    )

    assert [item["path"] for item in evidence] == [str(actual)]


@pytest.mark.parametrize(
    "code",
    [
        "import runpy\\ntarget = get_target()\\nrunpy.run_path(target)\\n",
        "import runpy\\nrunpy.run_path(target)\\ntarget = 'future.py'\\n",
        "import runpy\\ntarget = 'before.py'\\nif condition:\\n    target = 'branch.py'\\nrunpy.run_path(target)\\n",
    ],
)
def test_static_path_binding_drops_uncertain_values(code, tmp_path):
    evidence = collect_direct_script_evidence(
        code,
        cwd=str(tmp_path),
        source_kind="python",
    )

    assert evidence == []


def test_static_path_binding_is_scoped_for_function_locals(tmp_path):
    outer = tmp_path / "outer.py"
    inner = tmp_path / "inner.py"
    outer.write_text("print('outer')\\n", encoding="utf-8")
    inner.write_text("print('inner')\\n", encoding="utf-8")
    code = f"""
import runpy
target = {str(outer)!r}
def inspect():
    target = {str(inner)!r}
    runpy.run_path(target)
inspect()
"""

    evidence = collect_direct_script_evidence(
        code,
        cwd=str(tmp_path),
        source_kind="python",
    )

    assert [item["path"] for item in evidence] == [str(inner)]


def test_recognizes_supported_api_aliases_only(tmp_path):
    terminal_script = tmp_path / "terminal.py"
    loader_script = tmp_path / "loader.py"
    terminal_script.write_text("print('terminal')\\n", encoding="utf-8")
    loader_script.write_text("print('loader')\\n", encoding="utf-8")
    code = f"""
import hermes_tools as ht
from importlib.util import spec_from_file_location as load_spec
from runpy import run_path as execute_path
ht.terminal("python {str(terminal_script)}")
load_spec("loader", {str(loader_script)!r})
execute_path({str(loader_script)!r})
"""

    evidence = collect_direct_script_evidence(
        code,
        cwd=str(tmp_path),
        source_kind="python",
    )

    assert [item["path"] for item in evidence] == [
        str(terminal_script),
        str(loader_script),
    ]


def test_does_not_treat_same_named_or_shadowed_calls_as_apis(tmp_path):
    fake = tmp_path / "fake.py"
    fake.write_text("print('fake')\\n", encoding="utf-8")
    code = f"""
def terminal(command):
    return command

class service:
    def run_path(self, path):
        return path

from hermes_tools import terminal as trusted_terminal
trusted_terminal = lambda command: command
terminal("python {str(fake)}")
service().run_path({str(fake)!r})
trusted_terminal("python {str(fake)}")
"""

    evidence = collect_direct_script_evidence(
        code,
        cwd=str(tmp_path),
        source_kind="python",
    )

    assert evidence == []


@pytest.mark.parametrize(
    "command,script_name",
    [
        ("bash -O extglob actual.sh", "actual.sh"),
        ("bash --rcfile rc actual.sh", "actual.sh"),
        ("node --require helper.js main.js", "main.js"),
        ("ruby -I lib actual.rb", "actual.rb"),
        ("pwsh -ExecutionPolicy Bypass -File actual.ps1", "actual.ps1"),
    ],
)
def test_interpreter_option_values_do_not_replace_direct_script(command, script_name, tmp_path):
    script = tmp_path / script_name
    script.write_text("print('actual')\\n", encoding="utf-8")

    evidence = collect_direct_script_evidence(
        command,
        cwd=str(tmp_path),
        source_kind="shell",
    )

    assert [item["path"] for item in evidence] == [str(script)]


def test_unknown_interpreter_option_does_not_guess_a_script(tmp_path):
    main = tmp_path / "main.js"
    helper = tmp_path / "helper.js"
    main.write_text("console.log('main')\\n", encoding="utf-8")
    helper.write_text("module.exports = {}\\n", encoding="utf-8")

    evidence = collect_direct_script_evidence(
        "node --unknown helper.js main.js",
        cwd=str(tmp_path),
        source_kind="shell",
    )

    assert evidence == []


@pytest.mark.skipif(os.name == "nt", reason="symlink semantics differ on Windows")
def test_unreferenced_symlink_is_not_read(tmp_path):
    outside = tmp_path.parent / "outside_import_module.py"
    link = tmp_path / "helper.py"
    entry = tmp_path / "entry.py"
    outside.write_text("VALUE = 'outside'\n", encoding="utf-8")
    link.symlink_to(outside)
    entry.write_text("import helper\n", encoding="utf-8")

    try:
        evidence = collect_direct_script_evidence(
            "python entry.py",
            cwd=str(tmp_path),
            source_kind="shell",
        )
    finally:
        link.unlink()
        outside.unlink()

    assert evidence == [
        {
            "path": str(entry),
            "status": "read",
            "content": entry.read_text(encoding="utf-8"),
        }
    ]


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


def test_missing_is_gap_and_oversized_entry_is_truncated(tmp_path):
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
        {
            "path": str(oversized),
            "status": "truncated",
            "content": "x" * MAX_SCRIPT_BYTES,
        }
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


def test_duplicate_paths_are_deduplicated_without_hiding_later_scripts(tmp_path):
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
