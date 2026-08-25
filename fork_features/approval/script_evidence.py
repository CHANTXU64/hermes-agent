"""Collect bounded evidence from directly launched Python scripts.

This Fork-owned module also recognizes explicitly named Python files launched
by a literal ``hermes_tools.terminal`` command.  It does not follow ordinary
imports or recursively walk dependency trees.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
import sysconfig
import tempfile
from collections.abc import Callable
from typing import Optional

MAX_SCRIPT_BYTES = 32_000
MAX_SCRIPT_COUNT = 4

_TRUSTED_API_TARGETS = {
    "hermes_tools.terminal",
    "importlib.util.spec_from_file_location",
    "runpy.run_path",
    "importlib.machinery.SourceFileLoader",
}
_TRUSTED_API_MODULES = {
    "hermes_tools",
    "importlib",
    "importlib.util",
    "importlib.machinery",
    "runpy",
}
_GIT_ROOT_CACHE: dict[str, Optional[str]] = {}
_HERMES_SOURCE_ROOT = os.path.realpath(
    os.path.join(os.path.dirname(__file__), "../..")
)


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
_INTERPRETER_OPTIONS_WITH_VALUE = {
    "bash": {"-O", "-o", "--rcfile", "--init-file"},
    "sh": {"-o"},
    "zsh": {"-o"},
    "dash": {"-o"},
    "fish": {"--init-command"},
    "node": {
        "--require",
        "-r",
        "--loader",
        "--experimental-loader",
        "--import",
        "--conditions",
        "--input-type",
    },
    "ruby": {"-I", "-r", "--require", "-C", "--directory"},
    "perl": {"-I", "-M", "-m", "-d"},
    "pwsh": {
        "-ExecutionPolicy",
        "-ConfigurationName",
        "-WorkingDirectory",
        "-InputFormat",
        "-OutputFormat",
    },
    "powershell": {
        "-ExecutionPolicy",
        "-ConfigurationName",
        "-WorkingDirectory",
        "-InputFormat",
        "-OutputFormat",
    },
    "powershell.exe": {
        "-ExecutionPolicy",
        "-ConfigurationName",
        "-WorkingDirectory",
        "-InputFormat",
        "-OutputFormat",
    },
}
_INTERPRETER_OPTIONS_WITHOUT_VALUE = {
    "bash": {"-l", "--login", "-i", "--interactive", "-n", "--noexec", "-r", "--restricted", "-s", "--noprofile", "--norc", "--posix"},
    "sh": {"-i", "-l", "-n", "-s"},
    "zsh": {"-i", "-l", "-n", "-s"},
    "dash": {"-i", "-l", "-n", "-s"},
    "fish": {"--no-config", "--private"},
    "node": {"--inspect", "--inspect-brk", "--watch", "--trace-warnings", "--no-warnings"},
    "ruby": {"-c", "-v", "-w", "-W"},
    "perl": {"-w", "-W", "-T", "-t"},
    "pwsh": {"-NoLogo", "-NoProfile", "-NonInteractive", "-NoExit", "-File"},
    "powershell": {"-NoLogo", "-NoProfile", "-NonInteractive", "-NoExit", "-File"},
    "powershell.exe": {"-NoLogo", "-NoProfile", "-NonInteractive", "-NoExit", "-File"},
}
_INTERPRETER_INLINE_OPTIONS = {
    "bash": {"-c"},
    "sh": {"-c"},
    "zsh": {"-c"},
    "dash": {"-c"},
    "fish": {"-c"},
    "node": {"-e", "--eval", "-p", "--print"},
    "ruby": {"-e"},
    "perl": {"-e"},
    "pwsh": {"-Command", "-EncodedCommand"},
    "powershell": {"-Command", "-EncodedCommand"},
    "powershell.exe": {"-Command", "-EncodedCommand"},
}
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
    options_without_value: Optional[set[str]] = None,
    reject_unknown_options: bool = False,
) -> Optional[str]:
    """Return a directly executed script path, excluding inline/stdin code.

    Shell lexing keeps heredoc operators such as ``<<PY`` in the argv-like
    token stream. They are redirections, not script filenames. A real script
    before a redirection (``python reader.py <<EOF``) still wins, while ``-``
    and heredoc/here-string input mean the code is already visible inline in
    the command block.
    """
    options_with_value = options_with_value or set()
    options_without_value = options_without_value or set()
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

        if not options_ended and arg.startswith("-"):
            option_name = arg.split("=", 1)[0]
            if arg in options_with_value or option_name in options_with_value:
                index += 2 if "=" not in arg else 1
                continue
            if arg in options_without_value:
                index += 1
                continue
            if reject_unknown_options:
                return None
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
        inline_options = _INTERPRETER_INLINE_OPTIONS.get(executable, set())
        if any(
            arg in inline_options or arg.split("=", 1)[0] in inline_options
            for arg in args
        ):
            return None
        return _direct_interpreter_script_arg(
            args,
            options_with_value=_INTERPRETER_OPTIONS_WITH_VALUE.get(executable),
            options_without_value=_INTERPRETER_OPTIONS_WITHOUT_VALUE.get(executable),
            reject_unknown_options=True,
        )
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


def _static_path_value(
    node: ast.AST,
    bindings: dict[str, str],
    *,
    source_path: Optional[str],
) -> Optional[str]:
    """Evaluate the small path-expression subset needed for evidence lookup."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        if node.id == "__file__":
            return source_path
        return bindings.get(node.id)
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
                continue
            if isinstance(value, ast.FormattedValue):
                evaluated = _static_path_value(
                    value.value,
                    bindings,
                    source_path=source_path,
                )
                if evaluated is None:
                    return None
                parts.append(evaluated)
                continue
            return None
        return "".join(parts)
    if isinstance(node, ast.BinOp):
        left = _static_path_value(node.left, bindings, source_path=source_path)
        right = _static_path_value(node.right, bindings, source_path=source_path)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Div):
            return os.path.join(left, right)
        if isinstance(node.op, ast.Add):
            return left + right
        return None
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        value = _static_path_value(node.value, bindings, source_path=source_path)
        return os.path.dirname(value) if value else None
    if isinstance(node, ast.Call):
        name = _call_name(node.func)
        if name in {"str", "os.fspath"} and node.args:
            return _static_path_value(node.args[0], bindings, source_path=source_path)
        if name in {"Path", "pathlib.Path"} and node.args:
            return _static_path_value(node.args[0], bindings, source_path=source_path)
        if name in {"os.path.join", "posixpath.join", "ntpath.join"}:
            parts = [
                _static_path_value(arg, bindings, source_path=source_path)
                for arg in node.args
            ]
            if not parts or any(part is None for part in parts):
                return None
            resolved_parts: list[str] = [part for part in parts if part is not None]
            return os.path.join(*resolved_parts)
    return None


def _resolve_api_target(
    func: ast.AST,
    aliases: dict[str, Optional[str]],
) -> Optional[str]:
    """Resolve only API names whose import provenance is statically known."""
    name = _call_name(func)
    if isinstance(func, ast.Name):
        return aliases.get(func.id)
    if not isinstance(func, ast.Attribute) or not name:
        return None
    root, _, suffix = name.partition(".")
    module = aliases.get(root)
    if module is None or not suffix:
        return None
    return f"{module}.{suffix}"


def _record_import(node: ast.Import, aliases: dict[str, Optional[str]]) -> None:
    for item in node.names:
        bound_name = item.asname or item.name.split(".", 1)[0]
        module_name = item.name if item.asname else item.name.split(".", 1)[0]
        aliases[bound_name] = (
            module_name if item.name in _TRUSTED_API_MODULES else None
        )


def _record_import_from(
    node: ast.ImportFrom,
    aliases: dict[str, Optional[str]],
) -> None:
    module = node.module or ""
    for item in node.names:
        if item.name == "*":
            continue
        bound_name = item.asname or item.name
        target = f"{module}.{item.name}" if module else None
        aliases[bound_name] = (
            target
            if module in _TRUSTED_API_MODULES
            and (target in _TRUSTED_API_TARGETS or target in _TRUSTED_API_MODULES)
            else None
        )


def _invalidate_target(
    target: ast.AST,
    bindings: dict[str, str],
    aliases: dict[str, Optional[str]],
) -> None:
    if isinstance(target, ast.Name):
        bindings.pop(target.id, None)
        aliases[target.id] = None
        return
    if isinstance(target, (ast.Tuple, ast.List)):
        for item in target.elts:
            _invalidate_target(item, bindings, aliases)
        return
    if isinstance(target, ast.Attribute):
        root = target.value
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name):
            bindings.pop(root.id, None)
            aliases[root.id] = None
        return
    if isinstance(target, ast.Subscript):
        root = target.value
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name):
            bindings.pop(root.id, None)
            aliases[root.id] = None


def _assign_target(
    target: ast.AST,
    value: Optional[str],
    bindings: dict[str, str],
    aliases: dict[str, Optional[str]],
) -> None:
    if isinstance(target, ast.Name):
        if value is None:
            bindings.pop(target.id, None)
        else:
            bindings[target.id] = value
        aliases[target.id] = None
        return
    _invalidate_target(target, bindings, aliases)


def _bound_names(nodes: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for statement in nodes:
        for node in ast.walk(statement):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
    return names


def _call_ends_with(name: str, suffix: str) -> bool:
    return name == suffix or name.endswith(f".{suffix}")


def _call_argument(
    node: ast.Call,
    index: int,
    keyword: str,
) -> Optional[ast.AST]:
    if len(node.args) > index:
        return node.args[index]
    for item in node.keywords:
        if item.arg == keyword:
            return item.value
    return None


def _path_is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except (OSError, ValueError):
        return False


def _git_root_for(path: str) -> Optional[str]:
    """Return the containing Git worktree marker without executing a command."""
    absolute = os.path.abspath(path)
    directory = absolute if os.path.isdir(absolute) else os.path.dirname(absolute)
    while directory and not os.path.isdir(directory):
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent
    directory = os.path.realpath(directory or os.getcwd())
    if directory in _GIT_ROOT_CACHE:
        return _GIT_ROOT_CACHE[directory]
    current = directory
    while current:
        marker = os.path.join(current, ".git")
        if os.path.isdir(marker) or os.path.isfile(marker):
            _GIT_ROOT_CACHE[directory] = current
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    _GIT_ROOT_CACHE[directory] = None
    return None


def _path_components(path: str) -> set[str]:
    return {component.lower() for component in path.split(os.sep) if component}


def _is_protected_source_path(path: str) -> bool:
    """Reject standard-library, installed-package, and Hermes source paths."""
    candidates = {os.path.abspath(path), os.path.realpath(path)}
    stdlib_roots = {
        os.path.realpath(value)
        for value in (
            sysconfig.get_path("stdlib"),
            sysconfig.get_path("platstdlib"),
        )
        if value
    }
    for candidate in candidates:
        if _path_is_within(candidate, _HERMES_SOURCE_ROOT):
            return True
        components = _path_components(candidate)
        if {"site-packages", "dist-packages"} & components:
            return True
        if any(_path_is_within(candidate, root) for root in stdlib_roots):
            return True
        parts = candidate.split(os.sep)
        if any(
            re.fullmatch(r"python\d+(?:\.\d+)*", part, re.IGNORECASE)
            and index > 0
            and parts[index - 1].lower() in {"lib", "libs"}
            for index, part in enumerate(parts)
        ):
            return True
    return False


_TASK_TEMP_ROOT_PATTERN = re.compile(
    r"(?:hindsight|hermes-task)-[a-z0-9][a-z0-9-]*$", re.IGNORECASE
)


def _is_task_temp_path(path: str) -> bool:
    temp_roots = {
        os.path.realpath(tempfile.gettempdir()),
        os.path.realpath("/tmp"),
        os.path.realpath("/private/tmp"),
    }
    candidate = os.path.realpath(path)
    for root in temp_roots:
        if not _path_is_within(candidate, root):
            continue
        relative_parts = os.path.relpath(candidate, root).split(os.sep)
        if not relative_parts or relative_parts[0] in {".", ".."}:
            return False
        if not any(
            _TASK_TEMP_ROOT_PATTERN.fullmatch(part)
            for part in relative_parts[:-1]
        ):
            return False
        if relative_parts[0].lower() in {
            "bin",
            "lib",
            "lib64",
            "sbin",
            "site-packages",
            "dist-packages",
        }:
            return False
        return True
    return False


def _is_allowed_local_script_path(path: str, *, cwd: str) -> bool:
    """Apply the local evidence boundary before any content reader is called."""
    lexical = os.path.abspath(path)
    resolved = os.path.realpath(path)
    if _is_protected_source_path(lexical) or _is_protected_source_path(resolved):
        return False

    roots = {os.path.realpath(os.path.abspath(cwd))}
    base_git_root = _git_root_for(cwd)
    if base_git_root:
        roots.add(base_git_root)
    target_git_root = _git_root_for(resolved)
    if target_git_root and not any(
        _path_is_within(resolved, root) for root in roots
    ):
        return False
    if any(_path_is_within(resolved, root) for root in roots):
        return True
    return _is_task_temp_path(resolved)


def _read_bounded_source(
    path: str,
    *,
    read_script: Optional[Callable[[str], Optional[str]]],
) -> Optional[str]:
    try:
        if read_script is not None:
            candidate = read_script(path)
            if (
                isinstance(candidate, str)
                and len(candidate.encode("utf-8", errors="replace")) <= MAX_SCRIPT_BYTES
                and "\x00" not in candidate
            ):
                return candidate
            return None
        real_path = os.path.realpath(path)
        if not os.path.isfile(real_path):
            return None
        with open(real_path, "rb") as handle:
            data = handle.read(MAX_SCRIPT_BYTES + 1)
        if len(data) > MAX_SCRIPT_BYTES or b"\x00" in data:
            return None
        return data.decode("utf-8", errors="replace")
    except Exception:
        return None


def _literal_argv(node: Optional[ast.AST]) -> list[Optional[str]] | str | None:
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


class _PythonEvidenceScanner:
    """Conservative source-order scanner for explicit script launch calls."""

    _SUBPROCESS_CALLS = {
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "os.system",
        "os.popen",
    }

    def __init__(self) -> None:
        self.paths: list[str] = []

    def scan(self, tree: ast.Module) -> list[str]:
        self._scan_block(tree.body, {}, {})
        return self.paths

    @staticmethod
    def _merge_bindings(
        left: dict[str, str], right: dict[str, str]
    ) -> dict[str, str]:
        return {
            key: left[key]
            for key in left.keys() & right.keys()
            if left[key] == right[key]
        }

    @staticmethod
    def _merge_aliases(
        left: dict[str, Optional[str]], right: dict[str, Optional[str]]
    ) -> dict[str, Optional[str]]:
        return {
            key: left[key]
            for key in left.keys() & right.keys()
            if left[key] == right[key]
        }

    def _record_call(
        self,
        node: ast.Call,
        bindings: dict[str, str],
        aliases: dict[str, Optional[str]],
    ) -> None:
        target = _resolve_api_target(node.func, aliases)
        if target == "hermes_tools.terminal":
            command = _literal_argv(_call_argument(node, 0, "command"))
            if isinstance(command, str):
                self.paths.extend(_direct_shell_script_paths(command))
            return

        if target == "importlib.util.spec_from_file_location":
            value_node = _call_argument(node, 1, "location")
        elif target == "runpy.run_path":
            value_node = _call_argument(node, 0, "path_name")
        elif target == "importlib.machinery.SourceFileLoader":
            value_node = _call_argument(node, 1, "path")
        else:
            value_node = None
        if target in _TRUSTED_API_TARGETS and value_node is not None:
            value = _static_path_value(value_node, bindings, source_path=None)
            if value:
                self.paths.append(value)
            return

        if _call_name(node.func) not in self._SUBPROCESS_CALLS or not node.args:
            return
        argv = _literal_argv(node.args[0])
        if isinstance(argv, str):
            self.paths.extend(_direct_shell_script_paths(argv))
            return
        if not isinstance(argv, list) or not argv or any(value is None for value in argv):
            return
        literal_argv = [
            "python" if value == "<python-executable>" else str(value)
            for value in argv
        ]
        script = _direct_argv_script_path(literal_argv)
        if script:
            self.paths.append(script)

    def _scan_expr(
        self,
        node: ast.AST,
        bindings: dict[str, str],
        aliases: dict[str, Optional[str]],
    ) -> None:
        if isinstance(node, ast.Call):
            self._record_call(node, bindings, aliases)
            self._scan_expr(node.func, bindings, aliases)
            for argument in node.args:
                self._scan_expr(argument, bindings, aliases)
            for keyword in node.keywords:
                self._scan_expr(keyword.value, bindings, aliases)
            return
        if isinstance(node, ast.Lambda):
            local_bindings = dict(bindings)
            local_aliases = dict(aliases)
            for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                _invalidate_target(argument, local_bindings, local_aliases)
            if node.args.vararg:
                _invalidate_target(node.args.vararg, local_bindings, local_aliases)
            if node.args.kwarg:
                _invalidate_target(node.args.kwarg, local_bindings, local_aliases)
            self._scan_expr(node.body, local_bindings, local_aliases)
            return
        if isinstance(node, ast.NamedExpr):
            self._scan_expr(node.value, bindings, aliases)
            value = _static_path_value(node.value, bindings, source_path=None)
            _assign_target(node.target, value, bindings, aliases)
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                self._scan_statement(child, bindings, aliases)
            else:
                self._scan_expr(child, bindings, aliases)

    def _scan_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        bindings: dict[str, str],
        aliases: dict[str, Optional[str]],
    ) -> None:
        for decorator in node.decorator_list:
            self._scan_expr(decorator, bindings, aliases)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self._scan_expr(default, bindings, aliases)
        local_bindings = dict(bindings)
        local_aliases = dict(aliases)
        arguments = (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        )
        for argument in arguments:
            _invalidate_target(argument, local_bindings, local_aliases)
        if node.args.vararg:
            _invalidate_target(node.args.vararg, local_bindings, local_aliases)
        if node.args.kwarg:
            _invalidate_target(node.args.kwarg, local_bindings, local_aliases)
        self._scan_block(node.body, local_bindings, local_aliases)

    def _scan_branch(
        self,
        statements: list[ast.stmt],
        bindings: dict[str, str],
        aliases: dict[str, Optional[str]],
    ) -> tuple[dict[str, str], dict[str, Optional[str]]]:
        branch_bindings = dict(bindings)
        branch_aliases = dict(aliases)
        self._scan_block(statements, branch_bindings, branch_aliases)
        return branch_bindings, branch_aliases

    def _scan_statement(
        self,
        node: ast.stmt,
        bindings: dict[str, str],
        aliases: dict[str, Optional[str]],
    ) -> None:
        if isinstance(node, ast.Import):
            _record_import(node, aliases)
            return
        if isinstance(node, ast.ImportFrom):
            _record_import_from(node, aliases)
            return
        if isinstance(node, ast.Assign):
            self._scan_expr(node.value, bindings, aliases)
            value = _static_path_value(node.value, bindings, source_path=None)
            for target in node.targets:
                _assign_target(target, value, bindings, aliases)
            return
        if isinstance(node, ast.AnnAssign):
            if node.value is not None:
                self._scan_expr(node.value, bindings, aliases)
            value = (
                _static_path_value(node.value, bindings, source_path=None)
                if node.value is not None
                else None
            )
            _assign_target(node.target, value, bindings, aliases)
            return
        if isinstance(node, ast.AugAssign):
            self._scan_expr(node.value, bindings, aliases)
            _invalidate_target(node.target, bindings, aliases)
            return
        if isinstance(node, ast.Delete):
            for target in node.targets:
                _invalidate_target(target, bindings, aliases)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._scan_function(node, bindings, aliases)
            _invalidate_target(ast.Name(id=node.name), bindings, aliases)
            return
        if isinstance(node, ast.ClassDef):
            for decorator in node.decorator_list:
                self._scan_expr(decorator, bindings, aliases)
            for base in node.bases:
                self._scan_expr(base, bindings, aliases)
            local_bindings = dict(bindings)
            local_aliases = dict(aliases)
            self._scan_block(node.body, local_bindings, local_aliases)
            _invalidate_target(ast.Name(id=node.name), bindings, aliases)
            return
        if isinstance(node, ast.If):
            self._scan_expr(node.test, bindings, aliases)
            body_bindings, body_aliases = self._scan_branch(
                node.body, bindings, aliases
            )
            else_bindings, else_aliases = self._scan_branch(
                node.orelse, bindings, aliases
            )
            merged_bindings = self._merge_bindings(body_bindings, else_bindings)
            merged_aliases = self._merge_aliases(body_aliases, else_aliases)
            bindings.clear()
            bindings.update(
                {key: value for key, value in merged_bindings.items() if value is not None}
            )
            aliases.clear()
            aliases.update(merged_aliases)
            return
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            if isinstance(node, (ast.For, ast.AsyncFor)):
                self._scan_expr(node.iter, bindings, aliases)
                loop_target = node.target
            else:
                self._scan_expr(node.test, bindings, aliases)
                loop_target = None
            loop_bindings = dict(bindings)
            loop_aliases = dict(aliases)
            if loop_target is not None:
                _invalidate_target(loop_target, loop_bindings, loop_aliases)
            self._scan_block(node.body, loop_bindings, loop_aliases)
            self._scan_block(node.orelse, dict(bindings), dict(aliases))
            for name in _bound_names([*node.body, *node.orelse]):
                bindings.pop(name, None)
                aliases[name] = None
            if loop_target is not None:
                _invalidate_target(loop_target, bindings, aliases)
            return
        if isinstance(node, ast.With | ast.AsyncWith):
            for item in node.items:
                self._scan_expr(item.context_expr, bindings, aliases)
            body_bindings = dict(bindings)
            body_aliases = dict(aliases)
            for item in node.items:
                if item.optional_vars is not None:
                    _invalidate_target(item.optional_vars, body_bindings, body_aliases)
            self._scan_block(node.body, body_bindings, body_aliases)
            for name in _bound_names(node.body):
                bindings.pop(name, None)
                aliases[name] = None
            return
        if isinstance(node, ast.Try):
            states: list[tuple[dict[str, str], dict[str, Optional[str]]]] = []
            blocks = [node.body, node.orelse, node.finalbody]
            blocks.extend(handler.body for handler in node.handlers)
            for block in blocks:
                branch_bindings = dict(bindings)
                branch_aliases = dict(aliases)
                self._scan_block(block, branch_bindings, branch_aliases)
                states.append((branch_bindings, branch_aliases))
            if states:
                merged_bindings = states[0][0]
                merged_aliases = states[0][1]
                for branch_bindings, branch_aliases in states[1:]:
                    merged_bindings = self._merge_bindings(merged_bindings, branch_bindings)
                    merged_aliases = self._merge_aliases(merged_aliases, branch_aliases)
                bindings.clear()
                bindings.update(
                    {
                        key: value
                        for key, value in merged_bindings.items()
                        if value is not None
                    }
                )
                aliases.clear()
                aliases.update(merged_aliases)
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                self._scan_statement(child, bindings, aliases)
            else:
                self._scan_expr(child, bindings, aliases)

    def _scan_block(
        self,
        statements: list[ast.stmt],
        bindings: dict[str, str],
        aliases: dict[str, Optional[str]],
    ) -> None:
        for statement in statements:
            self._scan_statement(statement, bindings, aliases)


def _direct_python_script_paths(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return _PythonEvidenceScanner().scan(tree)


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
    """Read bounded scripts directly executed or explicitly launched by path."""
    raw_paths = (
        _direct_python_script_paths(source)
        if source_kind == "python"
        else _direct_shell_script_paths(source)
    )
    evidence: list[dict[str, str]] = []
    seen: set[str] = set()
    base = os.path.abspath(os.path.expanduser(cwd or os.getcwd()))
    has_additional_scripts = False

    def add_direct_script(raw_path: str) -> None:
        nonlocal has_additional_scripts
        expanded = os.path.expanduser(raw_path)
        resolved = os.path.abspath(
            expanded if os.path.isabs(expanded) else os.path.join(base, expanded)
        )
        if resolved in seen or _is_virtualenv_console_entrypoint(resolved):
            return
        if len(seen) >= MAX_SCRIPT_COUNT:
            has_additional_scripts = True
            return
        seen.add(resolved)
        content = (
            _read_bounded_source(resolved, read_script=read_script)
            if _is_allowed_local_script_path(resolved, cwd=base)
            else None
        )
        evidence.append(
            {
                "path": resolved,
                "status": "read" if content is not None else "unreadable",
                "content": content or "",
            }
        )

    for raw_path in raw_paths:
        add_direct_script(raw_path)
        if len(seen) >= MAX_SCRIPT_COUNT and has_additional_scripts:
            break

    if has_additional_scripts:
        evidence.append(
            {
                "path": "<additional-direct-scripts>",
                "status": "unreadable",
                "content": "",
            }
        )
    return evidence
