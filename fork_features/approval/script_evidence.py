"""Collect bounded evidence from scripts directly launched by an action.

This Fork-owned module deliberately limits inspection to direct entry scripts.
It does not traverse imports or dependency trees.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
from collections.abc import Callable
from typing import Optional

MAX_SCRIPT_BYTES = 32_000
MAX_SCRIPT_COUNT = 4


def _shell_segments(source: str) -> list[list[str]]:
    """Tokenize direct shell commands without executing expansions."""
    try:
        lexer = shlex.shlex(source, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except Exception:
        return []
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and all(ch in ";&|" for ch in token):
            if segments[-1]:
                segments.append([])
            continue
        segments[-1].append(token)
    return [segment for segment in segments if segment]


def _looks_like_script_path(value: str) -> bool:
    lowered = value.lower()
    return (
        value.startswith(("./", "../", "~/"))
        or lowered.endswith(
            (
                ".py",
                ".sh",
                ".bash",
                ".zsh",
                ".js",
                ".mjs",
                ".cjs",
                ".rb",
                ".pl",
                ".ps1",
            )
        )
    )


_SUDO_OPTIONS_WITH_VALUE = {
    "-C",
    "--close-from",
    "-D",
    "--chdir",
    "-g",
    "--group",
    "-h",
    "--host",
    "-p",
    "--prompt",
    "-R",
    "--chroot",
    "-r",
    "--role",
    "-T",
    "--command-timeout",
    "-t",
    "--type",
    "-u",
    "--user",
}
_ENV_OPTIONS_WITH_VALUE = {
    "-C",
    "--chdir",
    "-S",
    "--split-string",
    "-u",
    "--unset",
}
_PYTHON_OPTIONS_WITH_VALUE = {"-W", "-X", "--check-hash-based-pycs"}
_INTERPRETERS = {
    "bash",
    "sh",
    "zsh",
    "dash",
    "fish",
    "node",
    "ruby",
    "perl",
    "pwsh",
    "powershell",
    "powershell.exe",
}


def _skip_launcher_options(
    args: list[str],
    *,
    options_with_value: set[str],
    allow_assignments: bool = False,
) -> list[str]:
    """Return argv after one known launcher without interpreting its command."""
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            return args[index + 1 :]
        if allow_assignments and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", arg):
            index += 1
            continue
        if arg == "-" or not arg.startswith("-"):
            return args[index:]
        if arg in options_with_value:
            index += 2
        else:
            # Attached short values (`-uroot`) and long `--key=value` forms
            # are already contained in this token.
            index += 1
    return []


def _unwrap_known_launchers(argv: list[str]) -> list[str]:
    """Remove finite sudo/env prefixes shared by shell and literal subprocess argv."""
    remaining = list(argv)
    while remaining:
        executable = os.path.basename(remaining[0]).lower()
        if executable == "sudo":
            remaining = _skip_launcher_options(
                remaining[1:],
                options_with_value=_SUDO_OPTIONS_WITH_VALUE,
            )
            continue
        if executable == "env":
            remaining = _skip_launcher_options(
                remaining[1:],
                options_with_value=_ENV_OPTIONS_WITH_VALUE,
                allow_assignments=True,
            )
            continue
        break
    return remaining


def _direct_interpreter_script_arg(
    args: list[str],
    *,
    options_with_value: Optional[set[str]] = None,
) -> Optional[str]:
    """Return a directly executed script path, excluding inline/stdin code.

    Shell lexing keeps heredoc operators such as ``<<PY`` in the argv-like
    token stream. They are redirections, not script filenames. A real script
    before a redirection (``python reader.py <<EOF``) still wins, while ``-``
    and heredoc/here-string input mean the code is already visible inline in
    the command block.
    """
    options_ended = False
    index = 0
    while index < len(args):
        arg = args[index]
        if not options_ended and arg == "--":
            options_ended = True
            index += 1
            continue
        if arg == "-":
            return None

        redirection = re.sub(r"^\d+", "", arg)
        if redirection.startswith(("<<", "<<<")):
            return None
        if redirection == "<":
            return args[index + 1] if index + 1 < len(args) else None
        if redirection.startswith("<"):
            return redirection[1:] or None
        if redirection.startswith((">", "&>")):
            return None

        if not options_ended and options_with_value and arg in options_with_value:
            index += 2
            continue
        if not options_ended and arg.startswith("-"):
            index += 1
            continue
        return arg
    return None


def _direct_argv_script_path(argv: list[str]) -> Optional[str]:
    """Return the direct script from one finite, literal launcher argv."""
    segment = _unwrap_known_launchers(argv)
    if not segment:
        return None
    executable = os.path.basename(segment[0]).lower()
    args = segment[1:]
    if executable in {"source", "."}:
        return args[0] if args else None
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", executable):
        if any(
            arg in {"-c", "-m"} or arg.startswith(("-c=", "-m="))
            for arg in args
        ):
            return None
        return _direct_interpreter_script_arg(
            args,
            options_with_value=_PYTHON_OPTIONS_WITH_VALUE,
        )
    if executable in _INTERPRETERS:
        if any(arg in {"-c", "-Command", "-EncodedCommand"} for arg in args):
            return None
        return _direct_interpreter_script_arg(args)
    return segment[0] if _looks_like_script_path(segment[0]) else None


def _direct_shell_script_paths(source: str) -> list[str]:
    paths: list[str] = []
    for raw_segment in _shell_segments(source):
        segment = list(raw_segment)
        while segment and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", segment[0]):
            segment.pop(0)
        if not segment:
            continue
        script = _direct_argv_script_path(segment)
        if script:
            paths.append(script)
    return paths


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _literal_argv(node: ast.AST) -> list[Optional[str]] | str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    values: list[Optional[str]] = []
    for item in node.elts:
        if isinstance(item, ast.Constant) and isinstance(item.value, str):
            values.append(item.value)
        elif _call_name(item) == "sys.executable":
            values.append("<python-executable>")
        else:
            values.append(None)
    return values


def _direct_python_script_paths(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    paths: list[str] = []
    subprocess_calls = {
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "os.system",
        "os.popen",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name == "runpy.run_path" and node.args:
            value = _literal_argv(node.args[0])
            if isinstance(value, str):
                paths.append(value)
            continue
        if name not in subprocess_calls or not node.args:
            continue
        argv = _literal_argv(node.args[0])
        if isinstance(argv, str):
            paths.extend(_direct_shell_script_paths(argv))
            continue
        if not isinstance(argv, list) or not argv:
            continue
        if any(value is None for value in argv):
            continue
        literal_argv = [
            "python" if value == "<python-executable>" else str(value)
            for value in argv
        ]
        script = _direct_argv_script_path(literal_argv)
        if script:
            paths.append(script)
    return paths


def _is_virtualenv_console_entrypoint(path: str) -> bool:
    """Return whether *path* is a package-managed virtualenv command entry."""
    script_suffixes = {
        ".py",
        ".sh",
        ".bash",
        ".zsh",
        ".js",
        ".mjs",
        ".cjs",
        ".rb",
        ".pl",
        ".ps1",
    }
    if os.path.splitext(path)[1].lower() in script_suffixes:
        return False
    entry_dir = os.path.dirname(path)
    if os.path.basename(entry_dir).lower() not in {"bin", "scripts"}:
        return False
    environment_root = os.path.dirname(entry_dir)
    return (
        os.path.isfile(os.path.join(environment_root, "pyvenv.cfg"))
        and os.path.isfile(path)
        and os.access(path, os.X_OK)
    )


def collect_direct_script_evidence(
    source: str,
    *,
    cwd: Optional[str] = None,
    source_kind: str = "shell",
    read_script: Optional[Callable[[str], Optional[str]]] = None,
) -> list[dict[str, str]]:
    """Read bounded contents of scripts directly launched by this action."""
    raw_paths = (
        _direct_python_script_paths(source)
        if source_kind == "python"
        else _direct_shell_script_paths(source)
    )
    evidence: list[dict[str, str]] = []
    seen: set[str] = set()
    base = os.path.abspath(os.path.expanduser(cwd or os.getcwd()))
    has_additional_scripts = False
    for raw_path in raw_paths:
        expanded = os.path.expanduser(raw_path)
        resolved = os.path.abspath(
            expanded if os.path.isabs(expanded) else os.path.join(base, expanded)
        )
        if resolved in seen:
            continue
        if _is_virtualenv_console_entrypoint(resolved):
            continue
        seen.add(resolved)
        if len(seen) > MAX_SCRIPT_COUNT:
            has_additional_scripts = True
            break
        content: Optional[str] = None
        try:
            path = os.path.realpath(resolved)
            if read_script is not None:
                candidate = read_script(resolved)
                if (
                    isinstance(candidate, str)
                    and len(candidate.encode("utf-8", errors="replace"))
                    <= MAX_SCRIPT_BYTES
                    and "\x00" not in candidate
                ):
                    content = candidate
            elif os.path.isfile(path):
                with open(path, "rb") as handle:
                    data = handle.read(MAX_SCRIPT_BYTES + 1)
                if len(data) <= MAX_SCRIPT_BYTES and b"\x00" not in data:
                    content = data.decode("utf-8", errors="replace")
        except Exception:
            content = None
        evidence.append(
            {
                "path": resolved,
                "status": "read" if content is not None else "unreadable",
                "content": content or "",
            }
        )
    if has_additional_scripts:
        evidence.append(
            {
                "path": "<additional-direct-scripts>",
                "status": "unreadable",
                "content": "",
            }
        )
    return evidence
