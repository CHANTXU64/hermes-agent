# Auditable built-in memory governance

Status: fork-owned policy and audit boundary; it takes effect when a process
loads this version.

## Purpose

Hermes injects `MEMORY.md` and `USER.md` into future sessions under tight
character limits. Earlier behavior encouraged shortening or deleting "stale or
less important" entries without retaining why an entry existed. This fork keeps
memory autonomous while making every public memory mutation traceable.

## Behavior

- Successful `memory` tool writes append one structured event per line to
  `MEMORY_CHANGELOG.jsonl`, including the exact before/after entry, reason,
  evidence, semantic change type, batch transaction ID, related Skill, and
  deletion classification when applicable.
- Public mutations require a reason and evidence. Removal also requires one of:
  `safe`, `expired`, or `forced_capacity`; forced capacity eviction requires a
  loss note.
- The memory target lock remains held through journaling. A checked disk read
  captures the rollback state before mutation; unreadable state aborts the write.
  If journaling fails after the memory file changes, the write is rolled back. If
  a manual writer ignored the lock and changed the file again, rollback preserves
  those newer bytes and returns a critical error instead of restoring a stale
  snapshot.
- The first governed write creates an honest baseline inventory of existing
  `MEMORY.md` and `USER.md` entries. Unknown historical origins stay marked
  unknown rather than being reconstructed by guesswork.
- Background memory review receives only the latest on-disk `MEMORY.md` and
  `USER.md` in its uncached user message. Entries are threat-scanned and
  JSON-encoded as data before insertion; the full audit log is never injected.
- A pure add does not read history. Before changing an existing entry, review
  calls the read-only `memory(action="history", ...)`, which scans JSONL locally,
  follows the newest producer backward so reused text cannot splice separate
  lineages, and returns only the related lineage under a fixed 8,000-character
  bound.
- A transaction ID means the operations were committed atomically; it does not
  make unrelated operations in that batch part of one another's history. Only
  operations explicitly marked `merge` share merge lineage.
- Prompts distinguish an actually observed incident or user correction from a
  merely preventive concern. They must not label an unobserved risk as a lesson
  or save generic safety precautions only because they seem important.
- Repository implementation designs, architecture notes, and fork-only behavior
  already covered by repository docs stay out of global memory; only a short
  pre-load trigger may remain when it is needed to select the right Skill.
- Both live tool-dispatch paths forward single-operation governance metadata;
  batch approval staging/replay retains the same metadata.
- When a memory may duplicate a Skill, review must inspect the actual Skill with
  `skill_view`. A short pre-load trigger may remain in memory even when the full
  procedure lives in the Skill.

`SOUL.md` and `SOUL_CHANGELOG.md` are intentionally outside this feature. This
workflow does not append either file to the memory-governance context or modify
them. The background agent's pre-existing inherited system identity remains
unchanged.

## Responsibility map

- `tools/memory_tool.py` owns the upstream-facing `MemoryStore`, character
  limits, file locks, atomic Store writes, public tool registration, and one
  `MemoryStore.transaction(...)` seam. The Fork mutation path reaches private
  Store mutation and rollback state only through that Host-owned transaction;
  history and background context use public read-only Store readers.
- `fork_features/memory_governance.py` owns mutation metadata policy,
  single/batch normalization, exact change tracing, audit/rollback orchestration,
  public Schema/Prompt policy, live review-context rendering, and the shared
  sequential/concurrent forwarding field list.
- `fork_features/memory_audit.py` owns profile-scoped JSONL baseline, append,
  parsing, merge-only lineage, threat scanning, field bounds, and the 8,000
  character result bound.
- `agent/background_review.py`, `agent/prompt_builder.py`,
  `agent/tool_executor.py`, and `agent/agent_runtime_helpers.py` keep only thin
  composition or forwarding calls. The Fork modules do not import
  `tools.memory_tool` or call Store private methods.

This split keeps Store/data ownership visible instead of moving the old
governance functions to another directory while retaining hidden private-method
coupling.

## Maintenance exposure evidence

At upstream SHA `4f22543509d1b91dc45bcb369447126c5eb14fb7` and Fork
baseline `b6519e453af5ee74bda5619981e9748d690e5d8f`, the maintenance
profile counted one path touch per non-merge commit from
`git log --no-merges --since=2026-05-01 --name-status --find-renames`:

- `tools/memory_tool.py`: 34 upstream touches / 1 Fork-only non-merge touch;
- `agent/background_review.py`: 45 / 1;
- `agent/prompt_builder.py`: 112 / 3.

These are path-exposure counts, not measured merge-conflict or Token savings.
The repository-local `docs/FORK_SYNC_HISTORY.jsonl` was absent. The external
decoupling ledger inspected for the profile contained 13 implementation records
but no sync or follow-up records, so sync-outcome coverage is missing. No
measured conflict hunks, resolution time, rework, defects, or Token evidence was
available.

## Merge protection

Preserve:

- the profile-scoped path (`get_hermes_home()/memories`);
- live MEMORY/USER disk-state injection in the background review's user message,
  not the cached system prompt, with no full-log injection;
- reason/evidence and typed deletion metadata through approval staging/replay;
- metadata forwarding in both sequential and concurrent live dispatch;
- target locking, rollback, and stale-snapshot protection when the audit record
  cannot be written;
- structured JSONL records with transaction IDs;
- Store-private mutation, rollback, file-lock operations remaining behind the
  public `MemoryStore.transaction(...)` seam, while read-only consumers use
  public Store readers;
- bounded, threat-scanned, related-only history lookup before existing-entry
  changes, while pure adds skip history;
- threat scanning and data encoding for live MEMORY/USER context;
- the existing runtime whitelist limited to memory and Skill tools.

Drop this fork behavior only when upstream provides equivalent autonomous,
auditable memory changes with historical reasons and typed capacity eviction.
Ask the user before replacing it with a mechanism that requires manual review or
adds SOUL files to background memory context.

## Verification

```bash
./venv/bin/python -m pytest -q -o 'addopts=' \
  tests/agent/test_prompt_builder.py \
  tests/agent/test_memory_write_bridge.py \
  tests/fork/test_memory_changelog_governance.py \
  tests/fork_features/test_memory_governance_boundary.py \
  tests/tools/test_memory_tool.py \
  tests/tools/test_memory_tool_schema.py \
  tests/tools/test_write_approval.py \
  tests/run_agent/test_run_agent.py::TestExecuteToolCalls \
  tests/run_agent/test_background_review_cache_parity.py \
  tests/run_agent/test_background_review_toolset_restriction.py \
  tests/test_background_review_list_shapes.py \
  tests/test_background_review_session_isolation.py
```
