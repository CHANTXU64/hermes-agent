# Current-turn Smart Approval and compatible scanner routing

## Purpose

This fork keeps Smart Approval narrow and auditable while avoiding false manual approvals and denial-bypass retry loops.

It combines six related guarantees:

1. authorization evidence comes only from the latest real user turn and subsequent completed Clarify exchanges;
2. directly launched custom scripts and explicitly named local Python files provide best-effort source evidence without external dependency inspection; Git-tracked entries are identified without sending their source, while oversized untracked entries provide a bounded prefix;
3. standard package-managed development tools are not misclassified as unreadable custom scripts;
4. user-visible approval explanations follow Hermes' configured interface language;
5. a first Smart Review denial exposes one legitimate text-similar retry route; an existing legal bypass or a later `approve` remains effective, while a second `deny` can fall back to one-shot human approval;
6. a denial or timeout on that repeat one-shot card is final for the same or similar Terminal/`execute_code` action in that user turn, without changing ordinary approval behavior.

It also validates the Tirith command-line protocol before trusting a same-named executable found on `PATH`.

## Approval context contract

Smart Approval receives only:

- the action about to run;
- the resolved execution directory;
- the complete latest normalized real user instruction, without a character cap;
- all completed Clarify question/answer pairs after that instruction, without a pair-count cap;
- direct-entry metadata plus source for untracked entries, bounded to a prefix when oversized; Git-tracked entries are marked without sending their source.

The context normalizer excludes compaction summaries, ToDo snapshots, background and recovery notifications, Skill bodies, model-switch notices, reply/thread metadata, Cron delivery guidance, and pre-run/context-job output.

This prevents runtime scaffolding from becoming authorization evidence and prevents concurrent tool calls from borrowing another request's context.

## Development tools versus custom scripts

The fork does not whitelist commands merely because their names contain `test`, `lint`, or `build`.

Instead:

- standard module invocations such as `python -m pytest` pass directly when Tirith and static dangerous-command checks find no risk;
- verified console entry points under a Python virtual environment's `bin` or `Scripts` directory are treated as package-managed tools rather than opaque custom scripts;
- source-script paths such as `.py`, `.sh`, `.js`, and similar entries remain reviewable even inside a virtual-environment-like directory;
- all unique directly launched custom scripts and explicitly named local Python files are collected; duplicate paths are deduplicated without hiding later entries;
- a path recorded by its containing Git index is marked `skipped_git_tracked`, and its source reader is not called; this applies whether the repository is the current worktree or another local repository;
- an explicitly named path that Git does not track is read best-effort even when it is inside a Git worktree, outside the execution cwd, under a standard-library or installed-package path, or reached through a symlink;
- untracked source larger than 32,000 bytes is returned as a 32,000-byte prefix with status `truncated` instead of becoming empty `unreadable` evidence;
- missing, binary/NUL-containing, I/O-failed, Git-tracked/skipped, or truncated direct-script evidence remains visible as context but does not force denial or manual review by itself;
- `execute_code` code that passes a literal command to `hermes_tools.terminal` can expose the directly invoked Python script path; ordinary imports, module-name-only dynamic imports, and non-literal terminal commands are not expanded;
- here-doc and stdin forms are inline command content, while a real script named before the redirection remains evidence.

The reviewer judges the visible operation, current authorization, and actual operational consequences. This is bounded entry-point context, not a source-code supply-chain audit.

## Tirith compatibility contract

Multiple unrelated packages can install an executable named `tirith`. Existence on `PATH` is therefore not proof that the binary implements Hermes's scanner protocol.

Before accepting a default-name candidate, Hermes now runs:

```text
<path> check --help
```

A compatible candidate must exit successfully and advertise:

```text
--json
--shell
--non-interactive
```

Incompatible same-named executables are skipped. Resolution may then use the Hermes-managed binary under `~/.hermes/bin` or the existing installer path.

Explicit non-default configured paths retain the existing fail-closed behavior and are not silently replaced.

## Language-aware presentation

Structured machine fields remain English enums:

- `decision`: `approve`, `deny`, `escalate`;
- `risk_level`: `low`, `medium`, `high`, `critical`;
- `authorization`: `exact`, `sufficient`, `unclear`, `none`.

The reviewer receives Hermes' resolved interface-language code for `reason`.
Fixed smart-review presentation uses Chinese for `zh`/`zh-hant` and otherwise
retains its English fallback. Chinese interfaces localize:

- risk and authorization summaries;
- denial and timeout safeguards;
- terminal smart-auto-approval notes.

Language resolution reuses `agent.i18n.get_language()`: `HERMES_LANGUAGE` overrides
`display.language`, which falls back to English. Message text, historical summaries,
and system scaffolding are not used to guess the presentation language.

## Automatic Smart denial and repeat one-shot approval

This retry route begins only after Smart Review returns `deny` for a Terminal command or `execute_code` source:

1. The first denial returns the structured Smart Review reason and tells the main agent that, only if the operation is still necessary, it may submit the same or a textually similar action again in the same real user turn.
2. Retry candidates are limited to the same approval session, verified user turn, and tool kind (`shell` or `python`). Text is whitespace-normalized; exact normalized text matches immediately, otherwise a bounded first/last 8,192-character `SequenceMatcher(autojunk=False)` comparison uses a `0.86` threshold.
3. The second actual action follows the normal current approval path. YOLO/mode-off, an existing valid allowlist or session approval, and a current Smart Review `approve` may release it normally; a current `escalate` retains the ordinary manual-approval behavior.
4. Only when the current Smart Review still returns `deny` does text similarity consume the first-denial state and route the current second command or full Python source to a fresh one-shot human card. That card disables session/permanent approval.
5. A denial or timeout on this repeat one-shot card creates a same-turn latch for similar actions. Ordinary manual approval, a normal Smart `escalate`, and selected approval transports keep their prior behavior and do not create this repeat-specific latch.

This mechanism does not prove semantic equivalence, reuse prior authorization, or compare shell tokens, launchers, paths, targets, Python ASTs, here-doc structure, indentation, or effects. A false-positive similarity match can only produce an extra human card; it cannot execute an action automatically.

Both a real session identifier and a real turn identifier are required. State is in memory and is cleared with the session; it is intentionally not persisted across Gateway/process restart. A successful one-shot consumes the retry state and creates no reusable authorization. If no live approval callback exists, the route fails closed with `approval_unavailable`, creates no pending request, and restores the repeat state so a later retry can reach a subsequently registered callback.

Gateway restart/stop remains an independent deterministic hard block inside the running Gateway. Repeating it does not create an approval card and does not execute it. Computer Use, Cron, and cross-tool intent tracking are outside this feature.

## Main implementation seams

- `agent/conversation_compression.py`
  - real-user classification and runtime-wrapper stripping
- `agent/tool_executor.py`
  - thin adapter that injects the canonical real-user classifiers into the Fork context builder
- `agent/agent_runtime_helpers.py`, `model_tools.py`
  - propagation across execution paths; `model_tools.py` binds the request-local Fork context
- `fork_features/approval/policy.py`
  - the only public Fork Smart Approval facade: request-local context, context construction, structured reviewer access, direct-script evidence access, and same-turn retry state
- `fork_features/approval/script_evidence.py`
  - internal Policy implementation for direct-entry and explicit-path identification, including literal `terminal()` child commands; no import dependency traversal; Git-index tracking decides whether source is skipped, while oversized untracked source returns a bounded prefix
- `fork_features/approval/smart_review.py`
  - internal Policy implementation for the reviewer prompt, structured result parsing, and risk/authorization contract
- `fork_features/approval/retry_policy.py`
  - internal Policy implementation for same-turn Smart-denial retry state, text-only similarity policy, repeat-card denial latch, and approval descriptions
- `fork_features/approval/runtime.py`
  - concrete bounded script readers, structured auxiliary-client call, language selection and policy construction; no import of the approval gate; Terminal and `execute_code` share these readers directly
- `tools/approval.py`
  - gate-owned retry identity and shared lock are passed to the Fork runtime; deterministic floors, YOLO/mode/allowlists, Tirith warning keys, structured-verdict execution, manual approval transport, persistence, observability, and fail-closed stay here
- `tools/tirith_security.py`
  - scanner-protocol validation and compatible-path selection
- `tools/terminal_tool.py`
  - Terminal guard integration and the existing hard Gateway-lifecycle floor
- `tools/code_execution_tool.py`
  - complete visible Python-source review path
- `tests/fork_features/approval/test_smart_approval_context.py`
- `tests/fork_features/approval/test_policy_boundary.py`
- `tests/tools/test_denial_retry_escalation.py`
- `tests/fork_features/approval/`
- `tests/hermes_cli/test_gateway_restart_loop.py`
- `tests/tools/test_tirith_security.py`

## Maintenance evidence for this boundary

The responsibility boundary was measured at fixed refs:

- upstream: `4f22543509d1b91dc45bcb369447126c5eb14fb7`;
- Fork baseline: `63f75f3b6d8c7d54d411e88990b400743d474fc5`;
- history window: since `2026-05-01`;
- one touch per non-merge commit per current path, without historical alias collapse.

Reproduce one path with:

```bash
git log --since=2026-05-01 --no-merges --format='%H' \
  4f22543509d1b91dc45bcb369447126c5eb14fb7 -- tools/approval.py \
  | sort -u | wc -l
git log --since=2026-05-01 --no-merges --format='%H' \
  4f22543509d1b91dc45bcb369447126c5eb14fb7..63f75f3b6d8c7d54d411e88990b400743d474fc5 \
  -- tools/approval.py | sort -u | wc -l
```

The same method reports upstream/Fork-only touches of `125/2` for
`tools/approval.py`, `90/3` for `agent/tool_executor.py`, `191/7` for
`agent/agent_runtime_helpers.py`, `51/1` for `model_tools.py`, `112/1` for
`tools/terminal_tool.py`, `61/1` for `tools/code_execution_tool.py`, `12/1`
for `tools/tirith_security.py`, and `157/3` for
`agent/conversation_compression.py`.

`/Users/robot/Documents/Hermes/hermes-fork-decoupling-ledger.jsonl` contained
13 implementation records but no sync or follow-up records. Therefore no
measured conflict hunks, semantic decisions, resolution time, rework, defects,
or Token savings were available. Commit-touch counts show exposure only; they
do not prove that this extraction has already reduced merge effort or Token use.

## Non-goals

This feature does not:

- upload or recursively inspect the entire source tree;
- treat missing script source as proof that an operation is unsafe;
- trust scripts based on filenames containing `test`;
- make destructive custom scripts safe merely because they are part of a test workflow;
- add per-command or per-case keyword exceptions;
- change the configured approval model, endpoint, fallback chain, or operator policy;
- add cross-tool semantic same-result detection or change Computer Use approval behavior;
- persist denial/retry state across processes or Gateway restart;
- make Gateway restart/stop or persistent launchd registration approvable from the running Gateway.

## Merge protection

Preserve this fork behavior unless upstream provides an equivalent contract covering all of the following together:

- latest-real-turn and scoped Clarify authorization evidence;
- per-request isolation across concurrent tool calls;
- optional direct-entry and explicit-path evidence without external dependency traversal, including Git-index-based source skipping, untracked-path reading, and bounded oversized prefixes;
- here-doc/stdin distinction;
- package-managed virtual-environment console-entry handling without test-name whitelists;
- Tirith protocol validation before accepting a PATH candidate;
- language-aware human presentation while preserving structured enums;
- first-Smart-denial guidance, legal-release priority, second-denial text-similar one-shot fallback, and repeat-card-only same-turn denial/timeout latching;
- the unchanged hard block for Gateway lifecycle actions;
- Fork policy bodies under `fork_features/approval` with thin host integration.

If upstream implements a broader recursive analyzer or uses wider conversation history, compare behavior and privacy/cost boundaries before merging.

## Verification

Run at minimum:

```bash
python -m pytest -q -o 'addopts=' \
  tests/fork_features/approval \
  tests/fork_features/approval/test_smart_approval_context.py \
  tests/tools/test_smart_approval_policy.py \
  tests/tools/test_smart_approval_injection.py \
  tests/tools/test_denial_retry_escalation.py \
  tests/tools/test_denial_circuit_breaker.py \
  tests/tools/test_execute_code_approval_cluster.py \
  tests/tools/test_approval_deny_rules.py \
  tests/tools/test_approval.py \
  tests/tools/test_approval_interrupt.py \
  tests/tools/test_approval_mode_parity.py \
  tests/tools/test_request_tool_approval.py \
  tests/hermes_cli/test_gateway_restart_loop.py

python -m pytest -q -o 'addopts=' \
  tests/tools/test_terminal_tool.py \
  tests/tools/test_code_execution.py \
  tests/tools/test_code_execution_modes.py \
  tests/tools/test_tirith_security.py \
  tests/agent/test_i18n.py

python -m pytest -q -o 'addopts=' \
  tests/agent/test_context_compressor_zero_user_provenance.py \
  tests/agent/test_compression_concurrent_fork.py \
  tests/agent/test_context_compressor.py

python -m py_compile \
  tools/approval.py tools/tirith_security.py tools/terminal_tool.py \
  cron/lifecycle_guard.py \
  fork_features/approval/script_evidence.py \
  fork_features/approval/smart_review.py \
  fork_features/approval/retry_policy.py

./venv/bin/ruff check \
  tools/approval.py tools/tirith_security.py tools/terminal_tool.py \
  cron/lifecycle_guard.py fork_features/approval \
  tests/fork_features/approval tests/tools/test_denial_retry_escalation.py \
  tests/fork_features/approval/test_smart_approval_context.py \
  tests/tools/test_execute_code_approval_cluster.py \
  tests/hermes_cli/test_gateway_restart_loop.py

git diff --check
```

2026-08-16 local validation:

- approval, terminal, code-execution, and Tirith suite: `206 passed`, `7 subtests passed`;
- conversation-compression/provenance suite: `193 passed`;
- Ruff, `py_compile`, and `git diff --check`: passed;
- a live local resolver probe skipped pipx `py-tirith` 1.0.5, selected the Hermes-managed Tirith 0.2.12 binary, and allowed an ordinary pytest scan;
- no paid model replay, configuration change, Gateway restart, commit, or push was performed.

2026-08-24 post-correction local validation in the isolated worktree:

- the 11 tests that distinguish legal release from forced one-shot first failed against the overextended route, then passed after the route was withdrawn;
- focused Fork/retry/Terminal/`execute_code`/context regression: `113 passed`;
- broader approval/Gateway regression: `351 passed`, `2 failed`, `1 deselected`, with `7` third-party deprecation warnings. The two failures are existing order-dependent redaction tests and each passed in a fresh isolated run (`2 passed`); the deselected macOS `/tmp` alias test remains the unchanged platform-baseline case;
- adjacent Smart Approval policy, Terminal, code-execution, Tirith, approval-mode, and interface-language regression: `205 passed`, `7 subtests passed`;
- `py_compile`, Ruff, and `git diff --check` passed;
- all approval-model paths in these tests were mocked. No paid model replay, production configuration/provider/endpoint/fallback change, Gateway restart, live-main copy, commit, or push was performed.

2026-08-25 boundary correction validation:

- bounded script-evidence and Smart Approval context coverage: `59 passed`, including literal `hermes_tools.terminal` child-script extraction, explicit-path dynamic loading, and no ordinary-import expansion;
- adjacent Smart Approval / `execute_code` wiring coverage: `52 passed`;
- Terminal, code-execution, Tirith, and interface-language coverage: `167 passed`, `7 subtests passed`;
- approval-mode parity in an isolated process: `7 passed`; the standalone approval file remained `95 passed`, `1 failed` on the unchanged macOS `/tmp` verification-artifact baseline;
- Ruff, `py_compile`, and `git diff --check` passed. No model replay, configuration change, Gateway restart, commit, or push was performed.

2026-08-25 issue 1/2/3 and I1/I2 repair validation:

- bounded script-evidence and Smart Approval context coverage: `127 passed`, including current-worktree versus other-Git-project boundaries, recognized/unrecognized task temporary roots, protected-source and outside-root symlinks, interpreter-option handling, literal nested `hermes_tools.terminal` evidence, `execute_code` approval wiring, and Terminal integration;
- Ruff, `py_compile`, and `git diff --check` passed. This was a local targeted validation only; no model replay, configuration change, Gateway restart, commit, or push was performed.

2026-08-30 Git-tracking, oversized-prefix, and complete-authorization-context correction:

- focused direct-script and reviewer/context tests: `93 passed`;
- Fork Smart Approval, policy, injection, `execute_code`, retry, and denial-latch regression: `179 passed`;
- adjacent Terminal, code-execution, Tirith, approval-mode, interface-language, and Cron-session regression: `280 passed`, `1 deselected`, `7 subtests passed`; the deselection is the unchanged macOS `/tmp` verification-artifact baseline, which still fails when run;
- before local Git tracking, a live collector probe against `/Users/robot/.hermes/scripts/nc_report.py` from the Ontology cwd returned `status=truncated` with exactly `32,000` content bytes instead of empty unreadable evidence; after a local scripts repository tracked and committed only `nc_report.py`, the same probe returned `skipped_git_tracked` with zero source bytes;
- two live `openai-codex / gpt-5.6-luna` review-only probes used missing script source: a visible read-only `/tmp` diagnostic was `approve/low/sufficient`, while `--delete-all /Users/robot/Documents` under an explicit no-delete instruction was `deny/critical/none`; neither command was executed;
- `py_compile`, Ruff, and `git diff --check` passed; no configuration change or Gateway restart was performed, and the Hermes Agent Fork remains uncommitted. The only commit is the local scripts-repository snapshot of `nc_report.py`; nothing was pushed.

2026-08-31 Fork Policy boundary validation:

- Policy boundary and focused Host integration: `60 passed`; the dedicated Policy boundary file was `4 passed`;
- full approval/Gateway command: `430 passed`, `9 failed`; in fresh processes the seven approval-mode cases were `7 passed` and the two redaction cases were `2 passed`, while the unchanged macOS `/tmp` alias case remained `1 failed`;
- Terminal, code-execution, Tirith, and interface-language coverage: `167 passed`, `7 subtests passed`;
- conversation-compression and real-user provenance coverage: `197 passed`;
- the reviewer, script-evidence, and retry-policy source files were byte-identical to baseline `63f75f3b6d8c7d54d411e88990b400743d474fc5`; six fixed context samples matched the baseline builder exactly;
- Ruff, `py_compile`, and `git diff --check` passed. No paid model replay, configuration change, Gateway restart, commit, or push was performed.

## Runtime activation

These are source changes. A currently running Gateway keeps its previously imported code until it is restarted. Restart is a separate operational action and is not part of this feature's local verification.
