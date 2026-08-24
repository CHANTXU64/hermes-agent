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


def _direct_interpreter_script_arg(args: list[str]) -> Optional[str]:
    """Return a directly executed script path, excluding inline/stdin code.

    Shell lexing keeps heredoc operators such as ``<<PY`` in the argv-like
    token stream. They are redirections, not script filenames. A real script
    before a redirection (``python reader.py <<EOF``) still wins, while ``-``
    and heredoc/here-string input mean the code is already visible inline in
    the command block.
    """
    options_ended = False
    for index, arg in enumerate(args):
        if not options_ended and arg == "--":
            options_ended = True
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

        if not options_ended and arg.startswith("-"):
            continue
        return arg
    return None


def _direct_shell_script_paths(source: str) -> list[str]:
    paths: list[str] = []
    interpreters = {
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
    for raw_segment in _shell_segments(source):
        segment = list(raw_segment)
        while segment and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", segment[0]):
            segment.pop(0)
        if not segment:
            continue
        if os.path.basename(segment[0]) == "sudo":
            segment.pop(0)
            while segment and segment[0].startswith("-"):
                segment.pop(0)
        if segment and os.path.basename(segment[0]) == "env":
            segment.pop(0)
            while segment and (segment[0].startswith("-") or "=" in segment[0]):
                segment.pop(0)
        if not segment:
            continue
        executable = os.path.basename(segment[0]).lower()
        args = segment[1:]
        if executable in {"source", "."} and args:
            paths.append(args[0])
            continue
        if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", executable):
            if any(arg in {"-c", "-m"} for arg in args):
                continue
            script = _direct_interpreter_script_arg(args)
            if script:
                paths.append(script)
            continue
        if executable in interpreters:
            if any(arg in {"-c", "-Command", "-EncodedCommand"} for arg in args):
                continue
            script = _direct_interpreter_script_arg(args)
            if script:
                paths.append(script)
            continue
        if _looks_like_script_path(segment[0]):
            paths.append(segment[0])
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
        first = argv[0]
        first_base = os.path.basename(first or "").lower()
        if first == "<python-executable>" or re.fullmatch(
            r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", first_base
        ):
            if len(argv) > 1 and argv[1] and not argv[1].startswith("-"):
                paths.append(argv[1])
        elif first_base in {
            "bash",
            "sh",
            "zsh",
            "node",
            "ruby",
            "perl",
            "pwsh",
        }:
            if len(argv) > 1 and argv[1] and not argv[1].startswith("-"):
                paths.append(argv[1])
        elif first and _looks_like_script_path(first):
            paths.append(first)
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
    for raw_path in raw_paths[:MAX_SCRIPT_COUNT]:
        expanded = os.path.expanduser(raw_path)
        resolved = os.path.abspath(
            expanded if os.path.isabs(expanded) else os.path.join(base, expanded)
        )
        if resolved in seen:
            continue
        seen.add(resolved)
        if _is_virtualenv_console_entrypoint(resolved):
            continue
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
    if len(raw_paths) > MAX_SCRIPT_COUNT:
        evidence.append(
            {
                "path": "<additional-direct-scripts>",
                "status": "unreadable",
                "content": "",
            }
        )
    return evidence
