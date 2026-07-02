"""Safe command rewrite — replace destructive file operations with safe alternatives.

Uses bashlex AST parser to correctly identify rm/mv/cp commands in shell
syntax, avoiding false positives from strings, subcommands, etc.

Rewrites:
  local: rm [flags] files  →  trash files
  local: mv/cp [flags] src dst  →  gmv/gcp -b [flags] src dst
  ssh: same as local — rm → trash; mv/cp → gmv/gcp -b

Rules:
- trash does not support -r/-f; these flags are stripped.
- gmv/gcp get -b (backup) added automatically; if -b already present it is not duplicated.
- Handles sudo rm, /bin/rm, /usr/bin/rm, etc.
- Wrapper commands (nice, env, timeout, xargs, etc.) are preserved.
- Compound commands (&& || ; |) are rewritten per-command via AST.
- Shell structures (for/while/if/case) are handled natively by the AST.
- Command substitution $(...) and subshells (...) are parsed and rewritten.
- Strings containing 'rm' are correctly ignored (AST distinguishes string args).
- VCS subcommands (git rm, hg rm, etc.) are NOT rewritten.
- Sandbox environments (docker/modal/singularity/daytona) skip rewriting entirely.
- SSH backends and explicit ssh remote commands rewrite remote file operations.

Known limitations:
- Malformed shell commands (e.g. double ;; outside case) may not be parsed
  by bashlex and will be returned unchanged (safe fallback).
- Heredocs (<<EOF ... EOF) are not specially handled.
- When bashlex is not installed, falls back to regex-based rewriting with
  the original limitations (no $(...)/subshell rewriting).
"""

from __future__ import annotations

import re
from typing import Any, List, Tuple

bashlex: Any
try:
    import bashlex
    import bashlex.ast
    _HAS_BASHLEX = True
except ImportError:
    bashlex = None  # type: ignore[assignment]
    _HAS_BASHLEX = False

# ---------------------------------------------------------------------------
# Sandbox detection
# ---------------------------------------------------------------------------

_SANDBOX_ENVS: frozenset[str] = frozenset({
    "docker", "modal", "singularity", "daytona",
})


class _RewriteCommands:
    """Concrete command names for one execution context."""

    def __init__(self, rm: str, mv: str, cp: str) -> None:
        self.rm = rm
        self.mv = mv
        self.cp = cp


_LOCAL_COMMANDS = _RewriteCommands(rm="trash", mv="gmv", cp="gcp")
_SSH_COMMANDS = _LOCAL_COMMANDS


_SHELL_NAMES: frozenset[str] = frozenset({"sh", "bash", "zsh"})
_SSH_OPTIONS_TAKING_ARG: frozenset[str] = frozenset({
    "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L",
    "-l", "-m", "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w",
})
_SUDO_OPTIONS_TAKING_ARG: frozenset[str] = frozenset({
    "-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt",
    "-C", "--close-from", "-r", "--role", "-t", "--type",
})
_ENV_OPTIONS_TAKING_ARG: frozenset[str] = frozenset({
    "-u", "--unset", "-S", "--split-string",
})
_NICE_OPTIONS_TAKING_ARG: frozenset[str] = frozenset({"-n", "--adjustment"})
_TIMEOUT_OPTIONS_TAKING_ARG: frozenset[str] = frozenset({
    "-s", "--signal", "-k", "--kill-after",
})
_STDBUF_OPTIONS_TAKING_ARG: frozenset[str] = frozenset({
    "-i", "--input", "-o", "--output", "-e", "--error",
})
_IONICE_OPTIONS_TAKING_ARG: frozenset[str] = frozenset({
    "-c", "--class", "-n", "--classdata", "-p", "--pid",
})
_CHROOT_OPTIONS_TAKING_ARG: frozenset[str] = frozenset({
    "--userspec", "--groups",
})
_XARGS_OPTIONS_TAKING_ARG: frozenset[str] = frozenset({
    "-E", "--eof", "-I", "--replace", "-L", "--max-lines", "-l",
    "-n", "--max-args", "-P", "--max-procs", "-s", "--max-chars",
    "-d", "--delimiter",
})
_DOCKER_EXEC_OPTIONS_TAKING_ARG: frozenset[str] = frozenset({
    "-e", "--env", "--env-file", "-u", "--user", "-w", "--workdir",
    "--detach-keys",
})


def _is_sandbox(env_type: str) -> bool:
    """Return True if the environment is a sandbox where rewriting should be skipped."""
    return env_type.lower() in _SANDBOX_ENVS


def _commands_for_env(env_type: str) -> _RewriteCommands:
    """Return command replacements suitable for the execution environment."""
    if env_type.lower() == "ssh":
        # SSH uses the same safety contract as local terminal execution.
        return _SSH_COMMANDS
    return _LOCAL_COMMANDS


# ---------------------------------------------------------------------------
# AST-based rewriting
# ---------------------------------------------------------------------------

_VCS_TOOLS: frozenset[str] = frozenset({'git', 'hg', 'svn', 'bzr'})
_DESTRUCTIVE_CMDS: frozenset[str] = frozenset({'rm', 'mv', 'cp'})
_WRAPPER_NAMES: frozenset[str] = frozenset({
    'sudo', 'nice', 'env', 'timeout', 'xargs', 'stdbuf',
    'ionice', 'chroot', 'nohup', 'exec',
})
_WRAPPERS_TAKING_EXTRA_ARG: frozenset[str] = frozenset({'timeout', 'chroot'})


def _base(word: str) -> str:
    return word.split('/')[-1]


def _skip_options(words: List[str], index: int, options_taking_arg: frozenset[str]) -> int:
    """Skip simple command options and their value words where known."""
    i = index
    while i < len(words):
        word = words[i]
        if word == '--':
            return i + 1
        if not word.startswith('-') or word == '-':
            return i
        option = word.split('=', 1)[0]
        i += 1
        if option in options_taking_arg and '=' not in word and i < len(words):
            i += 1
    return i


def _leading_command_index(words: List[str]) -> int:
    """Skip common leading wrappers and return the effective command index."""
    i = 0
    while i < len(words):
        word = words[i]
        base = _base(word)

        if base == 'sudo':
            i += 1
            i = _skip_options(words, i, _SUDO_OPTIONS_TAKING_ARG)
            continue

        if base == 'env':
            i += 1
            i = _skip_options(words, i, _ENV_OPTIONS_TAKING_ARG)
            while i < len(words) and '=' in words[i] and not words[i].startswith('-'):
                i += 1
            continue

        if base == 'nice':
            i += 1
            i = _skip_options(words, i, _NICE_OPTIONS_TAKING_ARG)
            continue

        if base == 'timeout':
            i += 1
            i = _skip_options(words, i, _TIMEOUT_OPTIONS_TAKING_ARG)
            if i < len(words) and not words[i].startswith('-'):
                # timeout DURATION COMMAND
                i += 1
            continue

        if base == 'stdbuf':
            i += 1
            i = _skip_options(words, i, _STDBUF_OPTIONS_TAKING_ARG)
            continue

        if base == 'ionice':
            i += 1
            i = _skip_options(words, i, _IONICE_OPTIONS_TAKING_ARG)
            continue

        if base == 'chroot':
            i += 1
            i = _skip_options(words, i, _CHROOT_OPTIONS_TAKING_ARG)
            if i < len(words):
                # chroot NEWROOT COMMAND
                i += 1
            continue

        if base == 'xargs':
            i += 1
            i = _skip_options(words, i, _XARGS_OPTIONS_TAKING_ARG)
            continue

        if base in {'nohup', 'exec'}:
            i += 1
            continue

        if '=' in word and not word.startswith('-'):
            i += 1
            continue
        break
    return i


def _single_quote_shell_word(word: str) -> str:
    """Return a shell-safe single word using single-quote escaping."""
    return "'" + word.replace("'", "'\\''") + "'"


def _double_quote_shell_word(word: str) -> str:
    """Return a shell-safe double-quoted word while preserving double-quote semantics."""
    escaped = word.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _preserve_outer_quotes(raw: str, rewritten: str) -> str:
    """Replace a shell word while keeping the result one safely quoted word."""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        quote = raw[0]
        if quote == "'":
            return _single_quote_shell_word(rewritten)
        return _double_quote_shell_word(rewritten)
    return _single_quote_shell_word(rewritten)


def _shell_c_script_index(words: List[str], start_idx: int) -> int | None:
    """Return the script word index for sh/bash/zsh -c/-lc forms."""
    if start_idx >= len(words) or _base(words[start_idx]) not in _SHELL_NAMES:
        return None
    i = start_idx + 1
    while i < len(words):
        word = words[i]
        if word == '--':
            i += 1
            continue
        if word.startswith('-') and 'c' in word[1:]:
            return i + 1 if i + 1 < len(words) else None
        i += 1
    return None


def _ssh_remote_command_index(words: List[str], start_idx: int) -> int | None:
    """Return the first remote-command word in an ssh invocation."""
    i = start_idx + 1
    while i < len(words):
        word = words[i]
        if word == '--':
            i += 1
            break
        if not word.startswith('-'):
            break
        opt = word.split('=', 1)[0]
        if opt in _SSH_OPTIONS_TAKING_ARG and '=' not in word and len(word) == 2:
            i += 2
        elif opt in _SSH_OPTIONS_TAKING_ARG and '=' not in word and word in _SSH_OPTIONS_TAKING_ARG:
            i += 2
        else:
            i += 1

    # i is the destination host/user@host. Anything after it is remote command.
    return i + 1 if i + 1 < len(words) else None


def _docker_exec_command_index(words: List[str], start_idx: int) -> int | None:
    """Return COMMAND index for docker exec [OPTIONS] CONTAINER COMMAND."""
    if start_idx + 2 >= len(words):
        return None
    if _base(words[start_idx]) != 'docker' or words[start_idx + 1] != 'exec':
        return None

    i = start_idx + 2
    while i < len(words):
        word = words[i]
        if word == '--':
            i += 1
            break
        if not word.startswith('-'):
            break
        opt = word.split('=', 1)[0]
        if opt in _DOCKER_EXEC_OPTIONS_TAKING_ARG and '=' not in word:
            i += 2
        else:
            i += 1

    # i is CONTAINER. Anything after it is COMMAND.
    return i + 1 if i + 1 < len(words) else None


class _FallbackNodeVisitorBase:
    pass


_NodeVisitorBase: Any = bashlex.ast.nodevisitor if _HAS_BASHLEX else _FallbackNodeVisitorBase


class _RewriteVisitor(_NodeVisitorBase):
    """Visitor that finds rm/mv/cp commands and records position-based replacements."""

    def __init__(
        self,
        original: str,
        commands: _RewriteCommands,
        rewrite_explicit_ssh: bool = True,
    ) -> None:
        self.original = original
        self.commands = commands
        self.rewrite_explicit_ssh = rewrite_explicit_ssh
        self.replacements: List[Tuple[int, int, str]] = []

    def visitcommand(self, node, parts):
        words = [p.word for p in parts if hasattr(p, 'word')]
        word_parts = [p for p in parts if hasattr(p, 'word')]
        if not words:
            return True

        leading_idx = _leading_command_index(words)
        leading_base = _base(words[leading_idx]) if leading_idx < len(words) else ''

        if self.rewrite_explicit_ssh and leading_base == 'ssh':
            if self._rewrite_explicit_ssh_command(words, word_parts, leading_idx):
                return True

        if leading_base == 'docker':
            self._rewrite_docker_exec_command(words, word_parts, leading_idx)
            # Docker object/subcommands such as `docker rm`, `docker rmi`, and
            # `docker cp` are not filesystem rm/mv/cp in the current shell.  Only
            # `docker exec ... <file-op>` is eligible for nested rewriting.
            return True

        if leading_base in _SHELL_NAMES:
            if self._rewrite_shell_c_script(words, word_parts, leading_idx, self.commands):
                return True

        # Skip VCS subcommands: git rm, hg rm, svn rm, bzr rm
        if words[0] in _VCS_TOOLS and len(words) >= 2 and words[1] == 'rm':
            return True

        # Skip already-safe commands
        if words[0] in ('trash', 'gmv', 'gcp'):
            return True

        # Only the effective command after recognized wrappers/assignments is
        # eligible.  Do not scan later arguments such as `sudo echo rm ...`.
        cmd_idx = leading_idx
        actual_cmd = words[cmd_idx] if cmd_idx < len(words) else None
        if not actual_cmd:
            return True

        actual_base = actual_cmd.split('/')[-1]
        if actual_base not in _DESTRUCTIVE_CMDS:
            return True

        self._record_destructive_rewrite(parts, cmd_idx, actual_base, self.commands)

        return True

    def _record_destructive_rewrite(
        self,
        parts,
        cmd_idx: int,
        actual_base: str,
        commands: _RewriteCommands,
    ) -> None:
        cmd_part = parts[cmd_idx]
        cmd_start, cmd_end = cmd_part.pos

        if actual_base == 'rm':
            self.replacements.append((cmd_start, cmd_end, commands.rm))
            prev_end = cmd_end
            for p in parts[cmd_idx + 1:]:
                if hasattr(p, 'word') and p.word == '--':
                    # End of options — everything after is positional, do not touch
                    break
                if hasattr(p, 'word') and p.word.startswith('-'):
                    safe = '-' + ''.join(c for c in p.word[1:] if c not in 'rfR')
                    flag_start = p.pos[0]
                    flag_end = p.pos[1]
                    if safe == '-':
                        # Remove flag + preceding whitespace
                        self.replacements.append((prev_end, flag_end, ''))
                    elif safe != p.word:
                        self.replacements.append((flag_start, flag_end, safe))
                    prev_end = flag_end
                else:
                    break
        elif actual_base == 'mv':
            self.replacements.append((cmd_start, cmd_end, commands.mv))
            has_b = any(
                hasattr(p, 'word') and p.word.startswith('-') and 'b' in p.word
                for p in parts[cmd_idx + 1:]
                if hasattr(p, 'word') and p.word.startswith('-') and p.word != '--'
            )
            if not has_b:
                self.replacements.append((cmd_end, cmd_end, ' -b'))

        elif actual_base == 'cp':
            self.replacements.append((cmd_start, cmd_end, commands.cp))
            has_b = any(
                hasattr(p, 'word') and p.word.startswith('-') and 'b' in p.word
                for p in parts[cmd_idx + 1:]
                if hasattr(p, 'word') and p.word.startswith('-') and p.word != '--'
            )
            if not has_b:
                self.replacements.append((cmd_end, cmd_end, ' -b'))

    def _rewrite_script_word(self, part, commands: _RewriteCommands) -> bool:
        rewritten = _rewrite_command(
            part.word,
            commands,
            rewrite_explicit_ssh=False,
        )
        if rewritten == part.word:
            return False

        start, end = part.pos
        raw = self.original[start:end]
        self.replacements.append((start, end, _preserve_outer_quotes(raw, rewritten)))
        return True

    def _rewrite_shell_c_script(
        self,
        words: List[str],
        parts,
        start_idx: int,
        commands: _RewriteCommands,
    ) -> bool:
        script_idx = _shell_c_script_index(words, start_idx)
        if script_idx is None:
            return False
        return self._rewrite_script_word(parts[script_idx], commands)

    def _rewrite_docker_exec_command(self, words: List[str], parts, start_idx: int) -> bool:
        cmd_idx = _docker_exec_command_index(words, start_idx)
        if cmd_idx is None:
            return False

        inner_offset = _leading_command_index(words[cmd_idx:])
        actual_idx = cmd_idx + inner_offset
        if actual_idx >= len(words):
            return False

        inner_base = _base(words[actual_idx])
        commands = _SSH_COMMANDS

        if inner_base in _SHELL_NAMES:
            return self._rewrite_shell_c_script(words, parts, actual_idx, commands)

        if inner_base in _DESTRUCTIVE_CMDS:
            self._record_destructive_rewrite(parts, actual_idx, inner_base, commands)
            return True
        return False

    def _rewrite_explicit_ssh_command(self, words: List[str], parts, start_idx: int) -> bool:
        remote_idx = _ssh_remote_command_index(words, start_idx)
        if remote_idx is None:
            return False

        if remote_idx == len(words) - 1 and any(ch.isspace() for ch in words[remote_idx]):
            return self._rewrite_script_word(parts[remote_idx], _SSH_COMMANDS)

        start = parts[remote_idx].pos[0]
        end = parts[-1].pos[1]
        remote_command = self.original[start:end]
        rewritten = _rewrite_command(
            remote_command,
            _SSH_COMMANDS,
            rewrite_explicit_ssh=False,
        )
        if rewritten == remote_command:
            # This is still an explicit ssh remote command.  If the remote side
            # needs no rewrite (for example `docker rm`), do not let the local
            # generic scanner rewrite tokens inside the remote command.
            return True
        self.replacements.append((start, end, rewritten))
        return True


def _rewrite_with_ast(
    command: str,
    commands: _RewriteCommands = _LOCAL_COMMANDS,
    rewrite_explicit_ssh: bool = True,
) -> str:
    """Rewrite using bashlex AST parser."""
    try:
        asts = bashlex.parse(command)
    except Exception:
        return command

    visitor = _RewriteVisitor(command, commands, rewrite_explicit_ssh=rewrite_explicit_ssh)
    for ast_node in asts:
        visitor.visit(ast_node)

    if not visitor.replacements:
        return command

    # Apply replacements in reverse order (highest position first)
    visitor.replacements.sort(key=lambda x: x[0], reverse=True)
    result = command
    for start, end, repl in visitor.replacements:
        result = result[:start] + repl + result[end:]

    # Clean up double spaces from removed flags
    while '  ' in result:
        result = result.replace('  ', ' ')

    # Clean up trailing whitespace
    result = result.rstrip()

    return result


def _rewrite_command(
    command: str,
    commands: _RewriteCommands,
    rewrite_explicit_ssh: bool = True,
) -> str:
    """Rewrite a command string with a caller-selected replacement policy."""
    if _HAS_BASHLEX:
        return _rewrite_with_ast(
            command,
            commands,
            rewrite_explicit_ssh=rewrite_explicit_ssh,
        )
    return _rewrite_fallback(command, commands, rewrite_explicit_ssh=rewrite_explicit_ssh)


# ---------------------------------------------------------------------------
# Fallback: regex-based rewriting (used when bashlex is not installed)
# ---------------------------------------------------------------------------

# Pattern to match rm invocations (with optional sudo / absolute path prefix)
_RM_RE = re.compile(
    r'^(sudo\s+)?((?:/[^\s/]+)*/?)(rm)\b(.*)',
    re.DOTALL,
)

# Pattern to match mv/gmv invocations
_MV_RE = re.compile(
    r'^(sudo\s+)?((?:/[^\s/]+)*/?)(g?mv)\b(.*)',
    re.DOTALL,
)

# Pattern to match cp/gcp invocations
_CP_RE = re.compile(
    r'^(sudo\s+)?((?:/[^\s/]+)*/?)(g?cp)\b(.*)',
    re.DOTALL,
)

_SHELL_BODY_INTROS = [
    ("do", None),
    ("then", None),
    ("else", None),
    ("elif", None),
]

_SEP_PATTERN = re.compile(r'\s*(&&|\|\||[;|])\s*')


def _scan_skip_string(command: str, pos: int) -> int:
    """Skip past a single- or double-quoted string starting at *pos*."""
    n = len(command)
    quote = command[pos]
    pos += 1
    while pos < n:
        if quote == "'" and command[pos] == "'":
            return pos + 1
        if quote == '"':
            if command[pos] == "\\" and pos + 1 < n:
                pos += 2
                continue
            if command[pos] == '"':
                return pos + 1
        pos += 1
    return pos


def _shell_words_with_spans(command: str) -> List[Tuple[str, int, int]]:
    """Split a simple shell command into unquoted words with original spans."""
    words: List[Tuple[str, int, int]] = []
    i = 0
    n = len(command)

    while i < n:
        while i < n and command[i].isspace():
            i += 1
        if i >= n:
            break

        start = i
        buf: List[str] = []
        while i < n and not command[i].isspace():
            ch = command[i]
            if ch in ("'", '"'):
                quote = ch
                i += 1
                while i < n:
                    if quote == "'" and command[i] == "'":
                        i += 1
                        break
                    if quote == '"':
                        if command[i] == "\\" and i + 1 < n:
                            buf.append(command[i + 1])
                            i += 2
                            continue
                        if command[i] == '"':
                            i += 1
                            break
                    buf.append(command[i])
                    i += 1
                continue
            if ch == "\\" and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            buf.append(ch)
            i += 1

        words.append(("".join(buf), start, i))

    return words


def _rewrite_explicit_ssh_fallback(
    segment: str,
    commands: _RewriteCommands,
) -> str | None:
    """Fallback explicit-ssh rewriting; None means segment is not ssh."""
    word_spans = _shell_words_with_spans(segment)
    words = [word for word, _, _ in word_spans]
    if not words:
        return None

    leading_idx = _leading_command_index(words)
    if leading_idx >= len(words) or _base(words[leading_idx]) != "ssh":
        return None

    remote_idx = _ssh_remote_command_index(words, leading_idx)
    if remote_idx is None:
        return segment

    remote_start = word_spans[remote_idx][1]
    remote_end = word_spans[-1][2]

    if remote_idx == len(words) - 1 and any(ch.isspace() for ch in words[remote_idx]):
        raw_remote = segment[remote_start:remote_end]
        rewritten = _rewrite_command(
            words[remote_idx],
            commands,
            rewrite_explicit_ssh=False,
        )
        if rewritten == words[remote_idx]:
            return segment
        replacement = _preserve_outer_quotes(raw_remote, rewritten)
    else:
        remote_command = segment[remote_start:remote_end]
        replacement = _rewrite_command(
            remote_command,
            commands,
            rewrite_explicit_ssh=False,
        )
        if replacement == remote_command:
            return segment

    return segment[:remote_start] + replacement + segment[remote_end:]


def _find_keyword(command: str, start: int, keyword: str) -> int:
    """Find a shell keyword at depth 0, scanning from *start*."""
    n = len(command)
    kw_len = len(keyword)
    i = start
    depth = 0

    while i <= n - kw_len:
        ch = command[i]

        if ch in ("'", '"'):
            i = _scan_skip_string(command, i)
            continue

        if ch == "\\" and i + 1 < n:
            i += 2
            continue

        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth -= 1
            i += 1
            continue

        if depth == 0 and command[i:i + kw_len] == keyword:
            before_ok = (i == 0
                         or command[i - 1].isspace()
                         or command[i - 1] in ";|&()")
            after_idx = i + kw_len
            after_ok = (after_idx >= n
                        or command[after_idx].isspace()
                        or command[after_idx] in ";|&()")
            if before_ok and after_ok:
                return i

        i += 1

    return -1


def _split_respecting_structures(command: str) -> List[Tuple[str, str]]:
    """Split a compound command, but do NOT split inside shell structures."""
    _STARTERS = {"for", "while", "until", "if", "case"}
    _STARTER_ENDER = {
        "for": "done", "while": "done", "until": "done",
        "if": "fi", "case": "esac",
    }

    segments: List[Tuple[str, str]] = []
    pos = 0
    n = len(command)

    while pos < n:
        while pos < n and command[pos].isspace():
            pos += 1
        if pos >= n:
            break

        seg_start = pos
        depth = 0
        in_struct = False
        ender: str | None = None

        scan = pos
        while scan < n:
            ch = command[scan]

            if ch in ("'", '"'):
                scan = _scan_skip_string(command, scan)
                continue

            if ch == "\\" and scan + 1 < n:
                scan += 2
                continue

            if ch == "(":
                depth += 1
                scan += 1
                continue
            if ch == ")":
                depth -= 1
                scan += 1
                continue

            if depth == 0:
                m = _SEP_PATTERN.match(command, scan)
                if m:
                    if not in_struct:
                        break

                if in_struct and ender:
                    ender_pos = _find_keyword(command, scan, ender)
                    if ender_pos == scan:
                        ender_len = len(ender)
                        in_struct = False
                        ender = None
                        scan = ender_pos + ender_len
                        continue

                if not in_struct:
                    found_starter = False
                    for starter_kw in _STARTERS:
                        kw_len = len(starter_kw)
                        if command[scan:scan + kw_len] == starter_kw:
                            before_ok = (scan == 0
                                         or command[scan - 1].isspace()
                                         or command[scan - 1] in ";|&()")
                            after_idx = scan + kw_len
                            after_ok = (after_idx >= n
                                        or command[after_idx].isspace()
                                        or command[after_idx] in ";|&()")
                            if before_ok and after_ok:
                                in_struct = True
                                ender = _STARTER_ENDER[starter_kw]
                                scan += kw_len
                                found_starter = True
                                break
                    if not found_starter:
                        scan += 1
                else:
                    scan += 1
            else:
                scan += 1

        seg_text = command[seg_start:scan].strip()

        sep = ""
        if scan < n:
            m = _SEP_PATTERN.match(command, scan)
            if m:
                sep = m.group(1)
                scan = m.end()

        if seg_text:
            segments.append((seg_text, sep))
        pos = scan

    return segments


def _join_segments(segments: List[Tuple[str, str]]) -> str:
    """Join (segment, separator) pairs back into a single command string."""
    parts: List[str] = []
    for seg, sep in segments:
        parts.append(seg)
        if sep:
            parts.append(f" {sep} ")
    return "".join(parts).strip()


def _rewrite_shell_body(
    body: str,
    commands: _RewriteCommands = _LOCAL_COMMANDS,
    rewrite_explicit_ssh: bool = True,
) -> str:
    """Rewrite destructive commands inside a shell body."""
    parts = []
    current = []
    depth = 0

    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch in ("'", '"'):
            end = _scan_skip_string(body, i)
            current.append(body[i:end])
            i = end
            continue
        if ch == "\\" and i + 1 < n:
            current.append(body[i:i+2])
            i += 2
            continue
        if ch == "(":
            depth += 1
            current.append(ch)
            i += 1
            continue
        if ch == ")":
            depth -= 1
            current.append(ch)
            i += 1
            continue
        if depth == 0 and ch in (";", "\n"):
            seg = "".join(current).strip()
            if seg:
                parts.append(_rewrite_single_segment_fallback(seg, commands, rewrite_explicit_ssh))
            parts.append(ch + " ")
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1

    seg = "".join(current).strip()
    if seg:
        parts.append(_rewrite_single_segment_fallback(seg, commands, rewrite_explicit_ssh))

    return "".join(parts).rstrip()


def _rewrite_segment_with_shell_keywords(
    segment: str,
    commands: _RewriteCommands = _LOCAL_COMMANDS,
    rewrite_explicit_ssh: bool = True,
) -> str:
    """Rewrite a segment that may contain shell structure keywords."""
    stripped = segment.strip()

    # "do ... done"
    do_pos = _find_keyword(stripped, 0, "do")
    if do_pos == 0 or (do_pos > 0 and stripped[:do_pos].rstrip().endswith(";")):
        before_do = stripped[:do_pos].rstrip().rstrip(";").strip()
        first_word = before_do.split()[0] if before_do.split() else ""
        if first_word in ("for", "while", "until"):
            done_pos = _find_keyword(stripped, do_pos + 2, "done")
            if done_pos >= 0:
                header = stripped[:do_pos + 2].rstrip()
                body = stripped[do_pos + 2:done_pos]
                after = stripped[done_pos + 4:].lstrip()
                rewritten_body = _rewrite_shell_body(body, commands, rewrite_explicit_ssh)
                result = f"{header} {rewritten_body.strip()} done"
                if after:
                    result += f" {after}"
                return result

    # "then ... fi" / "else ... fi"
    then_pos = _find_keyword(stripped, 0, "then")
    if then_pos == 0 or (then_pos > 0 and stripped[:then_pos].rstrip().endswith(";")):
        fi_pos = _find_keyword(stripped, then_pos + 4, "fi")
        if fi_pos >= 0:
            body = stripped[then_pos + 4:fi_pos]
            else_pos = _find_keyword(body, 0, "else")
            elif_pos = _find_keyword(body, 0, "elif")

            if elif_pos >= 0 and (else_pos < 0 or elif_pos < else_pos):
                then_body = body[:elif_pos]
                rest_body = body[elif_pos:] + " fi"
                rewritten_then = _rewrite_shell_body(then_body, commands, rewrite_explicit_ssh).strip()
                before_then = stripped[:then_pos + 4].rstrip()
                after_fi = stripped[fi_pos + 2:].lstrip()
                rewritten_rest = _rewrite_segment_with_shell_keywords(rest_body.strip(), commands, rewrite_explicit_ssh)
                if rewritten_rest.rstrip().endswith(" fi"):
                    rewritten_rest = rewritten_rest.rstrip()[:-3].rstrip()
                result = f"{before_then} {rewritten_then} {rewritten_rest} fi"
                if after_fi:
                    result += f" {after_fi}"
                return result
            elif else_pos >= 0:
                then_body = body[:else_pos]
                else_body = body[else_pos + 4:]
                rewritten_then = _rewrite_shell_body(then_body, commands, rewrite_explicit_ssh).strip()
                rewritten_else = _rewrite_shell_body(else_body, commands, rewrite_explicit_ssh).strip()
                before_then = stripped[:then_pos + 4].rstrip()
                after_fi = stripped[fi_pos + 2:].lstrip()
                result = f"{before_then} {rewritten_then} else {rewritten_else} fi"
                if after_fi:
                    result += f" {after_fi}"
                return result
            else:
                rewritten_body = _rewrite_shell_body(body, commands, rewrite_explicit_ssh).strip()
                before_then = stripped[:then_pos + 4].rstrip()
                after_fi = stripped[fi_pos + 2:].lstrip()
                result = f"{before_then} {rewritten_body} fi"
                if after_fi:
                    result += f" {after_fi}"
                return result

    # "else ... fi"
    else_pos = _find_keyword(stripped, 0, "else")
    if else_pos == 0 or (else_pos > 0 and stripped[:else_pos].rstrip().endswith(";")):
        before_else = stripped[:else_pos].rstrip().rstrip(";").strip()
        if not before_else or before_else in (";", "then"):
            fi_pos = _find_keyword(stripped, else_pos + 4, "fi")
            if fi_pos >= 0:
                body = stripped[else_pos + 4:fi_pos]
                rewritten_body = _rewrite_shell_body(body, commands, rewrite_explicit_ssh).strip()
                header = stripped[:else_pos + 4].rstrip()
                after = stripped[fi_pos + 2:].lstrip()
                result = f"{header} {rewritten_body} fi"
                if after:
                    result += f" {after}"
                return result

    return segment


def _strip_rm_flags(args: str) -> str:
    """Remove -r, -f, -R flags from rm arguments."""
    tokens: List[str] = []
    rest = args.strip()

    flags_done = False
    i = 0
    n = len(rest)

    while i < n and not flags_done:
        if rest[i] == "-":
            j = i + 1
            while j < n and not rest[j].isspace():
                j += 1
            flag_token = rest[i:j]

            if flag_token == "--":
                # End of options — everything after is positional
                remaining = rest[j:].strip()
                if remaining:
                    tokens.append(remaining)
                break

            safe_chars = []
            for c in flag_token[1:]:
                if c not in ('r', 'R', 'f'):
                    safe_chars.append(c)
            if safe_chars:
                tokens.append("-" + "".join(safe_chars))
            i = j
        elif rest[i].isspace():
            i += 1
        else:
            flags_done = True

    remaining = rest[i:].strip()
    if remaining:
        tokens.append(remaining)

    return " ".join(tokens)


def _add_backup_flag(args: str, cmd: str) -> str:
    """Add -b flag to mv/cp arguments if not already present."""
    tokens: List[str] = []
    rest = args.strip()
    has_b = False
    i = 0
    n = len(rest)

    while i < n:
        if rest[i] == "-":
            j = i + 1
            while j < n and not rest[j].isspace():
                j += 1
            flag_token = rest[i:j]
            if 'b' in flag_token[1:]:
                has_b = True
            tokens.append(flag_token)
            i = j
        else:
            break

    if not has_b:
        tokens.insert(0, "-b")

    remaining = rest[i:].strip()
    if remaining:
        tokens.append(remaining)

    return " ".join(tokens)


def _rewrite_file_op_tail_fallback(
    tail: str,
    commands: _RewriteCommands,
) -> str | None:
    """Rewrite a tail that starts at rm/mv/cp; None means no file op."""
    m = _RM_RE.match(tail)
    if m:
        args = m.group(4).strip()
        safe_args = _strip_rm_flags(args)
        if safe_args:
            return f"{commands.rm} {safe_args}".strip()
        return commands.rm

    m = _MV_RE.match(tail)
    if m:
        args = m.group(4).strip()
        safe_args = _add_backup_flag(args, "mv")
        return f"{commands.mv} {safe_args}".strip()

    m = _CP_RE.match(tail)
    if m:
        args = m.group(4).strip()
        safe_args = _add_backup_flag(args, "cp")
        return f"{commands.cp} {safe_args}".strip()

    return None


def _rewrite_docker_exec_segment_fallback(
    sudo_prefix: str,
    rest: str,
    commands: _RewriteCommands,
) -> str:
    """Fallback support for `docker exec ... rm/mv/cp` and shell -c forms."""
    word_spans = _shell_words_with_spans(rest)
    tokens = [word for word, _, _ in word_spans]
    unchanged = f"{sudo_prefix}{rest}".strip()
    if len(tokens) < 2 or _base(tokens[0]) != "docker" or tokens[1] != "exec":
        return unchanged

    cmd_idx = _docker_exec_command_index(tokens, 0)
    if cmd_idx is None:
        return unchanged

    inner_offset = _leading_command_index(tokens[cmd_idx:])
    actual_idx = cmd_idx + inner_offset
    if actual_idx >= len(tokens):
        return unchanged

    inner_base = _base(tokens[actual_idx])
    if inner_base in _SHELL_NAMES:
        script_idx = _shell_c_script_index(tokens, actual_idx)
        if script_idx is None:
            return unchanged
        script = tokens[script_idx]
        rewritten = _rewrite_command(script, commands, rewrite_explicit_ssh=False)
        if rewritten == script:
            return unchanged
        start, end = word_spans[script_idx][1], word_spans[script_idx][2]
        raw = rest[start:end]
        replacement = _preserve_outer_quotes(raw, rewritten)
        return f"{sudo_prefix}{rest[:start]}{replacement}{rest[end:]}".strip()

    if inner_base not in _DESTRUCTIVE_CMDS:
        return unchanged

    start = word_spans[actual_idx][1]
    inner = rest[start:]
    rewritten_inner = _rewrite_single_segment_fallback(
        inner,
        commands,
        rewrite_explicit_ssh=False,
    )
    return f"{sudo_prefix}{rest[:start]}{rewritten_inner}".strip()


def _rewrite_single_segment(
    segment: str,
    commands: _RewriteCommands = _LOCAL_COMMANDS,
    rewrite_explicit_ssh: bool = True,
) -> str:
    """Rewrite a single command segment (fallback regex-based).

    Alias for _rewrite_single_segment_fallback for backward compatibility.
    """
    return _rewrite_single_segment_fallback(segment, commands, rewrite_explicit_ssh)


def _rewrite_single_segment_fallback(
    segment: str,
    commands: _RewriteCommands = _LOCAL_COMMANDS,
    rewrite_explicit_ssh: bool = True,
) -> str:
    """Rewrite a single command segment (fallback regex-based)."""
    stripped = segment.strip()
    if not stripped:
        return segment

    result = _rewrite_segment_with_shell_keywords(stripped, commands, rewrite_explicit_ssh)
    if result != stripped:
        return result

    if rewrite_explicit_ssh:
        ssh_result = _rewrite_explicit_ssh_fallback(stripped, commands)
        if ssh_result is not None:
            return ssh_result

    word_spans = _shell_words_with_spans(stripped)
    leading_words = [word for word, _, _ in word_spans]
    leading_idx = _leading_command_index(leading_words)
    if leading_idx >= len(leading_words):
        return segment

    leading_base = _base(leading_words[leading_idx])
    cmd_start = word_spans[leading_idx][1]

    if leading_base == "docker":
        prefix = stripped[:cmd_start]
        rest = stripped[cmd_start:]
        return _rewrite_docker_exec_segment_fallback(prefix, rest, commands)

    if leading_base not in _DESTRUCTIVE_CMDS:
        return segment

    rewritten_tail = _rewrite_file_op_tail_fallback(stripped[cmd_start:], commands)
    if rewritten_tail is None:
        return segment
    return f"{stripped[:cmd_start]}{rewritten_tail}".strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def safe_command_rewrite(command: str, env_type: str = "local") -> str:
    """Rewrite destructive file operations in *command* to safe alternatives.

    Uses bashlex AST parser when available for precise command detection.
    Falls back to regex-based rewriting when bashlex is not installed.

    Args:
        command: The shell command string to potentially rewrite.
        env_type: The execution environment type ("local", "docker", etc.).

    Returns:
        The rewritten command string, or the original if no rewrite was needed
        or if the environment is a sandbox.
    """
    if _is_sandbox(env_type):
        return command

    commands = _commands_for_env(env_type)
    return _rewrite_command(command, commands)


def _rewrite_fallback(
    command: str,
    commands: _RewriteCommands = _LOCAL_COMMANDS,
    rewrite_explicit_ssh: bool = True,
) -> str:
    """Fallback regex-based rewriting when bashlex is not available."""
    segments = _split_respecting_structures(command)
    if not segments:
        return command

    rewritten = [
        (_rewrite_single_segment_fallback(seg, commands, rewrite_explicit_ssh), sep)
        for seg, sep in segments
    ]
    return _join_segments(rewritten)
