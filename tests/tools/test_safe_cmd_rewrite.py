"""Tests for the safe command rewrite module.

Covers:
- Basic rm/mv/cp rewriting
- Flag stripping for rm (-r, -f, -R)
- Backup flag for mv/cp (-b)
- sudo and absolute path prefixes
- Wrapper commands (nice, env, timeout, xargs, etc.)
- Compound commands (&& || ; |)
- Shell structures (for/while/until/if/case)
- Sandbox environment skipping
- Negative tests (no false positives)
"""

from __future__ import annotations

import pytest

from tools.safe_cmd_rewrite import (
    _add_backup_flag,
    _find_keyword,
    _rewrite_shell_body,
    _rewrite_segment_with_shell_keywords,
    _rewrite_single_segment,
    _split_respecting_structures,
    _strip_rm_flags,
    safe_command_rewrite,
)


# ===========================================================================
# Helper function tests
# ===========================================================================

class TestStripRmFlags:
    """Test that -r, -f, -R flags are stripped from rm arguments."""

    @pytest.mark.parametrize("args,expected", [
        ("-rf dir", "dir"),
        ("-fr dir", "dir"),
        ("-r -f dir", "dir"),
        ("-rv dir", "-v dir"),
        ("-rfv dir", "-v dir"),
        ("-R dir", "dir"),
        ("-f dir", "dir"),
        ("-r dir", "dir"),
        ("-v dir", "-v dir"),
        ("dir", "dir"),
        ("-rf /tmp/a /tmp/b", "/tmp/a /tmp/b"),
        ("-v -r dir", "-v dir"),
    ])
    def test_strip_rm_flags(self, args, expected):
        assert _strip_rm_flags(args) == expected

    def test_empty_args(self):
        assert _strip_rm_flags("") == ""

    def test_whitespace_only(self):
        assert _strip_rm_flags("   ") == ""


class TestAddBackupFlag:
    """Test that -b flag is added to mv/cp arguments if not present."""

    @pytest.mark.parametrize("args,expected", [
        ("src dst", "-b src dst"),
        ("-r src dst", "-b -r src dst"),
        ("-b src dst", "-b src dst"),
        ("-rb src dst", "-rb src dst"),
        ("-br src dst", "-br src dst"),
        ("-a src dst", "-b -a src dst"),
    ])
    def test_add_backup_flag(self, args, expected):
        assert _add_backup_flag(args, "mv") == expected

    def test_empty_args(self):
        assert _add_backup_flag("", "mv") == "-b"


class TestFindKeyword:
    """Test the keyword finder utility."""

    def test_basic_match(self):
        assert _find_keyword("done", 0, "done") == 0

    def test_match_after_space(self):
        assert _find_keyword("for f in *.tmp; do rm $f; done", 0, "done") > 0

    def test_no_match(self):
        assert _find_keyword("echo hello", 0, "done") == -1

    def test_word_boundary_no_match(self):
        # "done" inside "undone" should not match
        assert _find_keyword("undone", 0, "done") == -1

    def test_word_boundary_match(self):
        assert _find_keyword("echo done", 0, "done") > 0

    def test_inside_subshell_no_match(self):
        # "done" inside (...) should not match at depth 0
        cmd = "echo (done)"
        pos = _find_keyword(cmd, 0, "done")
        # The "done" inside parens should not be found at depth 0
        assert pos == -1


# ===========================================================================
# Split respecting structures tests
# ===========================================================================

class TestSplitRespectingStructures:
    """Test that compound command splitting respects shell structures."""

    def test_simple_command(self):
        segments = _split_respecting_structures("rm file.txt")
        assert len(segments) == 1
        assert segments[0] == ("rm file.txt", "")

    def test_two_commands_semicolon(self):
        segments = _split_respecting_structures("rm a; rm b")
        assert len(segments) == 2
        assert segments[0] == ("rm a", ";")
        assert segments[1] == ("rm b", "")

    def test_two_commands_and(self):
        segments = _split_respecting_structures("rm a && rm b")
        assert len(segments) == 2
        assert segments[0] == ("rm a", "&&")
        assert segments[1] == ("rm b", "")

    def test_for_loop_not_split(self):
        segments = _split_respecting_structures("for f in *.tmp; do rm $f; done")
        assert len(segments) == 1
        assert segments[0][0] == "for f in *.tmp; do rm $f; done"

    def test_while_loop_not_split(self):
        segments = _split_respecting_structures("while read line; do rm $line; done")
        assert len(segments) == 1
        assert segments[0][0] == "while read line; do rm $line; done"

    def test_if_statement_not_split(self):
        segments = _split_respecting_structures("if [ -f a ]; then rm a; fi")
        assert len(segments) == 1
        assert segments[0][0] == "if [ -f a ]; then rm a; fi"

    def test_for_loop_and_then(self):
        segments = _split_respecting_structures(
            "for f in *.tmp; do rm $f; done && echo done"
        )
        assert len(segments) == 2
        assert segments[0] == ("for f in *.tmp; do rm $f; done", "&&")
        assert segments[1] == ("echo done", "")

    def test_rm_before_for(self):
        segments = _split_respecting_structures("rm /tmp/a; for f in *.tmp; do rm $f; done")
        assert len(segments) == 2
        assert segments[0] == ("rm /tmp/a", ";")
        assert segments[1][0] == "for f in *.tmp; do rm $f; done"

    def test_if_else_not_split(self):
        segments = _split_respecting_structures(
            "if [ -d old ]; then rm -rf old; else mv old old.bak; fi"
        )
        assert len(segments) == 1

    def test_subshell_not_split(self):
        segments = _split_respecting_structures("(cd /tmp; rm -rf *)")
        assert len(segments) == 1
        assert segments[0][0] == "(cd /tmp; rm -rf *)"

    def test_empty_input(self):
        segments = _split_respecting_structures("")
        assert segments == []

    def test_whitespace_only(self):
        segments = _split_respecting_structures("   ")
        assert segments == []

    def test_pipe(self):
        segments = _split_respecting_structures("cat file | grep rm")
        assert len(segments) == 2
        assert segments[0] == ("cat file", "|")
        assert segments[1] == ("grep rm", "")

    def test_or(self):
        segments = _split_respecting_structures("rm a || echo fail")
        assert len(segments) == 2
        assert segments[0] == ("rm a", "||")
        assert segments[1] == ("echo fail", "")


# ===========================================================================
# Basic rewriting tests
# ===========================================================================

class TestBasicRewriting:
    """Test basic rm/mv/cp rewriting."""

    @pytest.mark.parametrize("cmd,expected", [
        # rm → trash
        ("rm file.txt", "trash file.txt"),
        ("rm -rf dir/", "trash dir/"),
        ("rm -f file", "trash file"),
        ("rm -r dir", "trash dir"),
        ("rm -R dir", "trash dir"),
        ("rm -rfv file", "trash -v file"),
        ("rm -v file", "trash -v file"),
        ("rm file1 file2", "trash file1 file2"),
        # mv → gmv -b
        ("mv src dst", "gmv -b src dst"),
        ("mv -f src dst", "gmv -b -f src dst"),
        ("mv -b src dst", "gmv -b src dst"),
        ("mv -rb src dst", "gmv -rb src dst"),
        # cp → gcp -b
        ("cp src dst", "gcp -b src dst"),
        ("cp -r src dst", "gcp -b -r src dst"),
        ("cp -b src dst", "gcp -b src dst"),
        ("cp -a src dst", "gcp -b -a src dst"),
    ])
    def test_basic_rewrites(self, cmd, expected):
        assert safe_command_rewrite(cmd) == expected

    def test_bare_rm_no_args(self):
        """Bare rm with no args is rewritten to trash (no-op either way)."""
        assert safe_command_rewrite("rm") == "trash"

    def test_bare_mv_no_args(self):
        assert safe_command_rewrite("mv") == "gmv -b"

    def test_bare_cp_no_args(self):
        assert safe_command_rewrite("cp") == "gcp -b"


class TestSudoAndPath:
    """Test sudo and absolute path handling."""

    @pytest.mark.parametrize("cmd,expected", [
        ("sudo rm file", "sudo trash file"),
        ("sudo rm -rf /tmp/a", "sudo trash /tmp/a"),
        ("sudo mv a b", "sudo gmv -b a b"),
        ("sudo cp a b", "sudo gcp -b a b"),
        ("/bin/rm file", "trash file"),
        ("/usr/bin/rm -rf dir", "trash dir"),
        ("/bin/mv a b", "gmv -b a b"),
        ("/usr/bin/cp a b", "gcp -b a b"),
        ("sudo /bin/rm file", "sudo trash file"),
    ])
    def test_sudo_and_path(self, cmd, expected):
        assert safe_command_rewrite(cmd) == expected


class TestWrapperCommands:
    """Test wrapper command handling."""

    @pytest.mark.parametrize("cmd,expected", [
        ("nice rm file", "nice trash file"),
        ("nice -n 10 rm file", "nice -n 10 trash file"),
        ("env rm file", "env trash file"),
        ("timeout 10 rm file", "timeout 10 trash file"),
        ("xargs rm", "xargs trash"),
        ("stdbuf rm file", "stdbuf trash file"),
        ("ionice rm file", "ionice trash file"),
        ("chroot / rm file", "chroot / trash file"),
        ("nohup rm file", "nohup trash file"),
        ("exec rm file", "exec trash file"),
        ("nice env rm file", "nice env trash file"),
    ])
    def test_wrappers(self, cmd, expected):
        assert safe_command_rewrite(cmd) == expected


class TestCompoundCommands:
    """Test compound command rewriting."""

    @pytest.mark.parametrize("cmd,expected", [
        ("rm a && rm b", "trash a && trash b"),
        ("rm a || echo fail", "trash a || echo fail"),
        ("rm a; rm b", "trash a; trash b"),  # AST preserves original spacing
        ("rm a | grep b", "trash a | grep b"),
        ("rm a && mv b c || cp d e",
         "trash a && gmv -b b c || gcp -b d e"),
        ("echo ok && rm file", "echo ok && trash file"),
    ])
    def test_compound(self, cmd, expected):
        assert safe_command_rewrite(cmd) == expected


# ===========================================================================
# Shell structure tests (Problem 1)
# ===========================================================================

class TestShellStructures:
    """Test that rm/mv/cp inside shell structures are correctly rewritten."""

    # --- for loops ---
    @pytest.mark.parametrize("cmd,expected", [
        (
            "for f in *.tmp; do rm $f; done",
            "for f in *.tmp; do trash $f; done",
        ),
        (
            "for i in 1 2 3; do rm $i; done",
            "for i in 1 2 3; do trash $i; done",
        ),
        (
            "for f in *.log; do rm -rf $f; done",
            "for f in *.log; do trash $f; done",
        ),
        (
            "for f in *.log; do mv $f archived/; done",
            "for f in *.log; do gmv -b $f archived/; done",
        ),
        (
            "for f in *.log; do cp $f backup/; done",
            "for f in *.log; do gcp -b $f backup/; done",
        ),
    ])
    def test_for_loop(self, cmd, expected):
        assert safe_command_rewrite(cmd) == expected

    # --- while loops ---
    @pytest.mark.parametrize("cmd,expected", [
        (
            'while read line; do rm "$line"; done',
            'while read line; do trash "$line"; done',
        ),
        (
            "while [ -f lock ]; do rm work.txt; done",
            "while [ -f lock ]; do trash work.txt; done",
        ),
    ])
    def test_while_loop(self, cmd, expected):
        assert safe_command_rewrite(cmd) == expected

    # --- until loops ---
    @pytest.mark.parametrize("cmd,expected", [
        (
            "until [ -f stop ]; do rm work.txt; done",
            "until [ -f stop ]; do trash work.txt; done",
        ),
    ])
    def test_until_loop(self, cmd, expected):
        assert safe_command_rewrite(cmd) == expected

    # --- if/then/fi ---
    @pytest.mark.parametrize("cmd,expected", [
        (
            "if [ -f a.txt ]; then mv a.txt b.txt; fi",
            "if [ -f a.txt ]; then gmv -b a.txt b.txt; fi",
        ),
        (
            "if [ -f a ]; then rm a; fi",
            "if [ -f a ]; then trash a; fi",
        ),
        (
            "if true; then rm -rf /tmp/a; fi",
            "if true; then trash /tmp/a; fi",
        ),
    ])
    def test_if_then_fi(self, cmd, expected):
        assert safe_command_rewrite(cmd) == expected

    # --- if/then/else/fi ---
    @pytest.mark.parametrize("cmd,expected", [
        (
            "if [ -d old ]; then rm -rf old; else mv old old.bak; fi",
            "if [ -d old ]; then trash old; else gmv -b old old.bak; fi",
        ),
        (
            "if true; then rm a; else rm b; fi",
            "if true; then trash a; else trash b; fi",
        ),
        (
            "if [ -f a ]; then cp a b; else mv a c; fi",
            "if [ -f a ]; then gcp -b a b; else gmv -b a c; fi",
        ),
    ])
    def test_if_then_else_fi(self, cmd, expected):
        assert safe_command_rewrite(cmd) == expected

    # --- if/then/elif/fi ---
    @pytest.mark.parametrize("cmd,expected", [
        (
            "if true; then rm a; elif false; then rm b; else rm c; fi",
            "if true; then trash a; elif false; then trash b; else trash c; fi",
        ),
        (
            "if [ -f a ]; then rm a; elif [ -f b ]; then rm b; fi",
            "if [ -f a ]; then trash a; elif [ -f b ]; then trash b; fi",
        ),
    ])
    def test_if_then_elif_fi(self, cmd, expected):
        assert safe_command_rewrite(cmd) == expected

    # --- Shell structures combined with compound operators ---
    @pytest.mark.parametrize("cmd,expected", [
        (
            "for f in *.log; do rm $f; done && echo done",
            "for f in *.log; do trash $f; done && echo done",
        ),
        (
            "rm -rf /tmp/a; for f in *.tmp; do rm $f; done",
            "trash /tmp/a; for f in *.tmp; do trash $f; done",
        ),
        (
            "if [ -f a ]; then rm a; fi && echo ok",
            "if [ -f a ]; then trash a; fi && echo ok",
        ),
        (
            "for i in 1 2 3; do rm $i; done; echo done",
            "for i in 1 2 3; do trash $i; done; echo done",
        ),
    ])
    def test_shell_structures_with_compound(self, cmd, expected):
        assert safe_command_rewrite(cmd) == expected

    # --- sudo + shell structures ---
    # Note: "sudo for ..." is unusual syntax and not supported as a known
    # limitation. `sudo` is treated as a wrapper prefix, but `for` is not
    # a destructive command, so the wrapper logic returns unchanged.

    # --- Multiple commands in shell body ---
    @pytest.mark.parametrize("cmd,expected", [
        (
            "for f in *.tmp; do rm $f; mv ${f}.bak $f; done",
            "for f in *.tmp; do trash $f; gmv -b ${f}.bak $f; done",
        ),
    ])
    def test_multiple_commands_in_body(self, cmd, expected):
        assert safe_command_rewrite(cmd) == expected


# ===========================================================================
# Sandbox environment tests
# ===========================================================================

class TestSandboxSkip:
    """Test that sandbox environments skip rewriting entirely."""

    @pytest.mark.parametrize("env_type", [
        "docker", "modal", "singularity", "daytona", "ssh",
        "DOCKER", "Docker",
    ])
    def test_sandbox_skip(self, env_type):
        assert safe_command_rewrite("rm file", env_type=env_type) == "rm file"

    def test_local_does_rewrite(self):
        assert safe_command_rewrite("rm file", env_type="local") == "trash file"

    def test_unknown_env_does_rewrite(self):
        assert safe_command_rewrite("rm file", env_type="k8s") == "trash file"


# ===========================================================================
# Negative tests (Problem 2 — prevent false positives)
# ===========================================================================

class TestNegativeVersionControl:
    """Version control sub-commands should NOT be rewritten."""

    @pytest.mark.parametrize("cmd", [
        "git rm file.txt",
        "git rm --cached file.txt",
        "hg rm file.txt",
        "svn rm file.txt",
        "bzr rm file.txt",
    ])
    def test_vcs_rm(self, cmd):
        assert safe_command_rewrite(cmd) == cmd


class TestNegativeDeleteCommands:
    """Other delete-related commands should NOT be rewritten."""

    @pytest.mark.parametrize("cmd", [
        'find . -name "*.pyc" -delete',
        "rsync -a --delete src/ dst/",
        "npm uninstall package",
        "pip uninstall package",
        "kubectl delete pod mypod",
        "docker rm container",
        "docker rmi image",
    ])
    def test_delete_commands(self, cmd):
        assert safe_command_rewrite(cmd) == cmd


class TestNegativeNoDoubleRewrite:
    """Already-safe commands should NOT be double-rewritten."""

    @pytest.mark.parametrize("cmd", [
        "trash file.txt",
        "gmv -b src dst",
        "gcp -b src dst",
    ])
    def test_no_double_rewrite(self, cmd):
        assert safe_command_rewrite(cmd) == cmd


class TestNegativeRmAsArgument:
    """rm/mv/cp as arguments to other commands should NOT be rewritten."""

    @pytest.mark.parametrize("cmd", [
        "echo rm file",
        "cat rm_list.txt",
        "vim rm_notes.txt",
        'grep "rm" file.txt',
        'grep -l "rm" *.py',
        'sed "s/old/new/g" file.txt',
    ])
    def test_rm_as_argument(self, cmd):
        assert safe_command_rewrite(cmd) == cmd


class TestNegativeRmInStrings:
    """rm/mv/cp inside strings should NOT be rewritten."""

    @pytest.mark.parametrize("cmd", [
        'echo "rm -rf /"',
        'echo "please do not rm this"',
        "echo 'rm -rf /'",
    ])
    def test_rm_in_strings(self, cmd):
        assert safe_command_rewrite(cmd) == cmd


class TestNegativeWordsContainingRm:
    """Words containing 'rm' but not the rm command should NOT trigger."""

    @pytest.mark.parametrize("cmd", [
        "firmware update",
        "arm-none-eabi-gcc main.c",
        "termux-setup-storage",
        "normal_command",
        "cp_file.txt",
        "rm_suffix.txt",
        'term "something"',
        "warm up",
        "storm warning",
        "format disk",
        "program file",
        "german text",
        "farm animals",
        "alarm clock",
        "harm reduction",
        "prune old branches",
        "confirm action",
    ])
    def test_words_containing_rm(self, cmd):
        assert safe_command_rewrite(cmd) == cmd


class TestNegativeCommonCommands:
    """Common commands that should never be rewritten."""

    @pytest.mark.parametrize("cmd", [
        "mkdir -p new_dir",
        "ln -s src dst",
        "chmod +x script.sh",
        "chown user:group file",
        "tar -czf archive.tar.gz dir/",
        "ls -la",
        "cat file.txt",
        "echo hello",
        "cd /tmp",
        "pwd",
        "whoami",
        "ps aux",
        "kill -9 1234",
        "wget https://example.com",
        "curl https://example.com",
    ])
    def test_common_commands(self, cmd):
        assert safe_command_rewrite(cmd) == cmd


class TestNegativeRmLikeCommands:
    """Commands with rm/mv/cp prefix but different names."""

    @pytest.mark.parametrize("cmd", [
        "rmdir dir",
        "rmmod module",
        "mvnw build",
        "cpplint file.cpp",
        "mvp project",
        "cpanm Module",
    ])
    def test_rm_like_commands(self, cmd):
        assert safe_command_rewrite(cmd) == cmd


class TestNegativeVariablesAndAssignments:
    """Environment variables and assignments should NOT be rewritten."""

    @pytest.mark.parametrize("cmd", [
        "export RM_DIR=/tmp",
        "RM=1 ./script.sh",
        "$RM file",
    ])
    def test_variables(self, cmd):
        assert safe_command_rewrite(cmd) == cmd


class TestNegativeUnknownCommand:
    """Unknown commands followed by rm should NOT be rewritten."""

    def test_unknown_command_prefix(self):
        # "command_not_found" is not in the wrapper list, so rm won't be found
        cmd = "command_not_found rm file"
        assert safe_command_rewrite(cmd) == cmd


# ===========================================================================
# Edge cases
# ===========================================================================

class TestEdgeCases:
    """Edge cases and corner scenarios."""

    def test_empty_command(self):
        assert safe_command_rewrite("") == ""

    def test_whitespace_command(self):
        assert safe_command_rewrite("   ") == "   "

    def test_comment_only(self):
        assert safe_command_rewrite("# rm file") == "# rm file"

    def test_subshell_with_rm(self):
        # bashlex AST can now parse and rewrite commands inside subshells
        cmd = "(cd /tmp; rm -rf *)"
        result = safe_command_rewrite(cmd)
        assert result == "(cd /tmp; trash *)"

    def test_command_substitution(self):
        # bashlex AST can now parse and rewrite commands inside $(...)
        cmd = "echo $(rm file)"
        result = safe_command_rewrite(cmd)
        assert result == "echo $(trash file)"

    def test_multiple_semicolons(self):
        # Double semicolons are only valid inside case statements.
        # bashlex cannot parse this, so it falls back to original.
        cmd = "rm a;; rm b"
        result = safe_command_rewrite(cmd)
        # bashlex parse error → returns original command
        assert result == cmd

    def test_heredoc_reference(self):
        # Heredocs are a known limitation
        cmd = "cat <<EOF\nrm file\nEOF"
        # Should not crash
        safe_command_rewrite(cmd)

    def test_backslash_continuation(self):
        cmd = "rm \\\\\nfile.txt"
        # Should not crash, may or may not rewrite depending on implementation
        safe_command_rewrite(cmd)


# ===========================================================================
# BUG fix regression tests (dash separator + env-var prefix)
# ===========================================================================

class TestDoubleDashSeparator:
    """Test that '--' correctly marks end of options.

    Everything after '--' is positional, even if it starts with '-'.
    The rewrite must NOT strip flags or treat these as flags.
    """

    @pytest.mark.parametrize("cmd,expected", [
        # rm: preserve filenames that start with '-'
        ("rm -- -negfile.txt", "trash -- -negfile.txt"),
        ("rm -- -rf /tmp/test", "trash -- -rf /tmp/test"),
        ("rm -- -b -c file.txt", "trash -- -b -c file.txt"),
        ("rm -rf -- -dangerous", "trash -- -dangerous"),
        # mv: preserve filenames
        ("mv -- -old.txt -new.txt", "gmv -b -- -old.txt -new.txt"),
        ("mv -f -- -src -dst", "gmv -b -f -- -src -dst"),
        # cp: preserve filenames
        ("cp -- -src -dst", "gcp -b -- -src -dst"),
        ("cp -r -- -a /backup/", "gcp -b -r -- -a /backup/"),
        # Absolute path variants
        ("/bin/rm -- -file", "trash -- -file"),
        ("/usr/bin/rm -- -file", "trash -- -file"),
    ])
    def test_double_dash_preserves_positional(self, cmd, expected):
        result = safe_command_rewrite(cmd, "local")
        assert result == expected, f"{cmd!r} → {result!r}, expected {expected!r}"


class TestEnvVarPrefix:
    """Test that environment variable assignments before the command
    are correctly skipped over, and the actual command is rewritten.

    In shell, `FOO=bar cmd` runs `cmd` with `FOO=bar` in its environment.
    The assignment is NOT a wrapper command.
    """

    @pytest.mark.parametrize("cmd,expected", [
        # rm with env var prefix
        ("FOO=bar rm file.txt", "FOO=bar trash file.txt"),
        ("PATH=/usr/bin rm -rf /tmp/test", "PATH=/usr/bin trash /tmp/test"),
        ("A=1 B=2 rm -f a.txt b.txt", "A=1 B=2 trash a.txt b.txt"),
        # mv with env var prefix
        ("FOO=bar mv a.txt b.txt", "FOO=bar gmv -b a.txt b.txt"),
        ("HOME=/tmp mv src dest", "HOME=/tmp gmv -b src dest"),
        # cp with env var prefix
        ("FOO=bar cp -r src/ dst/", "FOO=bar gcp -b -r src/ dst/"),
        # Combined: sudo + env + rm
        ("sudo FOO=bar rm file.txt", "sudo FOO=bar trash file.txt"),
        # Multiple env vars
        ("A=1 B=2 C=3 rm file.txt", "A=1 B=2 C=3 trash file.txt"),
    ])
    def test_env_var_prefix_rewritten(self, cmd, expected):
        result = safe_command_rewrite(cmd, "local")
        assert result == expected, f"{cmd!r} → {result!r}, expected {expected!r}"
