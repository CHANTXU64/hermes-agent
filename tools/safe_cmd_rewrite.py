"""Safe command rewrite — replace destructive file operations with safe alternatives.

Uses bashlex AST parser to correctly identify rm/mv/cp commands in shell
syntax, avoiding false positives from strings, subcommands, etc.

Rewrites:
  rm [flags] files  →  trash files
  mv [flags] src dst  →  gmv -b [flags] src dst
  cp [flags] src dst  →  gcp -b [flags] src dst

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
- Sandbox environments (docker/modal/singularity/daytona/ssh) skip rewriting entirely.

Known limitations:
- Malformed shell commands (e.g. double ;; outside case) may not be parsed
  by bashlex and will be returned unchanged (safe fallback).
- Heredocs (<<EOF ... EOF) are not specially handled.
- When bashlex is not installed, falls back to regex-based rewriting with
  the original limitations (no $(...)/subshell rewriting).
"""

from __future__ import annotations

import re
from typing import List, Tuple

try:
    import bashlex
    import bashlex.ast
    _HAS_BASHLEX = True
except ImportError:
    _HAS_BASHLEX = False

# ---------------------------------------------------------------------------
# Sandbox detection
# ---------------------------------------------------------------------------

_SANDBOX_ENVS: frozenset[str] = frozenset({
    "docker", "modal", "singularity", "daytona", "ssh",
})


def _is_sandbox(env_type: str) -> bool:
    """Return True if the environment is a sandbox where rewriting should be skipped."""
    return env_type.lower() in _SANDBOX_ENVS


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


class _RewriteVisitor(bashlex.ast.nodevisitor):
    """Visitor that finds rm/mv/cp commands and records position-based replacements."""

    def __init__(self, original: str) -> None:
        self.original = original
        self.replacements: List[Tuple[int, int, str]] = []

    def visitcommand(self, node, parts):
        words = [p.word for p in parts if hasattr(p, 'word')]
        if not words:
            return True

        # Skip VCS subcommands: git rm, hg rm, svn rm, bzr rm
        if words[0] in _VCS_TOOLS and len(words) >= 2 and words[1] == 'rm':
            return True

        # Skip already-safe commands
        if words[0] in ('trash', 'gmv', 'gcp'):
            return True

        # Find the actual destructive command, skipping wrappers
        cmd_idx = 0
        in_wrapper = False
        skip_next = False

        for i, w in enumerate(words):
            if skip_next:
                skip_next = False
                cmd_idx = i + 1
                continue

            base = w.split('/')[-1]

            if base in _WRAPPER_NAMES or w in _WRAPPER_NAMES:
                in_wrapper = True
                cmd_idx = i + 1
            elif in_wrapper and w.startswith('-'):
                # Single-char flag after certain wrappers may take a value arg
                flag_only = len(w) == 2
                w_base = words[cmd_idx - 1].split('/')[-1] if cmd_idx > 0 else ''
                if flag_only and w_base in _WRAPPERS_TAKING_EXTRA_ARG:
                    # Check if next token looks like a value (not a flag, not a command)
                    if i + 1 < len(words):
                        next_w = words[i + 1]
                        if (not next_w.startswith('-')
                                and '=' not in next_w
                                and next_w.split('/')[-1] not in _DESTRUCTIVE_CMDS):
                            skip_next = True
                cmd_idx = i + 1
            elif in_wrapper and not w.startswith('-') and '=' not in w:
                # Non-flag token in wrapper mode — could be the actual command
                if base in _DESTRUCTIVE_CMDS:
                    cmd_idx = i
                    break
                else:
                    # Unknown token — might be wrapper arg (e.g. timeout DURATION)
                    # Consume it and continue looking
                    cmd_idx = i + 1
            elif base in _DESTRUCTIVE_CMDS:
                cmd_idx = i
                break
            elif '=' in w:
                # Environment variable assignment prefix: FOO=bar rm file.txt
                # Skip over it and keep looking for the actual command
                cmd_idx = i + 1
                continue
            else:
                break

        actual_cmd = words[cmd_idx] if cmd_idx < len(words) else None
        if not actual_cmd:
            return True

        actual_base = actual_cmd.split('/')[-1]
        if actual_base not in _DESTRUCTIVE_CMDS:
            return True

        cmd_part = parts[cmd_idx]
        cmd_start, cmd_end = cmd_part.pos

        if actual_base == 'rm':
            self.replacements.append((cmd_start, cmd_end, 'trash'))
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
            self.replacements.append((cmd_start, cmd_end, 'gmv'))
            has_b = any(
                hasattr(p, 'word') and p.word.startswith('-') and 'b' in p.word
                for p in parts[cmd_idx + 1:]
                if hasattr(p, 'word') and p.word.startswith('-') and p.word != '--'
            )
            if not has_b:
                self.replacements.append((cmd_end, cmd_end, ' -b'))

        elif actual_base == 'cp':
            self.replacements.append((cmd_start, cmd_end, 'gcp'))
            has_b = any(
                hasattr(p, 'word') and p.word.startswith('-') and 'b' in p.word
                for p in parts[cmd_idx + 1:]
                if hasattr(p, 'word') and p.word.startswith('-') and p.word != '--'
            )
            if not has_b:
                self.replacements.append((cmd_end, cmd_end, ' -b'))

        return True


def _rewrite_with_ast(command: str) -> str:
    """Rewrite using bashlex AST parser."""
    try:
        asts = bashlex.parse(command)
    except Exception:
        return command

    visitor = _RewriteVisitor(command)
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


def _rewrite_shell_body(body: str) -> str:
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
                parts.append(_rewrite_single_segment_fallback(seg))
            parts.append(ch + " ")
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1

    seg = "".join(current).strip()
    if seg:
        parts.append(_rewrite_single_segment_fallback(seg))

    return "".join(parts).rstrip()


def _rewrite_segment_with_shell_keywords(segment: str) -> str:
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
                rewritten_body = _rewrite_shell_body(body)
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
                rewritten_then = _rewrite_shell_body(then_body).strip()
                before_then = stripped[:then_pos + 4].rstrip()
                after_fi = stripped[fi_pos + 2:].lstrip()
                rewritten_rest = _rewrite_segment_with_shell_keywords(rest_body.strip())
                if rewritten_rest.rstrip().endswith(" fi"):
                    rewritten_rest = rewritten_rest.rstrip()[:-3].rstrip()
                result = f"{before_then} {rewritten_then} {rewritten_rest} fi"
                if after_fi:
                    result += f" {after_fi}"
                return result
            elif else_pos >= 0:
                then_body = body[:else_pos]
                else_body = body[else_pos + 4:]
                rewritten_then = _rewrite_shell_body(then_body).strip()
                rewritten_else = _rewrite_shell_body(else_body).strip()
                before_then = stripped[:then_pos + 4].rstrip()
                after_fi = stripped[fi_pos + 2:].lstrip()
                result = f"{before_then} {rewritten_then} else {rewritten_else} fi"
                if after_fi:
                    result += f" {after_fi}"
                return result
            else:
                rewritten_body = _rewrite_shell_body(body).strip()
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
                rewritten_body = _rewrite_shell_body(body).strip()
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


def _rewrite_single_segment(segment: str) -> str:
    """Rewrite a single command segment (fallback regex-based).

    Alias for _rewrite_single_segment_fallback for backward compatibility.
    """
    return _rewrite_single_segment_fallback(segment)


def _rewrite_single_segment_fallback(segment: str) -> str:
    """Rewrite a single command segment (fallback regex-based)."""
    stripped = segment.strip()
    if not stripped:
        return segment

    result = _rewrite_segment_with_shell_keywords(stripped)
    if result != stripped:
        return result

    sudo_prefix = ""
    rest = stripped
    if rest.startswith("sudo "):
        sudo_prefix = "sudo "
        rest = rest[5:].lstrip()

    wrapper_prefix = ""
    tokens = rest.split()

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok not in _WRAPPER_NAMES:
            break

        wname = tok
        i += 1
        wrapper_prefix += wname + " "

        if wname in _WRAPPERS_TAKING_EXTRA_ARG and i < len(tokens):
            next_tok = tokens[i]
            if not next_tok.startswith("-") and "=" not in next_tok:
                base = next_tok.split("/")[-1]
                if base not in _DESTRUCTIVE_CMDS:
                    wrapper_prefix += next_tok + " "
                    i += 1

        while i < len(tokens):
            next_tok = tokens[i]
            base = next_tok.split("/")[-1]
            if base in _DESTRUCTIVE_CMDS:
                break
            if "=" in next_tok:
                wrapper_prefix += next_tok + " "
                i += 1
            elif next_tok.startswith("-") and len(next_tok) > 1:
                wrapper_prefix += next_tok + " "
                i += 1
                if (i < len(tokens) and len(next_tok) == 2
                        and not tokens[i].startswith("-")
                        and "=" not in tokens[i]):
                    val_base = tokens[i].split("/")[-1]
                    if val_base not in _DESTRUCTIVE_CMDS:
                        wrapper_prefix += tokens[i] + " "
                        i += 1
            else:
                break

    if i == 0:
        wrapper_prefix = ""
    elif i < len(tokens):
        rest = " ".join(tokens[i:])
    else:
        return segment

    if not rest:
        return segment

    m = _RM_RE.match(rest)
    if m:
        args = m.group(4).strip()
        safe_args = _strip_rm_flags(args)
        if safe_args:
            return f"{sudo_prefix}{wrapper_prefix}trash {safe_args}".strip()
        else:
            return f"{sudo_prefix}{wrapper_prefix}trash".strip()

    m = _MV_RE.match(rest)
    if m:
        args = m.group(4).strip()
        safe_args = _add_backup_flag(args, "mv")
        return f"{sudo_prefix}{wrapper_prefix}gmv {safe_args}".strip()

    m = _CP_RE.match(rest)
    if m:
        args = m.group(4).strip()
        safe_args = _add_backup_flag(args, "cp")
        return f"{sudo_prefix}{wrapper_prefix}gcp {safe_args}".strip()

    return segment


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

    if _HAS_BASHLEX:
        return _rewrite_with_ast(command)
    else:
        return _rewrite_fallback(command)


def _rewrite_fallback(command: str) -> str:
    """Fallback regex-based rewriting when bashlex is not available."""
    segments = _split_respecting_structures(command)
    if not segments:
        return command

    rewritten = [
        (_rewrite_single_segment_fallback(seg), sep)
        for seg, sep in segments
    ]
    return _join_segments(rewritten)
