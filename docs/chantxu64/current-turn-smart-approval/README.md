# Current-turn Smart Approval and compatible scanner routing

## Purpose

This fork keeps Smart Approval narrow and auditable while avoiding false manual approvals during ordinary software-development verification.

It combines four related guarantees:

1. authorization evidence comes only from the latest real user turn and subsequent completed Clarify exchanges;
2. directly launched custom scripts are reviewed through bounded entry-script evidence;
3. standard package-managed development tools are not misclassified as unreadable custom scripts;
4. user-visible approval explanations follow the current user's language.

It also validates the Tirith command-line protocol before trusting a same-named executable found on `PATH`.

## Approval context contract

Smart Approval receives only:

- the action about to run;
- the resolved execution directory;
- the latest normalized real user instruction;
- completed Clarify question/answer pairs after that instruction;
- bounded contents of directly launched entry scripts.

The context normalizer excludes compaction summaries, ToDo snapshots, background and recovery notifications, Skill bodies, model-switch notices, reply/thread metadata, Cron delivery guidance, and pre-run/context-job output.

This prevents runtime scaffolding from becoming authorization evidence and prevents concurrent tool calls from borrowing another request's context.

## Development tools versus custom scripts

The fork does not whitelist commands merely because their names contain `test`, `lint`, or `build`.

Instead:

- standard module invocations such as `python -m pytest` pass directly when Tirith and static dangerous-command checks find no risk;
- verified console entry points under a Python virtual environment's `bin` or `Scripts` directory are treated as package-managed tools rather than opaque custom scripts;
- source-script paths such as `.py`, `.sh`, `.js`, and similar entries remain reviewable even inside a virtual-environment-like directory;
- directly launched custom scripts are read with existing limits of four scripts and 32,000 bytes per script;
- missing, unreadable, oversized, or excess direct-script evidence fails closed to manual review;
- here-doc and stdin forms are inline command content, while a real script named before the redirection remains evidence.

This is a bounded entry-point review, not a whole-repository upload or recursive dependency analysis.

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

The reviewer is instructed to write `reason` in the latest real user's natural language. Chinese contexts also localize:

- risk and authorization summaries;
- denial and timeout safeguards;
- terminal smart-auto-approval notes.

Language detection uses the same request-local trusted approval context, not historical summaries or system scaffolding.

## Main implementation seams

- `agent/conversation_compression.py`
  - real-user classification and runtime-wrapper stripping
- `agent/tool_executor.py`
  - request-local Smart Approval context construction and binding
- `agent/agent_runtime_helpers.py`, `model_tools.py`
  - propagation across execution paths
- `tools/approval.py`
  - structured review contract, direct-script evidence, here-doc handling, and localized approval presentation
- `tools/tirith_security.py`
  - scanner-protocol validation and compatible-path selection
- `tools/terminal_tool.py`
  - localized approval notes
- `tools/code_execution_tool.py`
  - complete visible Python-source review path
- `tests/tools/test_smart_approval_context.py`
- `tests/tools/test_tirith_security.py`

## Non-goals

This feature does not:

- upload or recursively inspect the entire source tree;
- trust scripts based on filenames containing `test`;
- make destructive custom scripts safe merely because they are part of a test workflow;
- add per-command or per-case keyword exceptions;
- change the configured approval model, endpoint, fallback chain, or operator policy;
- alter Gateway lifecycle behavior.

## Merge protection

Preserve this fork behavior unless upstream provides an equivalent contract covering all of the following together:

- latest-real-turn and scoped Clarify authorization evidence;
- per-request isolation across concurrent tool calls;
- bounded direct-entry-script evidence with fail-closed gaps;
- here-doc/stdin distinction;
- package-managed virtual-environment console-entry handling without test-name whitelists;
- Tirith protocol validation before accepting a PATH candidate;
- language-aware human presentation while preserving structured enums.

If upstream implements a broader recursive analyzer or uses wider conversation history, compare behavior and privacy/cost boundaries before merging.

## Verification

Run at minimum:

```bash
python -m pytest -q -o 'addopts=' \
  tests/tools/test_smart_approval_context.py \
  tests/tools/test_smart_approval_policy.py \
  tests/tools/test_smart_approval_injection.py \
  tests/tools/test_execute_code_approval_cluster.py \
  tests/tools/test_terminal_tool.py \
  tests/tools/test_code_execution.py \
  tests/tools/test_code_execution_modes.py \
  tests/tools/test_tirith_security.py

python -m pytest -q -o 'addopts=' \
  tests/agent/test_context_compressor_zero_user_provenance.py \
  tests/agent/test_compression_concurrent_fork.py \
  tests/agent/test_context_compressor.py

python -m py_compile \
  tools/approval.py tools/tirith_security.py tools/terminal_tool.py \
  tests/tools/test_smart_approval_context.py tests/tools/test_tirith_security.py

./venv/bin/ruff check \
  tools/approval.py tools/tirith_security.py tools/terminal_tool.py \
  tests/tools/test_smart_approval_context.py tests/tools/test_tirith_security.py

git diff --check
```

2026-08-16 local validation:

- approval, terminal, code-execution, and Tirith suite: `206 passed`, `7 subtests passed`;
- conversation-compression/provenance suite: `193 passed`;
- Ruff, `py_compile`, and `git diff --check`: passed;
- a live local resolver probe skipped pipx `py-tirith` 1.0.5, selected the Hermes-managed Tirith 0.2.12 binary, and allowed an ordinary pytest scan;
- no paid model replay, configuration change, Gateway restart, commit, or push was performed.

## Runtime activation

These are source changes. A currently running Gateway keeps its previously imported code until it is restarted. Restart is a separate operational action and is not part of this feature's local verification.
