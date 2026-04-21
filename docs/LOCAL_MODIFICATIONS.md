# Local Modifications — Hermes Agent Fork

> **Purpose**: Track all deviations from the upstream `NousResearch/hermes-agent` main branch.
> Every new feature, bugfix, or config change added to this fork must be documented here.
>
> **Rule**: When adding a new modification, append a new entry below. Keep existing entries
> up-to-date if the implementation changes. Do NOT delete old entries — they serve as an audit trail.

---

## How to read this doc

Each entry includes:
| Field | Description |
|-------|-------------|
| **Date** | When the modification was made |
| **Commit** | Git commit hash |
| **Files** | Which files were added/modified |
| **What** | What was changed and why |
| **Upstream status** | Whether this has been submitted as a PR to upstream, or is fork-only |

---

## Modification Log

### 1. Safe command rewrite (rm/mv/cp → trash/gmv/gcp)

| Field | Value |
|-------|-------|
| **Date** | 2026-04-21 |
| **Commit** | `5513a9b9` |
| **Files** | `tools/safe_cmd_rewrite.py` (new, 765 lines), `tests/tools/test_safe_cmd_rewrite.py` (new, 208 tests), `tools/terminal_tool.py` (+7 lines), `pyproject.toml` (+1 dep: bashlex) |
| **What** | Automatically replaces destructive shell commands with safe alternatives at the terminal tool execution layer:<br>• `rm [flags] files` → `trash files` (strips -r/-f flags since trash handles recursion automatically)<br>• `mv [flags] src dst` → `gmv -b [flags] src dst` (auto backup on overwrite)<br>• `cp [flags] src dst` → `gcp -b [flags] src dst` (auto backup on overwrite)<br><br>Uses bashlex AST parser for precise shell command detection, correctly handling for/while/if/case structures, $() subshells, subshells (...), compound commands (&& \|\| ; \|), sudo/env/nice/timeout wrappers, env-var prefixes (FOO=bar rm), -- option separator, and arbitrary absolute paths (/opt/homebrew/bin/rm).<br><br>Correctly ignores: git rm, echo "rm", comments, find -delete, rsync --delete, words containing 'rm' (firmware, arm, rmdir).<br><br>Sandbox environments (docker/modal/singularity/ssh) skip rewriting. Graceful fallback to regex when bashlex is unavailable. |
| **Upstream status** | Fork-only. No upstream PR exists for this feature. |

---

<!-- Add new modifications below this line. -->
