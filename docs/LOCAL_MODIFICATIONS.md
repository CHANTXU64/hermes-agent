# Local Modifications — Hermes Agent Fork

Purpose: track deviations from upstream `NousResearch/hermes-agent` so future
upstream merges do not accidentally delete active fork behavior.

Repository:

- Fork: https://github.com/CHANTXU64/hermes-agent
- Upstream: https://github.com/NousResearch/hermes-agent

This file is an audit and merge guide. It is not a blind rule to keep every old
fork change forever. Historical entries marked as reverted must not be revived
unless the user explicitly asks for them.

## Merge rules for AI agents

Before resolving conflicts, read this file and verify the current code with
`git diff`, `git log`, and direct file inspection.

Rules:

- Preserve active fork-only behavior listed under **Active modifications**.
- Do not preserve historical reverted behavior unless explicitly requested.
- In conflicts, keep upstream additions unless they break an active fork feature.
- Never use `-X ours` or `-X theirs` as a substitute for reading conflicts.
- If a file is listed here but no longer exists in code, treat this document as
  audit evidence, not proof that the feature is still active.
- `docs/LOCAL_MODIFICATIONS.md` is the correct path of this document.

Validation after a merge:

- Run focused tests for touched fork features where feasible.
- Compare failures against upstream baseline before blaming fork code.
- Use `fork_failures - upstream_failures` for CI triage.
- Attribute failures with `git diff`, `git blame`, and `git log` before fixing.

## Active modifications

### 1. Hindsight Chinese / Unicode support

Date: 2026-04-20

Commit: `7428b0da`

Files:

- `plugins/memory/hindsight/__init__.py`
- `.gitignore`

What changed:

- `json.dumps` in the Hindsight memory plugin uses `ensure_ascii=False`.
- Chinese and other Unicode text are stored/read as real characters instead of
  escaped `\uXXXX` sequences.
- `.gitignore` gained local development ignores.

Why it matters:

- The user relies on Chinese memory content being readable and not escaped.
- Future merges must not silently restore ASCII-escaped Hindsight payloads.

Merge protection:

- If this file conflicts, preserve `ensure_ascii=False` unless upstream has an
  equivalent Unicode-preserving implementation.
- Verify by inspecting the actual `json.dumps` call, not by keyword guessing.

Upstream status: fork-only.

### 4. Safe command rewrite for terminal tool

Date: 2026-04-21

Commit: `5513a9b9`

Files:

- `tools/safe_cmd_rewrite.py`
- `tools/terminal_tool.py`
- `tests/fork/test_safe_cmd_rewrite.py`
- `pyproject.toml`

What changed:

- Terminal execution rewrites destructive local shell commands into safer
  alternatives:
  - `rm ...` becomes `trash ...`
  - `mv ...` becomes `gmv -b ...`
  - `cp ...` becomes `gcp -b ...`
- SSH terminal backend and explicit `ssh host ...` remote commands are also
  rewritten with the same safety contract as local terminal execution:
  `rm` becomes `trash`, `mv` becomes `gmv -b`, and `cp` becomes `gcp -b`.
- `docker exec ... <file-op>` inside SSH/remote commands is rewritten for file
  operations, while non-exec Docker subcommands such as `docker rm`,
  `docker rmi`, and `docker cp` are intentionally not rewritten.
- The implementation uses `bashlex` AST parsing when available.
- It handles common shell structures, wrappers, env prefixes, and option
  separators.
- It intentionally ignores non-target cases such as `git rm`, quoted strings,
  comments, `find -delete`, `rsync --delete`, and words containing `rm`.
- Sandbox backends skip rewriting.

Why it matters:

- This is a user safety feature. A merge must not remove it accidentally.
- The behavior lives at the terminal tool execution layer, not in prompts.

Merge protection:

- Preserve the import and call path from `tools/terminal_tool.py` into
  `tools/safe_cmd_rewrite.py`.
- Preserve `bashlex` in `pyproject.toml` unless replaced by an equivalent parser.
- Run `tests/fork/test_safe_cmd_rewrite.py` after resolving conflicts touching
  terminal execution.

Upstream status: fork-only.

### 5. Local modifications document

Date: 2026-04-21

Commit: `f75fe530`

Files:

- `docs/LOCAL_MODIFICATIONS.md`

What changed:

- Added this document to record fork deviations from upstream.

Why it matters:

- Future merge agents need a concise map of which fork behaviors are active,
  which are historical, and which must not be deleted during conflict handling.

Merge protection:

- Keep this file at `docs/LOCAL_MODIFICATIONS.md`.
- Do not report it as missing by checking the repository root.
- Update it whenever fork-only code behavior changes.

Upstream status: fork-only documentation.

### 6. Disable newly bundled skills by default when configured

Date: 2026-05-09

Files:

- `tools/skills_sync.py`
- `hermes_cli/config.py`
- `hermes_cli/main.py`
- `hermes_cli/update_cmd.py`
- `tests/tools/test_skills_sync.py`
- `docs/LOCAL_MODIFICATIONS.md`

What changed:

- Added `skills.auto_enable_new_bundled` config with upstream-compatible default
  `true`.
- When set to `false`, newly discovered bundled skills are still copied into
  `~/.hermes/skills/` and recorded in `.bundled_manifest`, but their names are
  appended to `skills.disabled` during that first sync.
- Existing bundled skills, updated bundled skills, user-modified bundled skills,
  user-deleted bundled skills, hub-installed skills, and user-created skills are
  left alone.
- `hermes update` output reports when new bundled skills were disabled by this
  config.

Why it matters:

- The user does not want Hermes updates to silently enable newly shipped skills
  such as Kanban skills.
- Copying while disabling preserves discoverability and manifest tracking without
  injecting new instructions into normal skill discovery.

Merge protection:

- Preserve the default `true` behavior for upstream compatibility.
- Preserve the `false` behavior that only disables skills in `result["copied"]`;
  do not disable all bundled skills or re-disable skills the user already chose
  to enable.
- Run `tests/tools/test_skills_sync.py` after conflicts touching skill sync or
  skill config behavior.

Upstream status: fork-only.

### 8. Hindsight synchronous cache-miss recall

Date: 2026-05-22; upstream merge boundary reconfirmed 2026-08-08

Files:

- `plugins/memory/hindsight/__init__.py`
- `tests/plugins/memory/test_hindsight_provider.py`
- `tests/agent/test_memory_session_switch.py`
- `docs/LOCAL_MODIFICATIONS.md`

What changed:

- Hindsight auto-recall has a bounded synchronous fallback when no carried prior
  snapshot exists, so the first user turn in a new session or after compression
  can receive `<memory-context>` immediately.
- Added `recall_sync_on_cache_miss` and `recall_sync_timeout_seconds` provider
  settings. Defaults: enabled, 5 seconds.
- Current-turn recall snapshots are guarded by a generation counter so a late
  result from an older turn/session cannot overwrite newer recall context.
- Shared recall/reflect parameter handling lives in a single helper used by the
  synchronous current-turn fallback and P5-generated recall. Hindsight's
  post-turn `queue_prefetch()` hook is intentionally a no-op.

Why it matters:

- The user expects `auto_recall=true` to include relevant Hindsight memory on the
  first turn of fresh sessions and compression-created continuation sessions.
- Compression/session switches must still clear stale recall, while allowing the
  next current query to recall safely.

Merge protection:

- Preserve generation checks when refactoring Hindsight prefetch; clearing
  `_prefetch_result` alone does not stop a timed-out or overlapping older
  current-turn recall from carrying stale context after a newer turn or session
  lifecycle event.
- Preserve a short timeout for synchronous fallback; do not reuse the general
  Hindsight API timeout for first-turn recall.
- Preserve tests covering cache-miss sync recall, tools/auto_recall guards,
  reflect mode, empty P5 recall snapshots, and public-path late-turn generation
  discard.
- Upstream's post-turn queued-prefetch retain-drain feature is not applicable
  while this provider intentionally keeps `queue_prefetch()` as a no-op. Do not
  expose its settings or retain-operation polling as if that path were active.

Feature docs: `docs/chantxu64/hindsight-sync-cache-miss-recall/README.md`

Upstream status: fork-only.

### 9. Hindsight P5 recall preprocessor

Date: 2026-07-17; external-prefetch timeout compatibility fix 2026-07-19; configured model fallback 2026-08-09

Files:

- `agent/memory_provider.py`
- `agent/memory_manager.py`
- `agent/turn_context.py`
- `agent/codex_runtime.py`
- `agent/auxiliary_client.py`
- `hermes_cli/plugins.py`
- `plugins/memory/__init__.py`
- `plugins/memory/hindsight/recall_preprocessor.py`
- `plugins/memory/hindsight/__init__.py`
- `tests/fork/test_hindsight_recall_preprocessor.py`
- `tests/agent/test_memory_provider.py`
- `tests/fork/test_hindsight_provider_regressions.py`
- `tests/hermes_cli/test_plugin_auxiliary_tasks.py`
- `tests/run_agent/test_run_agent_codex_responses.py`
- `tests/agent/test_auxiliary_client.py`
- `docs/chantxu64/hindsight-p5-recall-preprocessor/README.md`
- `docs/LOCAL_MODIFICATIONS.md`

What changed:

- Hindsight carries the actual query and ordered result texts used by the
  current turn as a structured snapshot alongside the formatted cache string.
  Its generic post-turn `queue_prefetch()` hook is a no-op, so the completed
  turn's raw user text never starts another recall.
- The current user message, latest completed non-tool assistant response, and
  previous real recall are evaluated by the frozen P5 prompt through the
  standard `auxiliary.hindsight_recall_preprocessor` task. Its configurable
  provider/model/timeout defaults remain `openai-codex / gpt-5.6-luna / 30s`.
  A primary request, response-model validation, or strict-output parse failure
  tries only this task's configured `fallback_chain`; every usable entry must
  name an explicit provider and model, with dynamic, main-chat, and bare custom
  routes rejected before generic resolution. Generic provider discovery and the
  main chat model remain disabled. A fallback on another provider does not
  inherit the primary route's provider-specific `extra_body`; its own positive
  finite timeout is used when present, an omitted timeout inherits the primary
  timeout, and an invalid timeout rejects the fallback chain before resolution.
  This task alone opts into OpenAI Priority Processing through
  `extra_body.service_tier: priority`; the Codex auxiliary adapter projects that
  explicit task-local value to the Responses API's top-level `service_tier`
  field. Plugin-default `extra_body` remains bound to its default provider:
  selecting a different primary provider without an explicit `extra_body` drops
  the inherited request-body defaults, while an explicit user `extra_body` still
  wins.
- Dynamic `main`/`auto` routes are rejected. Bare `custom` is rejected unless
  this task has its own `base_url`, preventing fallback through global custom or
  unrelated API-key providers. Non-Codex provider-reported `response.model`
  values must match the configured model. Reserved canonical equivalents under
  `custom:*` (empty, `auto`, `main`, or `custom` suffixes) follow the same route
  rejection rules.
- The active memory-plugin loader now exposes auxiliary tasks declared by a
  memory provider to Hermes' standard model picker and dashboard registry; the
  bridge is generic and does not special-case Hindsight in the CLI.
- Strict output parsing accepts only `drop_old_refs` plus a one-line string or
  null `new_query`. A non-null query preserves un-dropped old results, appends
  one new read-only recall, and carries that actual merged snapshot to the next
  P5 decision. A null query skips only the new recall: un-dropped old results
  remain injected and carried. Dropping every old ref with a null query clears
  the chain.
- Query generation keeps or omits details according to whether they could have
  existed before the current session and whether they improve retrieval of
  useful history, not according to a field-type whitelist. A fresh commit hash
  or the complete path/name of a just-generated file is omitted and generalized
  to the related historical target; an explicitly historical identifier is not
  removed merely because it has the same field type.
- `MemoryManager` forwards the previous assistant message only to providers
  whose `prefetch` signature opts in, preserving legacy provider compatibility.
- Turn setup also checks the duck-typed manager's `prefetch_all` signature before
  passing the new keyword, preserving older manager substitutes and their memory
  context instead of silently dropping it on `TypeError`.
- The Codex stream consumer captures the terminal response's provider-reported
  model separately from the requested-model compatibility field; P5 validates
  the former before parsing output.
- Preprocessor or generated-recall failures conservatively restore the full old
  cache. Generation, session-switch, and rewind guards clear and protect both
  cached representations; a delayed old-turn synchronous result cannot
  repopulate a new or rewound session.
- External memory providers may declare a complete synchronous prefetch budget.
  Providers without a declaration retain upstream's generic 8-second fail-open
  guard. Hindsight declares the sum of its bounded stages: the configured P5
  primary timeout, the largest valid timeout of any configured fallback model,
  up to two sequential `recall_sync_timeout_seconds` windows, and a 1-second
  outer-guard scheduling margin. A fallback entry without its own timeout uses
  the primary P5 timeout. The second recall covers the branch where a
  P5-generated query fails with no old results and the provider retries the
  current query. With the current 30-second primary, 30-second fallback, and
  10-second recall settings the outer guard is 81 seconds, so it no longer
  truncates the fallback model or either recall stage.
- If the outer guard still times out, `MemoryManager` invokes the provider's
  non-blocking timeout hook. Hindsight invalidates only the abandoned
  turn/generation, so its late result cannot become a future carried snapshot.
  A stale-session prefetch is rejected before consuming current-session state.

Why it matters:

- Short continuations such as “继续” and “修吧” can inherit a specific target
  from the previous assistant analysis without sending those phrases or the
  full assistant response directly to Hindsight.
- The user accepts conservative side-topic retention, but does not accept a
  hard coverage gate that suppresses memory needed for the next answer or a
  post-turn duplicate recall using the raw user message.

Merge protection:

- Preserve P5 prompt SHA-256
  `b9b182478b41ab593398bb1649b8a318ab7f59464cd4abe5681a7add6481106f`
  unless the user explicitly approves and evaluates a successor.
- Preserve the explicit configurable auxiliary task, its evaluated Luna/30-second
  defaults, its task-local configured fallback chain, Codex provider-reported
  terminal-model validation, non-Codex `response.model` validation, and strict
  schema validation. Do not replace the task-local chain with generic provider
  discovery or a main-chat-model fallback. Continue rejecting `auto`, `main`,
  bare `custom`, and their reserved `custom:*` canonical equivalents. Do not
  treat the Codex adapter's requested-model `.model` compatibility field as
  independent backend-model evidence.
- Keep query/result snapshots under the same generation, session-switch, and
  rewind lifecycle as `_prefetch_result`; do not preserve one cache
  representation without the other. Preserve the no-op post-turn hook and carry
  only the query/results actually used by the current turn.
- Preserve fail-open restoration of old recall and the tools/auto_recall/
  shutdown guards.
- Preserve provider-specific prefetch budgeting. Do not replace the generic
  external-provider 8-second guard with a Hindsight-specific global constant,
  and do not let that generic guard silently override P5/recall stage timeouts.
- Do not reintroduce rejected P6 behavior that forces `new_query=null` merely
  because old results appear to cover the target.
- Run the focused command documented in the feature README after conflicts
  touching memory prefetch, turn context, Hindsight, or auxiliary routing.

Feature docs: `docs/chantxu64/hindsight-p5-recall-preprocessor/README.md`

Upstream status: fork-only.

## Historical / reverted modifications

### 3. MLX Whisper local STT provider

Status: historical / reverted per user decision

Date: 2026-04-20 to 2026-07-09

Historical commit: `ae8c0acd`

Historical files:

- `tools/transcription_tools.py`
- `agent/transcription_registry.py`
- `tests/fork/test_mlx_whisper_stt.py`

Historical behavior:

- Added `mlx_whisper` as a first-class local STT provider on macOS / Apple Silicon.
- Added model aliases such as `tiny`, `base`, `small`, `medium`, `large-v3`, and `turbo`.
- Auto-detection could choose MLX Whisper on Darwin when `faster-whisper` was not available.

Current status:

- On 2026-07-09 the user explicitly decided: "不要保留MLX Whisper了，反正现在我也不用了，这两个都以上游为准吧"。
- The fork now follows upstream STT providers for this area.
- MLX Whisper code, tests, registry entries, auto-detect branches, and provider docs are historical only.

Merge protection:

- Do not revive `_HAS_MLX_WHISPER`, `MLX_MODEL_ALIASES`, `_normalize_mlx_model`, `_transcribe_mlx_whisper`, or `stt.provider: mlx_whisper` unless the user explicitly requests it again.
- Future STT merges should preserve upstream behavior plus the active fork `custom_api` provider, not the old MLX Whisper provider.

Upstream status: reverted per user decision.

### 2. MoA custom provider support

Status: abandoned / superseded by upstream MoA architecture

Date: 2026-04-20 to 2026-06-29

Historical commits:

- `5c5ffe04` — provider-agnostic MoA adaptation
- `e60e548b` — tests for the provider-agnostic architecture
- `a0fc0fa0` — custom endpoint 401 authentication fix

Historical files:

- `tools/mixture_of_agents_tool.py` — removed during the 2026-06-29 upstream sync
- `tests/tools/test_mixture_of_agents_tool.py` — removed during the 2026-06-29 upstream sync
- `hermes_cli/config.py`
- `hermes_cli/runtime_provider.py`

Current status:

- Upstream replaced the old MoA model tool with the official MoA virtual-provider
  architecture: presets, model picker integration, `agent/moa_loop.py`,
  `hermes_cli/moa_config.py`, `hermes_cli/moa_cmd.py`, and related tests/docs.
- On 2026-06-29 the user explicitly decided: "MoA 的以官方为准吧，放弃我们自己的Fork修改".
- The old fork `mixture_of_agents_tool` and its tests must not be resurrected as
  active fork behavior.

Merge protection:

- Future upstream syncs should keep the official MoA architecture as canonical.
- If custom-provider MoA behavior breaks again, fix it in the official MoA
  virtual-provider / runtime-provider path, not by reviving
  `tools/mixture_of_agents_tool.py`.
- Treat mentions of the old MoA tool as historical evidence only.

Upstream status: superseded by upstream official MoA.

### 8. Review prompt / `skill_manage` config overrides

Date: 2026-04-22 to 2026-05-09

Commits:

- `907e6bd6` — initial `skills.skill_review_prompt` configurability
- `afc0f3d1` — documented entry 10
- `13446fdd` — memory/combined prompts and `skill_manage_description`
- `78607d74` — fallback fix for bare-object tests

Files:

- `run_agent.py`
- `tools/skill_manager_tool.py`
- `tests/run_agent/test_background_review.py`
- `docs/LOCAL_MODIFICATIONS.md`

What changed historically:

- Background review prompts could be overridden from `config.yaml`:
  - `skills.skill_review_prompt`
  - `skills.memory_review_prompt`
  - `skills.combined_review_prompt`
- The `skill_manage` tool description could be overridden from:
  - `skills.skill_manage_description`

Current status:

- Reverted / disabled in code.
- Background review now uses the built-in class prompt constants directly.
- `skill_manage` now uses its built-in schema description directly.
- Config keys above may exist in user config but should be ignored by code.

Why this matters:

- The config-driven prompt override was not useful in practice and made behavior
  harder to reason about.
- Future merges must not preserve or revive this feature just because older
  commits and docs mention it.

Merge protection:

- Do not reintroduce `_skill_review_prompt`, `_memory_review_prompt`, or
  `_combined_review_prompt` instance attributes loaded from config.
- Do not reintroduce `_load_skill_manage_description()` or config-backed
  `skills.skill_manage_description` loading.
- Preserve the regression test that proves configured instance prompts are not
  used by background review.

Upstream status: reverted fork-only experiment.

## Active modifications (continued)

### 9. Hindsight manual full-session retain

Status: active

Date: 2026-05-21

Files:

- `agent/memory_manager.py`
- `agent/memory_provider.py`
- `agent/conversation_loop.py`
- `agent/turn_context.py`
- `agent/chat_completion_helpers.py`
- `agent/turn_finalizer.py`
- `run_agent.py`
- `plugins/memory/hindsight/__init__.py`
- `toolsets.py`
- `tests/test_toolsets.py`
- `website/docs/reference/toolsets-reference.md`
- `hermes_state.py`
- `tests/plugins/memory/test_hindsight_provider.py`
- `tests/run_agent/test_run_agent.py`
- `tests/test_hermes_state.py`
- `tests/agent/test_memory_session_switch.py`
- `hermes_cli/commands.py`
- `tests/hermes_cli/test_commands.py`
- `cli.py`
- `tests/fork/test_cli_retain_command.py`
- `gateway/run.py`
- `gateway/slash_commands.py`
- `tests/fork/test_gateway_retain_command.py`
- `tests/fork/test_hindsight_provider_regressions.py`
- `tests/gateway/test_session_model_reset.py`
- `tests/gateway/test_session_race_guard.py`
- `tests/cli/test_cli_new_session.py`
- `tests/fork/test_hindsight_retain_document_flow.py`
- `tests/gateway/test_pending_event_none.py`
- `tests/gateway/test_message_timestamps.py`
- `tests/gateway/test_undo_rewind_session.py`
- `tui_gateway/server.py`
- `tests/tui_gateway/test_retain_on_new.py`
- `tests/tui_gateway/test_undo_command.py`
- `ui-tui/src/app/slash/commands/core.ts`
- `ui-tui/src/app/interfaces.ts`
- `ui-tui/src/app/uiStore.ts`
- `ui-tui/src/app/submissionCore.ts`
- `ui-tui/src/app/useMainApp.ts`
- `ui-tui/src/app/useSubmission.ts`
- `ui-tui/src/__tests__/createSlashHandler.test.ts`
- `ui-tui/src/__tests__/submissionCore.test.ts`
- `docs/chantxu64/hindsight-manual-retain.md`

Summary:

- Adds a user-triggered `/retain` command that records a full Hindsight session document from Hindsight's provider-owned retain-turn SQLite store while preserving fork-specific Hindsight document lineage.

What changed:

- Added `hindsight_retain_session` / `/retain` for user-triggered Hindsight session retain.
- Hindsight `sync_turn()` now persists the exact same turn JSON used by automatic retain into a separate SQLite file: `$HERMES_HOME/hindsight/retain_turns.sqlite3`.
- When `MemoryManager` supplies the completed OpenAI-style `messages` transcript, Hindsight `sync_turn(..., messages=...)` rebuilds clean retain turns from that transcript before persisting. This preserves an earlier real user message when a gateway turn is interrupted by a later user correction before the final assistant response, while still filtering tool outputs, assistant tool-call shells, `[Recent Summary ...]`, `Operation interrupted:` notices, empty assistant messages, and intermediate assistant drafts.
- 2026-08-11 pre-compress retain snapshot: Hindsight implements `on_pre_compress(messages)` and snapshots retainable turns into `$HERMES_HOME/hindsight/retain_turns.sqlite3` before context compression discards live transcript. Long tool work that never receives a final assistant answer can still leave the original real first user request in the provider-owned ledger; a later compressed window that only contains a continuation such as `继续` plus the final answer must merge onto that snapshot so the Hindsight Document starts at the original request, not the continuation. Orphan-user completion during replay/replace prefers matching `_hermes_source_occurrence_id` when both sides expose one (unequal ids never merge), and otherwise allows same-content completion when the incoming turn adds an assistant—even if compression rehydrated a later user timestamp. Pre-compress snapshots are local-ledger first: when nothing else is pending auto-retain, they do not become remote-flush-eligible on their own, and `on_session_switch` strips trailing user-only orphans from flush-on-switch so an incomplete Document is not published before the final answer.
- 2026-08-11 retain_on_new StateDB parent trap: `_load_persisted_retain_turns` prefers a session's own active document when all of its active rows already live under `retain_document_id == session_id`. This prevents `/new` from treating the previous chat's StateDB `parent_session_id` as retain lineage and re-submitting the wrong parent Document while the new session's local turns stay unsubmitted.
- 2026-07-10 clean retain-turn filter expansion / 2026-07-16 async-result correction / 2026-07-18 Recent-Summary boundary correction: `sync_turn()` / `_build_turn_group_from_conversation_messages()` drop or strip synthetic runtime injections before writing `retain_turns.sqlite3`. Covered markers: `[Recent Summary ...]`, `[Session Arc Summary ...]`, `[Durable Summary ...]`, `[Depth-N Summary ...]`, `[Current user objective preserved from compacted history]`, `[Your active task list was preserved across context compression]`, `[Externalized payload: ...]`, `[ASYNC DELEGATION BATCH COMPLETE ...]`, `[ASYNC DELEGATION COMPLETE ...]`, `[OUT-OF-BAND USER MESSAGE ...]`, and the leading `[Note: model was just switched ... Adjust your self-identification accordingly.]` runtime note. For the model-switch note, only the exact leading marker is removed from plain user text or the first text part of a serialized multimodal message; the following real user text and all non-text parts are preserved. Async completion payloads are omitted while the last eligible user-visible assistant response is retained as an assistant-only event; it is not paired with a fabricated user utterance or a prior orphan user. A transcript window consisting only of a synthetic, exact LCM header `[Recent Summary (d0...)]` user row followed by the actual final assistant answer follows the same assistant-only rule; user-authored lookalikes such as `[Recent Summary request]` remain normal conversation content, while standalone Session-Arc/Durable/Depth summary turns remain fully dropped. Other pure-noise messages are skipped, and mixed messages keep only residual real user text. Scalar `sync_turn(user, assistant)` follows the same async/Recent-Summary rule. Assistant-side LCM/interrupt noise is dropped. Manual `/retain` re-sanitizes historical `turn_json` rows on load (clean-on-retain), stripping historical async payloads while preserving their visible assistant response. Filtering is marker-based, not business-topic-based.
- 2026-07-20 tool-budget notice correction and review follow-ups: `handle_max_iterations()` adds `You've reached the maximum number of tool-calling iterations allowed. ... without calling any more tools.` only to the local API request copy; it no longer mutates the durable conversation `messages`. Initial success, retry success, empty-summary fallback, and API-error fallback now share one tail-aware close step: a plain intermediate Assistant tail is replaced and made eligible for re-persistence, while other tails receive the exact visible final Assistant response. Hindsight keeps role-specific backward cleanup for historical rows: an exact User notice is dropped, and recognized leading runtime sequences (including adjacent budget and model-switch notices in either order) are iteratively stripped while residual real user text remains. Same-line and multiline user quotations without that structural evidence, plus legitimate Assistant content equal to the notice, remain ordinary conversation content. Historical exact notices still preserve their following visible Assistant response, paired with an open real user request or retained assistant-only after an already-completed turn as appropriate.
- Before appending transcript-derived turns, `sync_turn()` mirrors active persisted rows from `$HERMES_HOME/hindsight/retain_turns.sqlite3` into the in-memory turn buffer. This prevents provider restart/compression replay from duplicating already-persisted active turns when the next sync receives the full transcript plus a new tail turn.
- If a rebuilt transcript is a full replay or a compressed tail window anchored to persisted turns but contains newly retainable async assistant-only events, replay reconciliation merges only the missing events into the full active history in transcript order, soft-deactivates the old local rows, writes one corrected active sequence while preserving matched root/child session ownership, and forces the next automatic retain (including a below-threshold buffer flushed by session switch) to use `replace` rather than `append`. Full-turn canonical equality remains the preferred anchor. When restart/lifecycle replay has regrouped a later Assistant-only event or rewritten one user media representation, reconciliation may also use an exact shared message identity or a leading user role/content anchor that is unique across the remaining incoming and persisted windows; the already-persisted completed answer stays authoritative and any genuinely new Assistant suffix is retained as its own event. A persisted orphan User closed by the replayed Assistant is a real same-length content replacement: the local row ownership is preserved, the completed turn is written, and automatic retain immediately submits `replace`. A persisted Assistant-only row can likewise be enriched when a later replay begins with a real User and contains the exact same Assistant identity: reconciliation replaces the orphan with the complete User/Assistant turn, preserves the original row owner, and remains idempotent on repeated replay. Transcript messages without a source timestamp remain time-unknown instead of receiving a fresh `now()`; only the final Assistant that completes the current `sync_turn()` may receive the current time. Therefore a historical Assistant cannot become a timestamp-new singleton merely because a replay changed its user/media representation. Between stable persisted anchors, a time-unknown Assistant-only event may still be recovered structurally; outside a bounded anchor gap it is not accepted as timestamp-new evidence. Stable, complete timestamps that prove identical text occurred later preserve that real recurrence instead of matching it to an older canonical anchor. If media rewriting plus Assistant regrouping leaves no safe anchor, only messages whose source timestamps are strictly newer than every persisted event are appended; the overlapping representation itself remains excluded. Legacy rows with an empty `retain_document_id` are soft-deactivated through the resolved lineage fallback.
- 2026-07-27 mixed timestamp replay correction: retained timestamps are serialized as naive local wall-clock strings, so `_retain_message_timestamp()` interprets naive ISO/datetime inputs in the host local zone rather than attaching UTC. This keeps normalization idempotent and keeps numeric Unix timestamps, aware ISO values, and already-normalized retained timestamps in the same local comparison domain; otherwise a non-UTC host can move only the Assistant side by its UTC offset and replay can discard the paired real User at the strict-new cutoff.
- 2026-07-31 source-occurrence and submission-evidence correction: Gateway initial messages and queued follow-ups propagate their own clean user text, platform timestamp, and platform message id through the Agent current turn and StateDB `platform_message_id`. Hindsight persists a canonical `_hermes_source_occurrence_id` on retained user messages, so restart/compression replay of the same platform event remains idempotent even when timestamps change, while identical text from distinct platform occurrences remains distinct. Native multimodal and Gateway-simplified image representations collapse to one occurrence without retaining base64 data or local image paths. Every document-bearing automatic flush, session-switch flush, transcript compatibility retain, and persisted/manual retain writes the exact outbound `content_json` to `hindsight_retain_submissions` before queueing and closes it as succeeded or failed after the API call; interrupted submissions remain visibly queued.
- The profile-local daily monitor now binds a remote Document only to a semantically exact generation reconstructed from successful submission-ledger rows (`replace` resets the generation; `append` extends it; the provider's empty legacy mode is a full-session overwrite). Unknown modes or malformed payloads invalidate append reconstruction until a later explicit reset and remain visible evidence errors. It no longer reconstructs the remote generation from current/rewound retain rows or timestamp proximity; missing exact evidence remains a visible `unresolved` candidate. Reviewed keys are display/history metadata and cannot hide still-confirmed or unresolved candidates. Every selected direct log/health alert receives a stable key, must be bound to a report section, and must appear as an explicit alert-key anchor in that section, so one represented alert cannot mask omitted siblings.
- 2026-08-15 empty-response/replay correction: exact synthetic Assistant `(empty)` rows and the canonical empty-tool-response recovery nudge are transparent to live turn grouping and are removed when historical persisted rows are read. A historical visible Assistant attached to that nudge remains eligible, but carries an internal recovery-projection marker so an adjacent identical completion can collapse without content-only global deduplication; completing an adjacent orphan User preserves its stable source occurrence and original timestamp. A timestamp-less leading Assistant replay projection is discarded only when it exactly matches the persisted final Assistant and the same replay window contains a timestamped event provably after the persisted cutoff. Same-text real User events with different platform occurrence ids remain distinct. The profile-local monitor also strips only the exact leading model-switch Note envelope before grouping StateDB source text, so compacted physical rows such as `继续` resolve to their platform occurrence instead of being reported as source `0`.
- Built-in toolset `none` intentionally resolves to zero tools. The independent Hindsight monitor reviewer uses it together with `--ignore-rules`, so untrusted evidence can be judged without filesystem, terminal, memory, Skill, or Provider-source access.
- Profile-local Hindsight monitor scripts are operational assets under `$HERMES_HOME/scripts/`; their durable regression source lives under `~/code/scripts/tests/`, not Hermes fork deltas. Do not place monitor regressions under `tests/fork/`; repository tests should cover only code tracked by this repository. The external monitor may consume fork behavior such as toolset `none`, but that dependency does not make the monitor itself part of the fork.
- Persisted retain rows include `retain_document_id`, a stable logical document id inherited across compression-created child sessions.
- Gateway/CLI `/retain` uses SessionDB only to resolve the active `session_id`, `parent_session_id`, and optional title; SessionDB transcript rows are not a content source.
- Provider-owned `$HERMES_HOME/hindsight/retain_turns.sqlite3` is the sole manual `/retain` content source. It stores the same turn JSON generated by Hindsight `sync_turn()` for automatic retain.
- Gateway/CLI `/retain` directly calls `retain_persisted_session_lineage(...)`; it must not call `retain_conversation_messages(...)`, `SessionDB.get_messages_as_conversation(...)`, or `SessionStore.load_transcript(...)` to build manual retain content.
- Manual retain groups persisted turns by stable `retain_document_id`; this preserves a single logical Hindsight document even when compression/session bookkeeping records continuation sessions as siblings rather than a clean parent chain.
- Persisted retain lookup uses only `active=1` rows, so `/undo`-rewound rows are skipped while retained for local audit. Rewind counts real user turns rather than raw persisted rows and also excludes trailing assistant-only async-result events belonging to the rewound suffix.
- If no persisted rows exist, manual `/retain` returns `No persisted turns to retain.` rather than falling back to SessionDB or LCM/compression summaries.
- Historical note: the 2026-06-15 SessionDB-primary path was added to preserve interrupted/orphan user messages, but it allowed LCM `[Recent Summary ...]` rows to replace Hindsight documents. Do not revive that path in merge conflict resolution.
- 2026-08-14 user decision — do not treat evaluator `20260810_101115_f263e7a2` StateDB user `10317` (`继续`, Telegram `1338`) as a retain assembler miss. It sits between `提交` (`10311`) and `？` (`10318`). Hermes received it while the `提交` turn was still running and Telegram was reconnecting. There was no dedicated completed model turn for `继续`; the next model call and visible answer (`已继续核验`) followed `？`. Current `_build_turns_from_conversation_messages()` keeps a standalone `继续` if that StateDB row is in the completed-turn transcript; the production ledger never received it because completed-turn `sync_turn()` was not given that row. Local retain and the remote Document both omit it. This is expected completed-turn entry behavior, not a filter that drops the two characters `继续`, not a Hindsight remote loss, and not the 2026-08-11 compression-window “document starts at 继续” class. User said skip; do not “fix” it by reading SessionDB as retain content, adding a `继续` special case, or making retain backfill mid-busy StateDB users.
- Parent-chain lookup remains a fallback for older local rows without `retain_document_id`, and ignores empty stored parents when looking for a prior non-empty parent.
- Persisted turn lookup does not filter by the historical local `bank_id`; `/retain` submits matching lineage turns to the bank configured at retain time.
- Manual `/retain` submits a clean item with `content`, configured `context`, and `update_mode="replace"` when the API supports explicit update modes. Automatic/incremental retain still uses `append` for new-turn deltas.
- Legacy provider buffer flush still tracks one pending append job, a session generation guard, and queued/flushed turn counts so automatic retain and direct provider tests do not regress.
- After upstream `09d66037f` added `_last_retained_turn_count` for append retain deltas, this fork intentionally keeps automatic retain routed through `flush_retained_turns()` instead, so automatic retain and manual/direct flush share the same queued/flushed/pending/generation state machine while still sending only new turns on append-capable APIs.
- When `auto_retain=false`, completed turns are written only to the local retain-turn SQLite file until `/retain` submits them.
- Adds opt-in Hindsight settings `retain_on_new=false` and `retain_on_new_timeout_seconds=30`. When enabled, explicit `/new` and its `/reset` alias drain pending MemoryManager work and synchronously wait for the persisted-lineage Retain API request before any session mutation. Pending drain, persisted payload reconstruction, the API capability probe, and the waited Retain request share one total timeout budget; the probe's own network timeout is capped to the remaining budget.
- Retain-before-new is fail-closed across CLI, Gateway chat platforms, and TUI: timeout, API failure, unavailable Provider, or missing synchronous capability aborts the reset and preserves the old session. A successful API return permits reset; it does not claim that an asynchronously configured Hindsight server has completed downstream extraction.
- The Gateway running-agent fast path performs the same preflight before interrupting the agent, invalidating its run generation, clearing pending messages, releasing running state, or evicting the cached agent. It passes the acknowledged result into the reset handler so the successful path retains exactly once.
- Gateway platform adapters keep their existing command-scoped session guard across the complete `/new` / `/reset` handler, so follow-up messages remain queued until reset finishes and then resolve through the new routing entry. TUI uses an explicit session-boundary flag: prompts entered while Retain or `newSession()` is pending remain in the local composer queue, then drain to the preserved old session on Retain failure or to the new session on success.
- Manual `/retain` remains non-blocking. `retain_on_new` is independent of `auto_retain` and does not enable per-turn automatic submission.
- `/undo` now calls a dedicated memory rewind hook in CLI, Gateway, and TUI paths; Hindsight mirrors that rewind by soft-excluding the last N active rows in `hindsight_retain_turns` (`active=0`, `rewound_at`) so future manual `/retain` skips undone turns without hard-deleting audit rows.
- Hindsight rewind handling truncates the in-memory retain buffer and invalidates flush state without running the normal session-switch flush, so `/undo` does not itself push stale buffered turns to Hindsight.
- Only `/retain` is user-facing; no long command aliases are registered.
- Slack native slash generation keeps Telegram-visible canonical commands ahead of low-priority aliases so the extra fork-only `/retain` command keeps a native Slack slot under Slack's 50-command cap.
- Slack routes low-frequency or high-cost commands such as `/topup`, `/blueprint`, `/moa`, `/debug`, `/disk-cleanup` / `/disk_cleanup`, and `/lcm` through `/hermes <command>` on this fork when native slots are exhausted; this preserves native `/retain` while keeping those commands reachable on Slack.
- `hindsight_retain_session` is not registered in model-visible tool schemas; CLI/Gateway call the provider directly via `memory_manager.get_provider("hindsight")`, so manual retain works even when `memory_mode="context"` hides Hindsight tools from the model.

Why it matters:

- The user needs an explicit, user-triggered way to preserve the normal Hindsight session document without changing the automatic retain storage model.
- Manual `/retain` must preserve the exact Hindsight turn payloads that were already captured by the provider, and must not reconstruct content from SessionDB because SessionDB may contain LCM/compression summaries instead of original turns.
- Interrupted/orphan user messages must be captured into `retain_turns.sqlite3` at normal turn-sync time via `sync_turn(..., messages=...)`; `/retain` still must not reconstruct content directly from SessionDB.
- Preserve `on_pre_compress(messages)` retain snapshot: before compression discards live context, Hindsight must write retainable turns (including unfinished real first-user requests) into the provider-owned ledger so a later compressed continuation window cannot make the Document start at `继续`/correction text. Orphan completion prefers source occurrence ids when present; unequal ids must not merge. Pre-compress must not alone make incomplete orphans remote-flush-eligible on session switch.
- 2026-08-11 retain_on_new / StateDB parent trap: `/new` sessions can still carry the previous chat as StateDB `parent_session_id` while provider-owned turns were written under the new session document id. `_load_persisted_retain_turns` must prefer a session's own active document when all of its active rows already live under `retain_document_id == session_id`, instead of resolving through the previous chat's lineage and re-submitting the wrong Document (observed as a successful retain of the parent FIP session while the Codex capacity session stayed local-only / remote 404).
- 2026-08-14: a StateDB user row that never entered a completed-turn retain transcript is not automatically a retain bug. Do not change the retain contract to chase evaluator `10317` `继续`.

Merge protection:

- Preserve provider-owned `$HERMES_HOME/hindsight/retain_turns.sqlite3` as the only manual `/retain` content source and as the stable `retain_document_id` resolver for compression-created logical documents.
- CLI/Gateway `/retain` must not read SessionDB transcript content, must not call `retain_conversation_messages(...)`, and must not fall back to SessionDB when persisted rows are absent.
- SessionDB use in `/retain` is limited to locating the active session and parent/title metadata needed to find persisted rows. LCM `[Recent Summary ...]` rows must never be sent to Hindsight as retained content.
- Future attempts to preserve interrupted/orphan user messages must first persist those turns into `retain_turns.sqlite3`; do not revive the 2026-06-15 SessionDB-primary path.
- Do not treat evaluator `20260810_101115_f263e7a2` / StateDB `10317` `继续` as a production retain miss to repair. Preserve completed-turn `sync_turn()` entry; do not add a `继续` special case or SessionDB backfill for mid-busy users unless the user explicitly reopens this decision.
- Preserve Hindsight `sync_turn(..., messages=...)` as a MemoryManager opt-in path: it must rebuild clean retain turns from the completed transcript and persist the real first user message before later correction turns, without retaining tool outputs, assistant tool-call shells, summaries, `Operation interrupted:` notices, empty assistant messages, intermediate assistant drafts, compression/task-list rehydration markers, externalized payload placeholders, or async delegation completion payloads. The last eligible user-visible assistant response triggered by async completion must remain in order as an assistant-only retained event, never paired with a prior orphan user. When a compressed replay window contains only an exact synthetic `[Recent Summary (d0...)]` LCM header user row and the actual final assistant answer, retain only that assistant answer as an assistant-only event; preserve user-authored lookalikes as ordinary user content, and do not extend this exception to standalone Session-Arc/Durable/Depth summary turns.
- Preserve source occurrence identity end to end. Initial and queued Gateway events must pass their own platform message id to the current user turn; StateDB and retained turn JSON must retain it. Reconciliation may identity-match only non-empty occurrence ids and must never globally deduplicate equal user text. Multimodal normalization must not write base64 image data or local image paths to retained content; safe `http(s)` image references remain available for source lookup.
- Preserve the bounded empty-response/replay rules: remove only the exact synthetic `(empty)` / canonical recovery nudge (or rows carrying Hermes' explicit empty-recovery metadata), retain the real completion, and drop a leading timestamp-less Assistant projection only when exact persisted-content and later-than-cutoff replay evidence are both present. Do not replace these constraints with global role/content or timestamp tuple deduplication; legitimate same-text platform occurrences must remain distinct.
- Preserve the submission ledger as the exact generation boundary for monitoring. Every real retain submission path must record its outbound payload before advancing queued-flush watermarks or queueing work, and finish success/failure state after the API operation; ledger-insert or enqueue failure must leave the buffered turns retryable. The monitor must report an explicit evidence gap when no successful payload exactly binds the remote revision; do not restore remote-time/current-row inference, suppress unresolved candidates through reviewed state, or validate only that some direct alert appears.
- Preserve marker-based synthetic-noise cleaning in `_clean_retain_user_content()`; do not replace it with business-topic filters (e.g. drop-if-mentions-FIP).
- Preserve manual full-session retain as `replace` on APIs with explicit update modes. Normal automatic/incremental flush paths use `append`; the one exception is anchored replay reconciliation, whose next automatic flush must use one full `replace` to repair the remote document without duplication.
- Do not create a separate `manual-session:*` document; use the stable resolved `retain_document_id` so manual full-session retain replaces the logical Hindsight document while automatic retain append deltas remain separate.
- Do not expose `hindsight_retain_session` as a model-visible tool by default; this is a user slash command/provider method.
- Provider-store manual `/retain` must include all sessions that share the same `retain_document_id`, ordered by persisted row id; do not rely solely on `parent_session_id`, because compression continuations can appear as siblings in SessionDB.
- Gateway `/retain` must resolve the active session from `SessionStore.get_or_create_session(source)` before consulting cached agents, mirroring the normal message path after `/resume` or gateway restart.
- Manual `/retain` must not filter persisted turns by historical local `bank_id`; the current provider config determines the API target bank.
- Manual `/retain` payload items should stay clean (`content`, configured `context`, and `update_mode` only when needed), without extra metadata/tags.
- `/undo` must notify memory providers through the dedicated rewind hook, not only evict cached agents or call the normal session-switch hook; otherwise provider-owned persisted turns can drift from the active transcript.
- Hindsight `/undo` handling must mark local persisted retain rows inactive and must not run flush-on-switch for rewound buffered turns.
- Preserve the retain-before-new gate before every `/new` / `/reset` session mutation. Do not move it after SessionDB end/reset, Gateway generation invalidation, TUI `newSession()`, history clearing, or Provider session switch.
- In the Gateway active-session dispatch path, do not call `_interrupt_and_clear_session()` before retain-before-new succeeds. A failed Retain must leave the running agent, run generation, and pending queues untouched.
- Preserve one total `retain_on_new_timeout_seconds` budget across pending-memory drain, persisted payload reconstruction, capability probing, and the waited Hindsight API request; cap the probe's network timeout to the remaining deadline, and do not silently convert the waited call back into queue-only success.
- Preserve the TUI session-boundary queue gate until both `session.retain_before_new` and `newSession()` finish. Do not let `queue`, `steer`, or `interrupt` busy-input modes submit a racing prompt to the old session during that interval.
- Retain-before-new failures must keep the old session usable and visible. Do not degrade this to “reset anyway with a warning.”
- Keep `retain_on_new` opt-in and independent from `auto_retain`; manual `/retain` must remain asynchronous unless deliberately changed and revalidated separately.
- Preserve tests proving manual retain persists Hindsight turn payloads to the separate SQLite file, groups compression siblings by root `retain_document_id`, falls back through prior non-empty parent rows for older data, resolves resumed/restarted gateway sessions without cached agents, ignores historical local bank casing/config changes, handles no-persisted-turn sessions, excludes rewound persisted turns by real-user-turn count, keeps legacy buffer flush behavior (pending rejection, failure rollback, generation guard, `memory_mode="context"`), verifies gateway interrupt / multi-user-turn transcript sync preserves the real first user message, drops async-delegation/compression/externalized synthetic payloads while retaining ordered user-visible async assistant results and residual real user text, and keeps Slack/Telegram slash registration parity despite the extra `/retain` command.
- Do not reintroduce upstream's standalone `_last_retained_turn_count` watermark unless the entire fork flush state machine is deliberately replaced and all manual `/retain`, append-delta, pending-failure rollback, and session-switch generation tests still pass. The expected fork behavior is `sync_turn()` persists the turn first, then automatic retain calls `flush_retained_turns()`.

Verification:

- 2026-08-15 empty-response/replay and monitor normalization: four new Hindsight regressions and one monitor regression were developed RED→GREEN. The nine Hindsight-related repository test files plus the empty-response persistence file passed with 284 tests; the profile-local monitor evidence suite passed with 18 tests. Both modified Python implementations passed `py_compile`, and `git diff --check` passed. Read-only replay of production evidence reduced case 1 from seven active rows to six logical turns with the completion once; normalized case 2 from 17 active rows to 15 logical turns with `好` and the completion once and zero `(empty)`/nudge content; both compacted case-3 StateDB rows canonicalized to `继续`, with platform occurrence `3332` preserved on the source row. No production SQLite row, remote Hindsight object, configuration, or running process was modified.
- 2026-07-29 orphan-Assistant replay enrichment: a production-shaped regression first failed because a persisted Assistant-only row caused the later complete `User: 提交` plus identical Assistant replay turn to be discarded. Reconciliation now replaces that orphan with the complete turn, preserves its row ownership, and remains idempotent on a second identical replay. `python -m pytest tests/fork/test_hindsight*.py -q -o 'addopts='` → 178 passed; the profile-local monitor suite → 9 passed; Ruff, `python -m py_compile`, `git diff --check`, historical document audits, and production-session read-only replay passed. Independent review returned PASS with no Critical or Important findings.

- 2026-07-28 retain-before-`/new` / `/reset`: provider, Gateway, CLI, and TUI regressions were developed RED→GREEN, including API failure, API timeout, pending-drain timeout, one shared timeout budget, Gateway cold-provider loading, no-local-history CLI retain, mutation ordering, TUI failure preservation, and `/reset` without a loaded command catalog. Independent review first found the Gateway running-agent fast path still interrupted and evicted the old agent before entering the gate; direct dispatcher regressions reproduced it, and the path now retains exactly once before interrupt/reset while failure preserves the running slot, generation, pending queue, and adapter activity. The completed review also alleged a Gateway follow-up race, but source inspection plus `TestAdapterSessionCancellation::test_new_keeps_guard_until_command_finishes_then_runs_follow_up` confirmed the existing platform command guard already serializes this path. The equivalent TUI two-RPC window was real: new RED tests showed no boundary guard, then passed after prompts in every busy-input mode were routed to the existing composer queue until Retain plus `newSession()` complete. Two additional RED provider tests reproduced the capability probe taking its independent 5-second timeout and payload preparation receiving time outside the API wait; both now consume the same absolute deadline. Final verification: `python -m pytest tests/fork -q -o 'addopts='` → 546 passed / 8 pre-existing dependency warnings; focused retain-before-new Python suite → 121 passed; adjacent Hindsight/MemoryManager suites → 158 passed; TUI `submissionCore` + `createSlashHandler` suites → 85 passed. Ruff, `py_compile`, TUI TypeScript typecheck, targeted ESLint with no warnings, and `git diff --check` passed. The real `$HERMES_HOME/hindsight/config.json` was not modified.
- 2026-07-27 mixed numeric/naive-local timestamp correction: the production-shaped provider restart regression first failed because the second retained turn contained only the Assistant, then passed with the full ordered User/Assistant pair after making local timestamp normalization idempotent. `python -m pytest tests/fork/test_hindsight_*.py tests/plugins/memory/test_hindsight_provider.py -q -o 'addopts='` → 280 passed; `git diff --check` passed. A fresh independent review returned PASS, with the existing DST-fold ambiguity of offset-free wall-clock persistence noted as a pre-existing format limitation rather than a new regression.
- 2026-07-24 replay synthetic-timestamp dedupe: production-shaped regressions first reproduced a historical Assistant becoming a second assistant-only turn when replay supplied no source timestamp, then reproduced the same rewritten old prefix being inserted again on the next replay. Independent review additionally reproduced two loss paths: a stable later repeated sequence before an existing anchor, and a time-unknown assistant-only async event between stable anchors. All four cases went RED→GREEN. `python -m pytest tests/fork/test_hindsight_retain_document_flow.py -q -o 'addopts='` → 29 passed; the eight Hindsight-related test files → 255 passed; Ruff, `python -m py_compile`, and `git diff --check` passed. A read-only reconstruction from production retain rows `5439`/`5440`/`5441` plus new tail `5446` produced four ordered turns with the historical target Assistant exactly once and the real new tail exactly once. Fresh specification review returned PASS; final quality review returned APPROVED with no Critical or Important findings.
- 2026-07-20 tool-budget notice correction and review follow-ups: the original transcript regression first failed because the injected notice became a retained User turn. The first independent review reproduced two over-filtering defects: a multiline user quotation lost its exact quoted line, and a legitimate Assistant answer equal to the notice was dropped. Follow-up RED tests proved `handle_max_iterations()` mutated durable `messages`; the structural fix moved the synthetic request into the API-call copy. A second independent review then reproduced two remaining gaps: plain Assistant tails produced consecutive Assistant rows or lost visible fallback/error output, and `budget notice → model-switch notice → real user text` survived historical cleanup. New RED tests cover initial success, empty retry, API failure, transcript rebuild, clean-on-retain, and both runtime-marker orders. The final implementation closes every visible summary path through one tail-aware persistence step, preserves User/Assistant lookalikes, and iteratively strips only recognized leading runtime sequences. Real StateDB message `105443` still cleans to exactly `继续`; every source notice present in the active production session at verification time is absent from its read-only rebuilt retain payload. The related suite passed with `292 passed`; Ruff, `python -m py_compile`, and `git diff --check` passed.
- 2026-07-18 Recent-Summary orphan-assistant correction: the focused provider regression first failed with zero retained turns, then passed after preserving only the visible assistant event. A fresh negative regression also first reproduced the over-broad `[Recent Summary request]` false trigger, then passed after summary detection was limited to exact LCM header syntax. The historical combined Hindsight/monitor verification passed with `256 passed`; its monitor-stage checks, live `--defer-state` run, candidate-stage evidence, and state-commit checks exercised profile-local scripts rather than repository-owned fork code. Those operational tests now live at `$HERMES_HOME/scripts/tests/test_hindsight_monitor_scripts.py` and are intentionally excluded from `tests/fork/`. The read-only run returned a state checkpoint but left the real monitor state SHA-256 unchanged. Direct evidence checks recovered all six confirmed historical omissions (`201422`, `201464`, `202446`, `202469`, `202520`, `202802`) as candidate-level `before_local_retain`, proved each local active retain payload still matched its remote Document, and excluded inactive false-positive ids `192891`, `197989`, and `197990`.
- 2026-07-16 async-completion visible-result correction: exact regressions first failed with the final assistant result missing and `/undo 1` removing only the trailing assistant-only row. Fresh spec reviews returned `PATCH`; each finding was independently reproduced as a failing test before repair. Follow-up RED→GREEN cases cover pre-fix replay duplication, orphan-user mispairing, legacy empty-document-id rows, root/child session ownership, below-threshold reconciliation flushed during session switch, compressed partial replay windows, repeated identical anchor text, a disjoint post-restart tail, and legitimate later repeated sequences both with and without an additional new tail. The final related command `python -m pytest tests/fork/test_hindsight_provider_regressions.py tests/fork/test_hindsight_retain_document_flow.py tests/fork/test_hindsight_rewind.py tests/agent/test_memory_provider.py::TestMemoryManager::test_sync_all_passes_messages_to_opted_in_provider tests/plugins/memory/test_hindsight_provider.py tests/agent/test_memory_session_switch.py tests/agent/test_memory_async_sync.py tests/run_agent/test_memory_sync_interrupted.py tests/gateway/test_undo_rewind_session.py tests/tui_gateway/test_undo_command.py -q -o 'addopts='` → 236 passed; Gateway/CLI retain command tests → 7 passed. `python -m py_compile`, `ruff check`, and `git diff --check` passed. Read-only replay of production session `20260715_180736_de081e45` merged the persisted 32 events with the active 6-event compressed window into 35 ordered events, added exactly the 3 missing assistant-only results once, omitted all `ASYNC DELEGATION` payloads, and preserved subsequent user/assistant order.
- 2026-07-10 clean retain-turn noise filter + review follow-up (Durable/Depth, assistant symmetry, clean-on-retain): `python -m pytest tests/fork/test_hindsight_provider_regressions.py tests/fork/test_hindsight_retain_document_flow.py -q -o 'addopts='` → 50 passed; production dirty doc offline sanitize keeps 35/72 and drops known synthetic markers.
- 2026-07-07 gateway interrupt / multi-user-turn retain flow fix: added `tests/fork/test_hindsight_retain_document_flow.py` to exercise `AIAgent._sync_external_memory_for_turn` → `MemoryManager.sync_all` → Hindsight `sync_turn(..., messages=...)` → `retain_turns.sqlite3` → `/retain`/`retain_persisted_session_lineage` with fake Hindsight clients only. `python -m pytest tests/fork/test_hindsight_retain_document_flow.py -q -o 'addopts='` → 2 passed; `python -m pytest tests/fork/test_hindsight_retain_document_flow.py tests/fork/test_hindsight_provider_regressions.py tests/fork/test_hindsight_rewind.py tests/agent/test_memory_provider.py::TestMemoryManager::test_sync_all_passes_messages_to_opted_in_provider -q -o 'addopts='` → 49 passed; `python -m pytest tests/plugins/memory/test_hindsight_provider.py tests/agent/test_memory_session_switch.py tests/agent/test_memory_async_sync.py tests/run_agent/test_memory_sync_interrupted.py tests/gateway/test_undo_rewind_session.py tests/tui_gateway/test_undo_command.py -q -o 'addopts='` → 167 passed; `python -m py_compile plugins/memory/hindsight/__init__.py tests/fork/test_hindsight_retain_document_flow.py` and `git diff --check` passed.
- 2026-07-01 SQLite-only manual `/retain` content-source fix: new Gateway/CLI regressions first failed while `/retain` called `retain_conversation_messages(...)`; after the fix, `python -m pytest tests/fork/test_gateway_retain_command.py tests/fork/test_cli_retain_command.py tests/fork/test_hindsight_provider_regressions.py -q -o 'addopts='` → 49 passed, and `python -m pytest tests/plugins/memory/test_hindsight_provider.py tests/agent/test_memory_session_switch.py tests/agent/test_memory_async_sync.py tests/run_agent/test_memory_sync_interrupted.py tests/gateway/test_undo_rewind_session.py tests/tui_gateway/test_undo_command.py -q -o 'addopts='` → 167 passed.
- 2026-06-17 recursive lineage + boundary replay dedupe fix: `python -m pytest tests/plugins/memory/test_hindsight_provider.py::TestToolHandlers::test_transcript_retain_dedupes_parent_child_boundary_overlap_only tests/fork/test_gateway_retain_command.py::test_retain_command_uses_session_transcript_lineage_when_available -q -o 'addopts='` → 2 passed after first failing with duplicate boundary content and missing `_session_id` lineage tags.
- 2026-06-18 upstream-sync resolution for recursive lineage + boundary replay dedupe + original timestamp fix: preserved upstream's full `tests/test_hermes_state.py` and added the fork timestamp opt-out regression instead of keeping the bad 28-line replacement; `python -m py_compile agent/transports/codex.py hermes_state.py plugins/memory/hindsight/__init__.py cli.py gateway/run.py tests/agent/transports/test_codex_transport.py tests/test_hermes_state.py tests/plugins/memory/test_hindsight_provider.py tests/fork/test_gateway_retain_command.py tests/fork/test_cli_retain_command.py` passed; `python -m pytest tests/agent/transports/test_codex_transport.py tests/test_hermes_state.py tests/plugins/memory/test_hindsight_provider.py tests/fork/test_gateway_retain_command.py tests/fork/test_cli_retain_command.py tests/run_agent/test_run_agent_codex_responses.py tests/gateway/test_agent_cache.py -q -o 'addopts='` → 630 passed.
- 2026-06-20 transcript completeness fix for Document `20260619_183111_1f26c39e`: added regression coverage for `order_by="id"`, CLI/Gateway manual `/retain` using insertion order, and last-eligible-assistant pairing; `python -m pytest tests/plugins/memory/test_hindsight_provider.py tests/fork/test_gateway_retain_command.py tests/fork/test_cli_retain_command.py tests/test_hermes_state.py -q` → 439 passed; `git diff --check` → passed. Local sample reconstruction for root `20260619_183111_1f26c39e` + child `20260619_185935_ad6649` contained `已落地` and `继续进化了一轮`, and excluded `Need template patch`.
- 2026-06-20 manual full-session replace fix for Document `20260620_154051_8e27678d`: added regression coverage that tagged SessionDB root→tip lineage transcripts are authoritative, do not merge stale persisted retain rows, and manual full-session retains use `update_mode="replace"`; `python -m pytest tests/plugins/memory/test_hindsight_provider.py -q -o 'addopts='` → 154 passed; `python -m pytest tests/plugins/memory/test_hindsight_provider.py tests/fork/test_gateway_retain_command.py tests/fork/test_cli_retain_command.py tests/test_hermes_state.py -q` → 440 passed; `git diff --check` → passed.
- 2026-06-15 orphan-user transcript fix: `python -m pytest tests/hermes_cli/test_commands.py tests/run_agent/test_memory_sync_interrupted.py tests/agent/test_memory_session_switch.py tests/agent/test_memory_async_sync.py -q -o 'addopts='` → 195 passed / 1 unrelated `audioop` deprecation warning.
- `python -m pytest tests/plugins/memory/test_hindsight_provider.py tests/hermes_cli/test_commands.py tests/fork/test_gateway_retain_command.py -q -o 'addopts='` → 260 passed.
- `python -m py_compile plugins/memory/hindsight/__init__.py cli.py gateway/run.py hermes_cli/commands.py tests/fork/test_gateway_retain_command.py` → passed.
- `git diff --check` → passed.
- `python -m pytest tests/plugins/memory/test_hindsight_provider.py -q` → 127 passed after adding `retain_document_id` grouping.
- 2026-06-09 conflict resolution against upstream `09d66037f`: `python -m pytest tests/plugins/memory/test_hindsight_provider.py tests/agent/test_memory_session_switch.py tests/run_agent/test_memory_sync_interrupted.py -q -o 'addopts='` → 158 passed, 1 unrelated `audioop` deprecation warning.
- Rewind filtering update: `python -m pytest tests/plugins/memory/test_hindsight_provider.py tests/agent/test_memory_session_switch.py tests/agent/test_memory_async_sync.py tests/run_agent/test_memory_sync_interrupted.py tests/gateway/test_undo_rewind_session.py tests/tui_gateway/test_undo_command.py -q -o 'addopts='` → 186 passed.
- Rewind filtering update: `python -m pytest tests/hermes_cli/test_commands.py tests/fork/test_gateway_retain_command.py -q -o 'addopts='` → 148 passed.

Feature docs: `docs/chantxu64/hindsight-manual-retain.md`.

Upstream status: fork-only.

### 10. Custom hosted STT provider

Status: active

Date: 2026-05-22; Qwen Audio 3.0 update 2026-08-01; custom keyword context 2026-08-02

Files:

- `tools/transcription_tools.py`
- `hermes_cli/config_defaults.py`
- `agent/transcription_registry.py`
- `tests/fork/test_custom_stt.py`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- Adds a configurable custom STT provider that supports generic OpenAI-compatible endpoints and Alibaba DashScope multimodal ASR, with `qwen-audio-3.0-asr-flash` as the fork default.

What changed:

- Added `stt.provider: custom_api` dispatch in `tools/transcription_tools.py`.
- Added `stt.custom_api` config for `base_url`, `api_key` / `api_key_env`, `model`, `endpoint`, `mode`, `response_format`, `language`, `prompt`, `keywords`, and `timeout`.
- Preserved generic multipart audio uploads and DashScope-style chat-completions audio input.
- Added `dashscope_multimodal` mode for Qwen-Audio-3.0-ASR-Flash. It posts a Base64 Data URL through DashScope's `input.messages[].content[].input_audio` shape, requests a non-SSE response, and reads `output.text`.
- Runtime custom STT defaults now target `qwen-audio-3.0-asr-flash` at DashScope's multimodal generation endpoint. Neutral `DEFAULT_CONFIG` schema leaves preserve legacy endpoint inference and the `config.yaml` > `STT_CUSTOM_API_*` environment > runtime-default resolution order. Its effective prompt is empty because DashScope treats text messages as ASR context rather than transcription instructions; compatible modes retain their historical prompt default.
- DashScope multimodal mode normalizes `keywords` as an ordered, de-duplicated list. A configured `prompt` and/or keyword list is sent as an `input_text` context message immediately before the audio message; empty values preserve the prior audio-only request shape.
- Custom STT language hints follow the shared provider/global resolution order for compatible multipart and chat-completions modes. DashScope multimodal does not use a language hint. Unsupported mode names fail before any network request.
- It parses common transcription response shapes: `{text: ...}`, plain text, `{choices:[{message:{content: ...}}]}`, and DashScope `{output:{text: ...}}`.
- Added tests for provider selection, dotenv/env-key resolution, default configuration, all three request modes, response parsing, and `transcribe_audio()` dispatch.

Why it matters:

- The user's gateway can use the evaluated Qwen Audio 3.0 STT model through normal Hermes configuration while retaining generic custom endpoint support.
- Future upstream merges must not collapse custom STT back into OpenAI-only credentials or hardcoded provider names.

Merge protection:

- Preserve explicit `stt.provider: custom_api` behavior and do not silently fall back to another STT provider when custom credentials are missing.
- Preserve `custom_api` as a native/built-in STT provider name in `agent/transcription_registry.py`; command providers and plugin providers must not shadow the fork's configured custom STT implementation.
- Preserve `api_key_env` lookup through `get_env_value()` so keys in `~/.hermes/.env` work.
- Preserve the Qwen Audio 3.0 configuration shape: `QWEN_API_KEY`, model `qwen-audio-3.0-asr-flash`, base URL `https://dashscope.aliyuncs.com`, endpoint `/api/v1/services/aigc/multimodal-generation/generation`, and `dashscope_multimodal` mode.
- Preserve `stt.custom_api.keywords` and its DashScope `input_text` context mapping; do not move keywords into the audio item or send an empty context message.
- Preserve response parsing for OpenAI-compatible and DashScope multimodal response shapes unless upstream has a verified equivalent.

Verification:

```bash
python -m py_compile tools/transcription_tools.py hermes_cli/config_defaults.py tests/fork/test_custom_stt.py
python -m pytest tests/fork/test_custom_stt.py tests/tools/test_transcription.py tests/tools/test_transcription_dotenv_fallback.py tests/hermes_cli/test_config.py tests/gateway/test_stt_config.py -q -o 'addopts='
```

Feature docs: none — this remains a focused provider extension fully described in the index.

Upstream status: fork-only.

### 11. Custom Qwen/DashScope TTS provider

Status: active

Date: 2026-05-28

Files:

- `agent/tts_registry.py`
- `tools/tts_tool.py`
- `tests/agent/test_tts_registry.py`
- `tests/fork/test_custom_qwen_tts.py`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- Adds a configurable `tts.provider: custom_api` path so the fork can synthesize speech through Qwen/DashScope using the same `QWEN_API_KEY` convention as custom STT.

What changed:

- Added `custom_api` as a built-in TTS provider in `tools/tts_tool.py`.
- Reserved `custom_api` in `agent/tts_registry.py` so plugin registration cannot
  shadow the fork's native provider; this list must stay synchronized with
  `BUILTIN_TTS_PROVIDERS`.
- Added `tts.custom_api` resolution for `base_url`, `endpoint`, `mode`, `api_key` / `api_key_env`, `model`, `voice`, `language_type`, `response_format`, `speed`, `timeout`, and `extra_body`.
- Default custom TTS config targets Alibaba DashScope Qwen TTS: `https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation`, `mode: dashscope_multimodal`, model `qwen3-tts-flash`, voice `Cherry`, and `api_key_env: QWEN_API_KEY`.
- The DashScope multimodal mode posts `model` plus `input.text` / `input.voice`, then downloads the returned `output.audio.url` into the requested audio file.
- A generic `audio_speech` mode remains available for OpenAI-compatible `/audio/speech` endpoints.
- Custom API JSON, direct-audio, and DashScope URL-download responses use the upstream bounded streaming reader and the shared TTS response-size limit.
- Telegram voice delivery can convert custom TTS output to Opus/OGG for voice-compatible media.
- Added focused tests for OpenAI-compatible request construction, DashScope multimodal request construction, URL audio download, JSON base64 audio parsing, and missing `QWEN_API_KEY` errors.

Why it matters:

- The user wants Chinese voice replies to use the existing Qwen/DashScope credential setup rather than Edge TTS or a separate TTS-specific key.
- Future upstream merges must not remove the `custom_api` TTS provider path or collapse it into Edge/OpenAI-only behavior.

Merge protection:

- Preserve `custom_api` as a native/built-in TTS provider name; command providers or plugin providers must not shadow this configured implementation.
- Keep `agent.tts_registry._BUILTIN_NAMES` synchronized with
  `tools.tts_tool.BUILTIN_TTS_PROVIDERS`; run the registry invariant test after
  upstream changes to TTS provider discovery.
- Preserve `api_key_env` lookup through `get_env_value()` so keys in `~/.hermes/.env` work.
- Preserve DashScope multimodal handling of `output.audio.url`; Qwen TTS does not use the OpenAI-compatible `/audio/speech` endpoint shape by default.
- Keep custom API POST responses and DashScope audio downloads on the shared bounded streaming reader; do not restore eager `response.content` / `response.json()` reads.
- Preserve the generic `audio_speech` mode unless upstream provides a verified equivalent configurable HTTP TTS provider.
- Preserve Telegram Opus conversion behavior for `custom_api` when voice-compatible delivery is needed.

Verification:

```bash
scripts/run_tests.sh tests/agent/test_tts_registry.py tests/fork/test_custom_qwen_tts.py tests/tools/test_tts_response_body_cap.py tests/tools/test_tts_plugin_dispatch.py tests/tools/test_tts_command_providers.py tests/tools/test_tts_opus_routing.py tests/tools/test_tts_max_text_length.py -q
```

- 2026-07-10 upstream sync: upstream's newer provider registry exposed a logical
  merge gap because `custom_api` existed only in the dispatcher. After reserving
  it in `agent/tts_registry.py`, the fork and changed-upstream focused suite
  reported `2070 passed`; the TTS subset in the command above was included.
- Later in the same sync, upstream advanced another 9 commits including null
  subsection guards in `tools/transcription_tools.py` and `tools/tts_tool.py`.
  The final fork plus changed-upstream focused suite reported `1210 passed`.

Feature docs: none — TTS provider extension documented in this index.

Upstream status: fork-only.


### 12. Request-only recall isolation and Codex prompt-cache routing

Status: active fork maintenance

Date: 2026-06-16, restored 2026-07-22; upstream routing merged 2026-08-08;
LCM request-context placement 2026-08-15

Decision and behavior:

- Hindsight recall, `pre_llm_call` user-message context, explicit plugin request
  context, and gateway turn notes are volatile additions for the current
  provider request. Durable user history keeps the clean user-authored content
  only.
- A later turn replays historical `content`; it never substitutes an older
  `messages.api_content` value. This intentionally accepts a prompt-cache
  boundary miss when volatile recall changes rather than replaying stale recall
  as if it were new user input.
- OpenAI/Codex Responses places explicit plugin request context followed by the
  current turn's recall in one request-only `developer` item immediately after
  the clean current user item. That position is rebuilt for every same-turn tool
  call, but the developer item is not replayed on the next user turn. Other
  runtimes receive both on a copy of the current user content in the same order.
  Ordinary plugin context and Gateway one-turn context remain on the current-user
  request copy for all runtimes; only a plugin that explicitly selects request
  context can use the provider-aware route.
- MoA reference fan-out and its aggregator receive a provider-neutral request
  copy containing explicit request context, current recall, and ordinary
  plugin/Gateway context. The acting model still receives its normal
  provider-specific shape; neither MoA auxiliary path mutates durable history.
- The max-iteration forced-summary request receives the same current
  `TurnContext` as the preceding tool loop. Chat-completions keeps explicit
  request context followed by recall on the current-user request copy;
  OpenAI/Codex Responses keeps both in that order in the request-only developer
  item after the user. The synthetic summary request and all volatile context
  remain absent from durable history.
- String and multimodal composition never mutates the durable message object or
  its content list.
- New normal CLI, Gateway, branch, compression, and session-flush paths do not
  write or forward `api_content`. The nullable SQLite column and low-level
  reader/writer compatibility remain so existing databases require no unsafe
  schema migration; legacy values are stripped before model requests. The one
  live-only exception is active-turn redirect scaffolding: an in-memory marker
  lets the same-turn provider copy consume its sidecar, while persistence and
  later replay still keep only clean transcript content, including early
  terminal-return paths and rich Gateway tool-call replay.
- In-place compression does not backfill a sidecar. Max-iteration summaries and
  Gateway replay also ignore legacy values.
- `codex_app_server` remains separate: request-only recall is prefetched for the
  normal memory lifecycle but is not injected into its persistent Codex thread,
  because the protocol has no safe per-request volatile input slot.

Codex Responses cache routing:

- The physical Hermes `session_id` remains distinct from the logical cache
  scope.
- Logical scope priority is the stable Gateway `_gateway_session_key`, then the
  root returned by compression-only lineage. Branch and delegate parent links
  do not merge cache scope. If lineage lookup is unavailable or fails, routing
  falls back to the physical session id.
- Known ordinary non-compression sessions use upstream's bounded,
  content-addressed `prompt_cache_key` derived from physical session scope,
  static instructions, and tool schema, including the 64-character hardening.
  Recurring Cron fire timestamps are removed from the physical cache scope so
  different runs of the same job share a stable key while different jobs remain
  isolated.
- Codex backend HTTP routing is:
  - `session_id` = raw physical Hermes session id
  - `thread-id` = logical/bounded `prompt_cache_key`
  - `x-client-request-id` = logical/bounded `prompt_cache_key`
- Both cache-routing headers mirror the effective body `prompt_cache_key`.
  Explicit top-level overrides take precedence over an `extra_body` spelling;
  an extra-body-only override still becomes the effective key. Only non-empty
  string overrides qualify, so malformed top-level values cannot shadow a valid
  extra-body key. The duplicate extra-body field is removed before the SDK
  builds the request, so body and headers cannot diverge during merge.
- Do not restore the obsolete `session-id` spelling.

Primary files:

- `agent/turn_context.py`
- `agent/conversation_loop.py`
- `agent/codex_responses_adapter.py`
- `agent/chat_completion_helpers.py`
- `agent/model_metadata.py`
- `agent/turn_finalizer.py`
- `agent/transports/codex.py`
- `run_agent.py`
- `gateway/run.py`
- `gateway/session.py`
- `gateway/slash_commands.py`
- `hermes_cli/cli_commands_mixin.py`
- `hermes_state.py` (schema compatibility only)
- `tests/agent/test_api_content_sidecar.py`
- `tests/agent/test_model_metadata.py`
- `tests/agent/test_gateway_turn_sidecar.py`
- `tests/agent/transports/test_codex_transport.py`
- `tests/gateway/test_replay_entry_fields.py`
- `tests/run_agent/test_run_agent_codex_responses.py`
- `tests/run_agent/test_codex_app_server_integration.py`
- `tests/fork/test_codex_request_only_memory_context.py`

Merge protection:

- Upstream commit `7b3dcee92` introduced exact-wire `api_content` persistence
  and replay. Preserve the fork's request-only isolation when syncing code that
  touches that mechanism; an upstream nullable column is harmless, historical
  sidecar substitution is not.
- Preserve current-turn must-deliver gateway notes while keeping them out of
  durable history, including multimodal turns.
- Preserve the current-turn Codex `user → developer(request-context → recall)`
  position across tool calls and max-iteration summaries. Ordinary plugin and
  Gateway context must remain on the user request copy. Do not restore the
  historical cross-turn replay of prior developer memory slots; that
  cache-affinity workaround violates the current lifecycle contract.
- Preserve current-turn explicit request context, recall, and ordinary
  plugin/Gateway context in MoA reference and aggregator requests; building MoA
  advice from the clean durable transcript alone silently drops current context.
- Preserve Hindsight P5/synchronous recall, `/retain`, `/undo`, multi-Telegram
  account routing, and upstream Gateway lifecycle improvements.
- Do not merge delegate or branch cache scope merely because
  `parent_session_id` is present.
- Do not remove upstream content-addressed key hardening while restoring the
  fork's logical/physical routing split.

Historical implementation references:

- `ce52975c27` introduced the fork's Codex developer-item support for
  request-only memory context.
- `ca60311b33` is a useful current-turn placement reference, but its replay of
  prior developer-memory slots is intentionally not restored.
- `a19af2e5a2`, `9a3a8e18d0`, `4d39a603d1`, and `bafa2360dc9` document the
  stable cache scope, physical/logical header split, and corrected `session_id`
  spelling that this restoration adapts to current upstream code.

Verification after the 2026-07-22 restoration:

- The 2026-08-15 LCM request-context change was observed RED before production
  edits: OpenAI/Codex Responses still put LCM guidance in user content, and the
  host had no separate non-Responses fallback channel (`2 failed, 1 passed`).
  After repair, the focused request-only, Codex/OpenAI-API, MoA, Gateway,
  tool-loop, next-turn, max-summary, timeout-parity, and finalizer suite reported
  `141 passed`. Ruff, `py_compile`, and `git diff --check` also passed.
- Initial RED before production edits: `14 failed, 1 passed`, covering sidecar
  stamping, replay, multimodal mutation, summary replay, and Codex header
  separation.
- Final semantic review found two gaps not covered by that first rebaseline:
  current-turn Codex developer placement (`3 failed`) and obsolete
  `session-id` removal (`1 failed`). Both were observed RED before their
  production fixes.
- Focused request-isolation, Codex transport/runtime, app-server, Gateway replay,
  prompt-tail, state compatibility, syntax, and whitespace validation:
  `335 passed`; `git diff --check` and `py_compile` also passed.
- Adjacent compression, replay cleanup, chat-completions, branch/resume/undo,
  compression-lineage, and multi-Telegram regression suite: `389 passed`
  (`7` third-party deprecation warnings).
- Independent pre-commit review then found two request shapes missing current
  context: MoA auxiliary calls and the max-iteration forced summary. Focused RED
  reproduced all three provider shapes (`3 failed`): MoA, chat-completions
  summary, and Codex summary. After repair, an end-to-end
  `run_conversation → turn_finalizer → summary` regression also passed. The MoA
  regression executes the real `aggregate_moa_context()` consumer path and
  separately captures the rendered reference request and aggregator synthesis
  request; it does not mock the function under test.
- Post-repair request-only/Codex/MoA/turn-finalizer suite: `239 passed`.
  Existing `TestHandleMaxIterations`: `18 passed`. A final main-agent gate review
  then reproduced a stale-index defect after message repair moved the current
  user: the forced summary attached context to its synthetic summary request
  instead (`1 failed`). Synchronizing the loop's latest re-anchored index into
  the ephemeral `TurnContext` made the end-to-end regression pass. The final
  expanded focused suite, additionally covering Codex transport, Gateway replay,
  prompt-tail, and state compatibility, reported `408 passed`. The adjacent
  regression suite remained `389 passed` with the same `7` third-party
  deprecation warnings; `git diff --check` and `py_compile` passed.

Upstream status: intentional fork divergence from persistent `api_content`
replay; compatible upstream schema and content-addressed key hardening retained.


### 13. Multi Telegram bots in one profile (account_id session slots)

Status: active

Date: 2026-07-13

Files:

- `gateway/session.py`
- `gateway/config.py`
- `gateway/platforms/base.py`
- `gateway/authz_mixin.py`
- `gateway/slash_commands.py`
- `gateway/run.py`
- `plugins/platforms/telegram/adapter.py`
- `tests/fork/test_multi_telegram_accounts.py`
- `tests/gateway/test_background_process_notifications.py`
- `tests/gateway/test_resume_command.py`
- `docs/chantxu64/multi-telegram-accounts/README.md`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- One Hermes profile can run multiple Telegram bot tokens; each bot is an
  independent session slot (like multi CLI), while config/skills/memory stay shared.

What changed:

- Env discovery: `TELEGRAM_BOT_TOKEN_<ACCOUNT>` extras under
  `platforms.telegram.extra.accounts`; multiplex profile loads enumerate only
  the active profile secret scope and cannot inherit another profile's tokens.
- Primary `TELEGRAM_BOT_TOKEN` still owns `adapters[Platform.TELEGRAM]` and is
  required when named bots are configured; named tokens are not promoted into
  the legacy primary slot.
- Extra bots live in `GatewayRunner._telegram_account_adapters`.
- `SessionSource.account_id` + session key suffix `:account:<id>` isolates
  short-term context; real `user_id`/`chat_id` stay for ownership and `/resume`.
- Database peer recovery requires the same account suffix, preventing a fresh
  named route from recovering the primary bot's persisted session id.
- Telegram stamps `account_id` while building pre-dispatch auth/event sources,
  before batching, observation persistence, or session-key computation.
- `_adapter_for_source()` resolves a named account from
  `_telegram_account_adapters` and fails closed if that configured account is
  unavailable, preventing streaming/typing/media/busy traffic from falling back
  to primary.
- Background process/watch notifications rebuild an account-aware source and
  send or inject through the originating named adapter.
- Cross-bot `/resume` transfers one idle transcript to the current bot route and
  unbinds the old route; a running target is rejected instead of dual-bound.
- Named accounts have an independent fatal-error/reconnect queue that retains
  their own token/config and never replaces or tears down the primary adapter.
- `/restart` routing metadata retains `account_id`, so shutdown/comeback
  lifecycle notices use the originating bot.
- Personal-use boundary: named bots use ordinary DM sessions; Telegram DM Topic
  mode remains on the primary bot, and `/update` multi-account lifecycle routing
  is intentionally outside this change.

Why it matters:

- User wants multi-CLI-like Telegram doors without multi-profile brains.
- Must not collide Telegram DM chat_id across bots for the same human.

Merge protection:

- Preserve when: multi-token env + account session suffix + primary adapter
  compatibility.
- Drop when: upstream merges an equivalent multi-bot shared-brain design and
  tests prove primary key compatibility + cross-bot `/resume` by real user.
- Ask user when: upstream uses a different session-key or account model.

Verification:

```bash
python -m py_compile gateway/session.py gateway/config.py gateway/platforms/base.py gateway/authz_mixin.py gateway/slash_commands.py gateway/run.py plugins/platforms/telegram/adapter.py
scripts/run_tests.sh tests/fork/test_multi_telegram_accounts.py tests/gateway/test_background_process_notifications.py tests/gateway/test_resume_command.py tests/gateway/test_restart_notification.py tests/gateway/test_runner_fatal_adapter.py tests/gateway/test_platform_reconnect.py -q
scripts/run_tests.sh tests/gateway/test_telegram_auth_check.py tests/gateway/test_telegram_callback_auth_fail_closed.py -q
scripts/run_tests.sh tests/fork -q
```

CI uses `scripts/run_tests.sh` with the default `tests/` discovery root, so
`tests/fork/test_multi_telegram_accounts.py` is collected automatically.
2026-07-13 pre-commit baseline: focused runtime `172 passed`, cache/auth
`28 passed`, complete fork suite `424 passed`, py_compile and diff-check passed.

Feature docs: `docs/chantxu64/multi-telegram-accounts/README.md`

Upstream status: fork-only (related open PRs/issues exist, not merged as equivalent).

### 15. Telegram tool-progress literal-text rendering

Status: active

Date: 2026-07-16

Files:

- `gateway/run.py`
- `plugins/platforms/telegram/adapter.py`
- `tests/fork/test_telegram_tool_progress_literal_text.py`
- `tests/gateway/test_run_progress_topics.py`
- `tests/gateway/test_telegram_rich_messages.py`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- Every Telegram tool-progress bubble treats dynamic tool arguments as literal
  text. Terminal also retains the fork's compact one-line status style instead
  of a fenced Markdown command block.

What changed:

- `GatewayRunner` marks only Telegram tool-progress sends and edits as
  `plain_text`, retaining topic/reply metadata and leaving typing indicators,
  approvals, final replies, and other platforms unchanged.
- The Telegram adapter bypasses both rich-message delivery and MarkdownV2
  conversion for that marker, including finalized accumulated bubbles and
  overflow continuations. Regexes, code fragments, URLs, backticks, pipes, and
  spoiler-like tokens therefore display literally.
- A fork-protection test keeps the Telegram-only metadata contract and literal
  send/edit behavior visible during future upstream merges.
- Telegram terminal command previews normalize whitespace while retaining the
  `terminal` tool label, so multi-line shell commands remain one persistent
  status line (for example, `💻 terminal: set -euo pipefail ...`).
- Other Markdown-capable platforms retain their existing fenced terminal
  progress rendering, including full-command verbose mode and consecutive-call
  header collapsing.

Why it matters:

- Tool previews contain machine-generated arguments. A regex beginning with
  triple backticks can open a code block, and paired pipes can create Telegram
  spoiler/blur styling; accumulating several progress lines can make such
  delimiters interact across a single edited bubble.

Merge protection:

- Preserve when: Telegram tool-progress still routes dynamic arguments through
  a Markdown or rich-message parser without an equivalent literal-text guard.
- Drop when: upstream supplies equivalent all-tool Telegram literal delivery
  with coverage for normal sends, edits, and overflow continuation.
- Ask user when: upstream introduces a platform-wide message-kind or
  per-tool-display system with different progress metadata semantics.

Verification:

```bash
.venv/bin/python -m pytest tests/fork/test_telegram_tool_progress_literal_text.py -q -o 'addopts='
.venv/bin/python -m pytest tests/gateway/test_run_progress_topics.py -q -o 'addopts='
.venv/bin/python -m pytest tests/gateway/test_telegram_rich_messages.py -q -o 'addopts='
.venv/bin/python -m py_compile gateway/run.py plugins/platforms/telegram/adapter.py tests/fork/test_telegram_tool_progress_literal_text.py tests/gateway/test_run_progress_topics.py tests/gateway/test_telegram_rich_messages.py
git diff --check
```

Feature docs: none — localized gateway/Telegram display behavior with merge
guidance and verification captured in this index entry.

Upstream status: fork-only.

### 16. Prompt execution-contract deduplication

Status: active

Date: 2026-07-16

Files:

- `agent/prompt_builder.py`
- `agent/system_prompt.py`
- `tests/agent/test_prompt_builder.py`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- The universal task-completion block is now a bounded execution contract, and
  the model-family tool-use block only enforces same-response follow-through.

What changed:

- The universal contract keeps real delivery, prerequisites, reasonable
  in-scope recovery, and anti-fabrication while adding explicit goal, scope,
  authorization, material-ambiguity, and sufficient-evidence stop boundaries.
- `TOOL_USE_ENFORCEMENT_GUIDANCE` no longer repeats unbounded completion or
  persistence pressure; it only prevents promise-only turns when a tool action
  is stated.
- Gemini/Gemma guidance no longer adds a second `Keep going` instruction.
- `OPENAI_MODEL_EXECUTION_GUIDANCE`, Memory/Skill guidance, tool descriptions,
  and their write-gate implementation are intentionally unchanged by this
  source modification.

Why it matters:

- Repeated persistence language can overweight continued action after the
  user's requested result is already supported by sufficient evidence.
- Clarification must also cover material changes to authorization, scope,
  acceptance criteria, and user-visible effects, even when the same tool would
  be used.

Merge protection:

- Preserve when: upstream still distributes completion, stop, and
  same-response tool-follow-through policy across overlapping prompt blocks.
- Drop when: upstream supplies an equivalent or stronger bounded execution
  contract without weakening real delivery, grounding, or anti-fabrication.
- Ask user when: upstream redesigns the GPT/Codex execution overlay or moves
  authorization and stop policy into a different runtime-enforced layer.

Verification:

```bash
.venv/bin/python -m pytest tests/agent/test_prompt_builder.py -q -o 'addopts='
.venv/bin/python -m pytest tests/run_agent/test_run_agent.py::TestToolUseEnforcementConfig tests/run_agent/test_run_agent.py::TestTaskCompletionGuidance -q -o 'addopts='
.venv/bin/python -m py_compile agent/prompt_builder.py agent/system_prompt.py tests/agent/test_prompt_builder.py
git diff --check
```

Feature docs: none — prompt behavior and merge guidance are captured by the
behavior-contract tests and this index entry.

Upstream status: fork-only.

### 17. Self-contained Clarify decision cards

Status: active

Date: 2026-07-16

Files:

- `tools/clarify_tool.py`
- `tests/tools/test_clarify_tool.py`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- Clarify tool calls must carry enough context in the rendered question for the
  user to make the decision without unseen or earlier assistant prose.

What changed:

- The tool schema now explains that messaging UIs may render Clarify as a
  standalone card.
- Action and approval questions must briefly state the current situation,
  proposed action and scope, material impact or trade-off, and a recommendation
  when one exists.
- References such as `above`, `earlier`, or `the recommended scope` cannot stand
  in for the omitted context.
- Selectable answers remain separate `choices`; the tool parameters, callback,
  Gateway flow, and platform adapters are unchanged.

Why it matters:

- A real Clarify call asked whether to apply “the recommended scope” while its
  assistant message contained no visible prose. The user could not know what
  was being approved and had to ask for the recommendation separately.

Merge protection:

- Preserve when: upstream Clarify guidance still permits context-dependent
  questions that messaging surfaces can render alone.
- Drop when: upstream supplies an equivalent or stronger self-contained
  decision-card contract while keeping choices independently selectable.
- Ask user when: upstream replaces the single question string with structured
  context, impact, recommendation, or approval fields.

Verification:

```bash
.venv/bin/python -m pytest tests/tools/test_clarify_tool.py -q -o 'addopts='
.venv/bin/python -m py_compile tools/clarify_tool.py tests/tools/test_clarify_tool.py
git diff --check
```

Feature docs: none — this is model-visible tool guidance with behavior-contract
coverage and no platform API change.

Upstream status: fork-only.

### 18. First browser navigation opens a fresh tab

Status: active

Date: 2026-07-16

Files:

- `tools/browser_tool.py`
- `tests/fork/test_browser_first_conversation_tab.py`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- The first `browser_navigate` call in each conversation opens and activates a
  new tab before loading the requested URL; later calls keep their existing
  navigation behavior.

What changed:

- A conversation marker keyed by the stable task/session ID survives the normal
  per-turn Browser resource cleanup.
- Calls for the same task/session ID are serialized through the complete
  `browser_navigate` result, so the first `tab new`/`open` and later navigations
  cannot interleave; different conversations retain independent locks.
- On the first call only, `browser_navigate` runs `tab new` before its existing
  `open <url>` command. `agent-browser` activates the new tab as part of that
  command.
- The tool description states this actual first-call behavior. The earlier
  live-CDP warning text and the unrelated `browser_vision` prompt override were
  removed.
- This does not bind later backend reconnects to the created tab. Subsequent
  target selection remains unchanged, matching the intentionally minimal scope.

Why it matters:

- The first navigation in a new conversation must not replace a useful page
  that was already open in the connected browser.

Merge protection:

- Preserve the one-time marker separately from backend session `_first_nav`,
  because Browser resources are cleaned after every agent turn.
- Preserve the command order `tab new` then `open <url>` on the first call and
  plain `open <url>` on later calls in the same conversation.

Verification:

```bash
.venv/bin/python -m pytest tests/fork/test_browser_first_conversation_tab.py -q -o 'addopts='
.venv/bin/python -m py_compile tools/browser_tool.py tests/fork/test_browser_first_conversation_tab.py
git diff --check
```

Feature docs: none — the behavior is confined to one tool and covered by a
focused runtime test plus this merge note.

Upstream status: fork-only.


### 19. Clarify attachment replies preserve media paths

Status: active

Date: 2026-07-28

Files:

- `gateway/run.py`
- `tools/clarify_gateway.py`
- `tools/clarify_tool.py`
- `tests/gateway/test_clarify_active_session_bypass.py`
- `tests/tools/test_clarify_gateway.py`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- A pending Gateway Clarify preserves attachment paths as separate response
  context without corrupting the user's canonical text or selected choices.

What changed:

- The Gateway passes the raw typed reply and media context separately to the
  resolver. Numeric, label, and multi-select replies are normalized before the
  context is attached, so `user_response` keeps its original string/list shape.
- `ClarifyResponsePayload` carries the canonical response and optional context
  through the blocking callback. `clarify_tool` exposes that context as a
  separate `response_context` field only when an attachment was present.
- Clarify media placeholders translate host cache paths with
  `to_agent_visible_cache_path()`, so Docker-backed agents receive mounted
  `/root/.hermes/cache/...` paths rather than unreadable host paths.
- Open-ended Clarifies accept text-plus-media and media-only replies. Typed
  choice replies can carry media without changing the selected option. A
  choice prompt with media but no actual selection remains unresolved.
- Text-only replies, native button callbacks, slash-command bypass, timeout,
  and existing queue/vision placeholders remain unchanged.

Why it matters:

- Without the media context, an agent asked to “use this attachment” may search
  the filesystem and pick a stale file. If media is concatenated before choice
  parsing, numeric and multi-select answers stop resolving; if host paths are
  exposed to a Docker agent, the correct file is still unreadable.

Merge protection:

- Preserve until upstream's pending-Clarify interception carries agent-visible
  attachment paths in a field separate from canonical choice/text responses.
- Do not move this after normal media processing: the active agent is blocked
  waiting for the Clarify answer, so the early interception must retain the
  media context itself.
- Preserve the string-only callback path when no attachment is present, so
  native platform button adapters remain backward compatible.

Verification:

```bash
.venv/bin/python -m pytest tests/gateway/test_clarify_active_session_bypass.py tests/tools/test_clarify_gateway.py tests/tools/test_clarify_tool.py -q -o 'addopts='
.venv/bin/python -m py_compile gateway/run.py tools/clarify_gateway.py tools/clarify_tool.py tests/gateway/test_clarify_active_session_bypass.py tests/tools/test_clarify_gateway.py
git diff --check
```

Feature docs: none — this is a narrow Gateway interception contract covered by
runtime regression tests and this merge note.

Upstream status: fork-only.


### 20. Credential cooldown intentional-clear persistence

Status: active fork maintenance

Date: 2026-07-28

Files:

- `agent/credential_pool.py`
- `hermes_cli/auth.py`
- `tests/fork/test_codex_credential_pool.py`
- `tests/fork/test_hermes_state_transcript.py` (upstream merge compatibility)
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- A live Codex quota probe that proves an observed 429 cooldown stale now
  clears that exact status generation in `auth.json`, while a newer cooldown
  written concurrently by another process remains protected.

What changed:

- `CredentialPool._available_entries()` records the `last_status` and
  `last_status_at` of every cooldown it intentionally clears and forwards
  those preconditions through `_persist()`.
- `write_credential_pool()` accepts the preconditions at its existing locked
  read/merge/write boundary. It bypasses stale-snapshot cooldown adoption only
  when the disk row still matches the exact observed status generation.
- A disk row with a different status or timestamp continues through upstream's
  normal `_merge_disk_cooldown_state()` path, so a concurrent newer 429/DEAD
  quarantine cannot be erased by the probe result.
- Both the fork's throttled Codex usage probe and upstream's quota-restored
  probe use the same persistence contract.
- The fork transcript timestamp regression now supplies explicit message
  timestamps. Upstream compression-lock enforcement legitimately samples the
  same module clock inside each append, so globally replacing `time.time()`
  with a two-value iterator no longer represented the behavior under test.

Merge protection:

- Do not replace the generation-matched clear with an unconditional
  `cleared_ids` bypass; that would reintroduce the cross-process lost-update
  bug upstream's cooldown merge prevents.
- Preserve until upstream's credential-pool writer distinguishes an
  intentional, evidence-backed status clear from a stale healthy snapshot.

Verification:

- RED before production edits: the existing fork probe and a new upstream
  probe regression both left `auth.json` at `last_status=exhausted`; the
  concurrent-newer-cooldown control already passed.
- Focused post-fix credential/state and upstream cooldown-merge regressions:
  `11 passed`.
- Complete fork gate: `534 passed` with `8` third-party deprecation warnings.
- Credential-pool/auth adjacent regressions: `195 passed`.
- Ruff, `py_compile`, and `git diff --check` passed.

Upstream status: the conflicting quota-clear and stale-snapshot protection
paths are both still present in the current upstream baseline; this fork adds
the missing concurrency-safe bridge between them.


### 21. Delivery-ledger session-reset boundary

Status: active fork maintenance

Date: 2026-07-29

Files:

- `gateway/delivery_ledger.py`
- `gateway/slash_commands.py`
- `tests/gateway/test_delivery_ledger.py`
- `tests/gateway/test_session_model_reset.py`
- `website/docs/user-guide/messaging/index.md`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- A successful explicit `/new` or `/reset` now transitions undelivered final
  responses for the replaced conversation route to terminal `superseded`
  state. Gateway startup recovery cannot inject those old answers into the
  fresh Hermes session.
- Normal same-session crash/restart redelivery remains unchanged. The boundary
  update is route-scoped, leaves other chats untouched, and is terminal against
  late send acknowledgements or failures from the old turn.

Why it matters:

- Upstream's delivery ledger persists `session_key`, which identifies a stable
  platform route, but not the Hermes session generation behind that route.
  `/new` intentionally reuses the route, so a failed old response could be
  recovered after restart even though the user had explicitly started fresh.

Merge protection:

- Preserve the explicit session-boundary transition until upstream associates
  delivery obligations with the originating Hermes session or provides an
  equivalent terminal supersession mechanism.
- Do not disable ordinary startup redelivery or remove the recovered-reply
  ambiguity marker as a substitute for this boundary check.

Verification:

- Regression tests cover route-scoped supersession, both `/new` and `/reset`,
  startup-sweep exclusion, and late-state-update terminality.

Feature docs: none — this is a narrow lifecycle invariant covered by the
delivery documentation, regression tests, and this merge note.

Upstream status: the durable delivery ledger is upstream; the explicit
session-reset boundary is fork-only.


### 22. Transport disconnect classification stays out of context compression

Status: active

Date: 2026-07-29

Files:

- `agent/error_classifier.py`
- `agent/conversation_loop.py`
- `tests/agent/test_error_classifier.py`
- `tests/agent/test_thinking_timeout_guidance.py`
- `tests/fork/test_transport_disconnect_classification.py`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- A status-less network or stream disconnect remains a retryable transport
  timeout regardless of session size; it no longer triggers context compression
  from token-count or message-count pressure alone.

What changed:

- Disconnect signatures such as `peer closed connection`, `server
  disconnected`, `unexpected eof`, and `incomplete chunked read` return
  `FailoverReason.timeout` with `should_compress=False` when no HTTP status is
  available.
- Removed the prior inference that a disconnect above 60% of the configured
  context window, above an absolute token threshold, or above a message-count
  threshold was evidence of `context_overflow`.
- Explicit provider context-overflow responses still return
  `FailoverReason.context_overflow` with `should_compress=True`.
- The existing generic HTTP 400 large-request heuristic and proactive pre-API
  context-size compression remain unchanged.
- Reasoning-model-specific timeout guidance remains a presentation/recovery
  layer after the common transport classification; it no longer changes the
  base classifier result.

Why it matters:

- A dropped connection proves that transport failed, but session size alone
  does not prove the provider rejected the request for excessive context.
- Misclassifying a network interruption as context overflow can start an
  unnecessary LCM compression cycle and ultimately report a misleading
  `Cannot compress further` result instead of the original network failure.

Merge protection:

- Preserve when: upstream still converts a status-less disconnect into context
  overflow based only on estimated tokens or message count.
- Drop when: upstream provides an equivalent separation between transport
  failures and explicit or locally confirmed context overflow, with regression
  coverage for both paths.
- Ask user when: upstream adds a typed provider/gateway signal that can reliably
  distinguish an oversized-request disconnect from an ordinary network drop.

Verification:

```bash
./venv/bin/python -m pytest tests/fork/test_transport_disconnect_classification.py -q -o 'addopts='
./venv/bin/python -m pytest tests/agent/test_error_classifier.py tests/agent/test_thinking_timeout_guidance.py -q -o 'addopts='
./venv/bin/python -m pytest tests/fork -q -o 'addopts='
./venv/bin/ruff check agent/error_classifier.py agent/conversation_loop.py tests/agent/test_error_classifier.py tests/agent/test_thinking_timeout_guidance.py tests/fork/test_transport_disconnect_classification.py
git diff --check
```

2026-07-29 verification results:

- Fork-owned disconnect contract: `5 passed`.
- Error-classifier and thinking-timeout suites: `257 passed`.
- Adjacent API recovery/failover suites: `169 passed`.
- Complete fork gate: `552 passed`, with 8 third-party deprecation warnings.
- Ruff, `py_compile`, and `git diff --check` passed.

Feature docs: none — this is a localized classifier/recovery-routing contract
covered by upstream-adjacent tests, a fork-owned regression suite, and this
merge note.

Upstream status: fork-only; current upstream still contains the disconnect plus
large-session context-overflow inference.


### 23. Auditable autonomous built-in memory governance

Status: fork-only; active after a process loads this version

Date: 2026-08-08

Files:

- `tools/memory_tool.py`
- `agent/background_review.py`
- `agent/prompt_builder.py`
- `agent/tool_executor.py`
- `agent/agent_runtime_helpers.py`
- `tests/agent/test_prompt_builder.py`
- `tests/agent/test_memory_write_bridge.py`
- `tests/fork/test_memory_changelog_governance.py`
- `tests/tools/test_memory_tool.py`
- `tests/tools/test_memory_tool_schema.py`
- `tests/tools/test_write_approval.py`
- `tests/run_agent/test_run_agent.py`
- `docs/chantxu64/memory-change-governance/README.md`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- Keep autonomous `MEMORY.md` / `USER.md` maintenance while recording the
  evidence and exact semantic loss behind every public mutation.

What changed:

- The public memory tool requires reason/evidence metadata, writes exact
  before/after events to profile-scoped `MEMORY_CHANGELOG.jsonl`, classifies
  removal as `safe`, `expired`, or `forced_capacity`, and groups batch operations
  with a transaction ID. The target lock remains held through journaling, and a
  checked disk snapshot is required before mutation. Journal failures roll back
  the write; if a non-cooperating manual writer changed the file again, the newer
  bytes are preserved instead of being erased by a stale rollback.
- Sequential and concurrent live dispatch both forward single-operation
  governance metadata.
- Background memory review receives only the latest on-disk `MEMORY.md` and
  `USER.md` in its uncached user message after per-entry threat scanning and JSON
  data encoding. It never receives the full audit log. Pure adds skip history;
  existing-entry changes use the read-only, 8,000-character-bound
  `memory(action="history", ...)` lookup, which follows the newest producer
  backward and includes only related JSONL lineage. A transaction ID records
  atomic submission only: ordinary operations in the same batch do not become
  each other's history; only operations explicitly marked `merge` share merge
  lineage. The existing memory/Skill-only
  runtime whitelist remains unchanged, and no SOUL files are appended to this
  governance context.
- Main-agent guidance, background review prompts, and the memory tool schema
  distinguish actually observed incidents/user corrections from merely preventive
  concerns. Unobserved risks cannot be recorded as lessons, and generic safety
  precautions are not saved solely because they seem important. Repository
  implementation designs, architecture notes, and fork-only behavior already
  documented in repository docs are also excluded; only a short pre-load trigger
  may remain when needed to select the correct Skill.
- Prompts require actual `skill_view` inspection before treating a Skill as the
  reliable carrier of a procedure; pre-load triggers may remain in memory.

Why it matters:

- Tight character limits previously encouraged untyped deletion of "stale or
  less important" entries without retaining the original problem or known loss.
- The user wants background review to continue managing memory itself, including
  last-resort eviction, without silently erasing why an older lesson existed.

Merge protection:

- Preserve when: upstream still lacks equivalent per-change audit history,
  deletion typing, sanitized live memory-review context, metadata-preserving
  dispatch, and journal-failure rollback without stale-snapshot data loss.
- Drop when: upstream provides an equivalent autonomous, evidence-backed memory
  governance mechanism.
- Ask user when: a replacement requires manual approval, broad file access, or
  injection of SOUL/SOUL_CHANGELOG into background memory review.

Verification:

```bash
./venv/bin/python -m pytest -q -o 'addopts=' tests/agent/test_prompt_builder.py tests/agent/test_memory_write_bridge.py tests/fork/test_memory_changelog_governance.py tests/tools/test_memory_tool.py tests/tools/test_memory_tool_schema.py tests/tools/test_write_approval.py tests/run_agent/test_run_agent.py::TestExecuteToolCalls tests/run_agent/test_background_review_cache_parity.py tests/run_agent/test_background_review_toolset_restriction.py tests/test_background_review_list_shapes.py tests/test_background_review_session_isolation.py
```

Feature docs: `docs/chantxu64/memory-change-governance/README.md`

Upstream status: fork-only.


### 24. Launchd gateway open-file ceiling

Status: superseded by upstream (2026-08-11 sync)

Date: 2026-08-10; superseded 2026-08-11

Files:

- `hermes_cli/gateway.py` — now upstream `SoftResourceLimits` via `runtime.nofile_soft_limit`
- `hermes_cli/resource_limits.py` — upstream configurable floor
- `tests/fork/test_launchd_open_file_limit.py` — fork regression that the official configurable path still emits/omits correctly
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- The old fork hard-coded `NumberOfFiles = 8192` in `generate_launchd_plist()`.
- Upstream now provides the same launchd embedding through
  `configured_nofile_soft_limit()` / `runtime.nofile_soft_limit`, plus an
  in-process soft-limit raise. This fork dropped the hard-coded block and keeps
  only a thin regression that the official path still writes the plist key.
- Operator config on this machine: `runtime.nofile_soft_limit: 65536` in
  `~/.hermes/config.yaml` (higher than upstream default 4096).

What changed:

- Removed fork-only hard-coded `SoftResourceLimits` block after merging
  upstream `fix(gateway): persist RLIMIT_NOFILE floor into the generated launchd plist`.
- Updated `tests/fork/test_launchd_open_file_limit.py` to assert the official
  configurable accessor, not the retired constant 8192.

Why it matters:

- Upstream covers the original EMFILE failure mode with one shared config knob.
- Keeping a second hard-coded block would emit duplicate SoftResourceLimits and
  fight plist rewrites.

Merge protection:

- Preserve when: only the fork regression that official configurable emission
  still works; do not reintroduce a hard-coded 8192 block.
- Drop when: upstream's own tests already cover the same emission contract and
  the fork regression is pure duplication.
- Ask user when: a replacement removes launchd SoftResourceLimits entirely or
  lowers the operator-chosen floor without an equivalent guarantee.

Verification:

```bash
./venv/bin/python -m pytest tests/fork/test_launchd_open_file_limit.py tests/hermes_cli/test_gateway_service.py -q -o 'addopts=' -k nofile
./venv/bin/python -m py_compile hermes_cli/gateway.py hermes_cli/resource_limits.py tests/fork/test_launchd_open_file_limit.py
git diff --check
```

### 25. OpenAI API Codex-compatible gateway integration

Status: active

Date: 2026-08-14; Codex watchdog parity 2026-08-15

Files:

- `agent/auxiliary_client.py`
- `agent/model_metadata.py`
- `agent/chat_completion_helpers.py`
- `run_agent.py`
- `agent/transports/codex.py`
- `agent/turn_context.py`
- `plugins/image_gen/openai-codex/__init__.py`
- `tests/plugins/image_gen/test_openai_codex_provider.py`
- `tests/fork/test_openai_api_codex_gateway.py`
- `tests/fork/test_codex_request_only_memory_context.py`
- `tests/fork/test_image_gen_openai_api.py`
- `tests/agent/test_non_stream_stale_timeout.py`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- `openai-api` now works as one identity across chat, auxiliary tasks, context sizing, and image generation when `OPENAI_BASE_URL` targets a Codex-compatible API-key gateway.

What changed:

- The existing Codex image plugin still registers `openai-codex` for official OAuth and now also registers `openai-api`.
- The `openai-api` image backend resolves the same API key and base URL as chat and posts Codex Responses `image_generation` calls there.
- Auxiliary clients with no explicit `api_mode` now consult the provider-declared transport, so `openai-api` uses its Responses adapter instead of sending the generic/OpenRouter-shaped `reasoning.enabled` body to the configured endpoint.
- OpenAI-compatible `/models` metadata now preserves `owned_by`; when the same response identifies itself as `codexmanager`, Hermes reads the current model's exact `slug/context_window` from that response's `models` extension.
- Exact CodexManager context values are retained in `~/.hermes/endpoint_context_cache.json`, scoped by normalized endpoint, API-key SHA-256 fingerprint, and model. A newer exact value replaces the old one; a partial/failed catalog reuses the last valid value without resurrecting cached models in the live model list.
- The five-minute in-memory endpoint catalog uses the same credential scope, so two API keys on one CodexManager URL cannot reuse each other's 272K/372K metadata.
- For custom `openai-api` endpoints, the public context resolver consults live/credential-scoped metadata before the legacy `model@base_url` cache; official OpenAI URLs and other providers retain the prior cache order.
- Main-session Codex transport sends the same three cache-routing headers (`session_id` / `thread-id` / `x-client-request-id`) to a custom `openai-api` Codex-compatible gateway as it sends on `openai-codex` (upstream only sends two of them), via a dedicated `use_codex_cache_headers` flag. Unlike the official backend, the custom gateway still receives `max_output_tokens` and keeps its endpoint-specific encrypted-reasoning issuer (reasoning blobs are sealed per endpoint; stamping a custom gateway as the shared `codex_backend` issuer would let a gateway switch replay foreign blobs → HTTP 400 `invalid_encrypted_content`). Native server-side compaction (`context_management`) stays limited to official OpenAI/ChatGPT routes so a custom gateway never receives that field.
- Request-only memory recall on `openai-api` Codex Responses routes is injected as a trailing `role=developer` input item after the current user message (same shape as `openai-codex`), instead of being appended into the user content.
- The non-streaming stale-call policy treats `openai-api` Codex Responses routes as Codex backends. Large requests therefore receive the existing Codex context floors (900 seconds above 50K estimated tokens and 1200 seconds above 100K) in both interactive and direct Cron/subagent call paths instead of the generic 150/240-second ceilings.

Why it matters:

- The user selects `openai-api` across all configured surfaces. Before this change image generation could not resolve that name, auxiliary calls used an incomplete request-shaping path, and Hermes ignored the CodexManager API's own context-window catalog and misreported this route as 1.05M.

Merge protection:

- Preserve when: an API-key Codex-compatible endpoint is still selected through `openai-api` and upstream lacks equivalent image registration, auxiliary transport inheritance, live CodexManager context-catalog handling, credential-scoped last-valid context retention, or Codex large-context watchdog parity across interactive and direct call paths.
- Drop when: upstream provides all five behaviors with equivalent tests.
- Ask user when: upstream changes `openai-api` transport or timeout semantics, image generation routing, the CodexManager enriched model catalog no longer provides authoritative context windows, or a durable endpoint cache adopts different deletion/expiry semantics.

Verification:

- 2026-08-15 watchdog-parity review: shared backend classification, the direct
  Responses path, and the non-Codex Chat Completions boundary were checked;
  adjacent timeout, TTFB, wait-state, `openai-api`, and Responses regressions
  reported `92 passed`. Ruff, `py_compile`, and `git diff --check` also passed.

```bash
./venv/bin/python -m pytest \
  tests/fork/test_openai_api_codex_gateway.py \
  tests/fork/test_codex_request_only_memory_context.py \
  tests/fork/test_image_gen_openai_api.py \
  tests/plugins/image_gen/test_openai_codex_provider.py \
  tests/agent/test_auxiliary_client.py \
  tests/agent/test_model_metadata.py \
  tests/agent/test_non_stream_stale_timeout.py \
  tests/hermes_cli/test_model_switch_openai_api_mode.py \
  -q -o 'addopts='
./venv/bin/python -m py_compile \
  agent/auxiliary_client.py agent/model_metadata.py \
  agent/chat_completion_helpers.py run_agent.py agent/transports/codex.py \
  agent/turn_context.py \
  plugins/image_gen/openai-codex/__init__.py \
  tests/fork/test_openai_api_codex_gateway.py \
  tests/fork/test_codex_request_only_memory_context.py \
  tests/fork/test_image_gen_openai_api.py
```

Feature docs: none — one provider integration across existing generic seams, with focused regressions and full merge guidance here.

Upstream status: fork-only.

### 26. Successful STT keeps an explicit voice-origin marker

Status: active

Date: 2026-08-15

Files:

- `gateway/run.py`
- `tests/gateway/test_stt_config.py`
- `tests/gateway/test_telegram_audio_vs_voice.py`
- `tests/gateway/test_telegram_voice_v0_regressions.py`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- Successful Gateway STT enrichment identifies the quoted transcript as content from a user voice message.

What changed:

- Successful non-empty transcripts are injected as `[The user sent a voice message~ Here's what they said: "<transcript>"]` instead of a bare quoted line.
- The raw `successful_transcripts` list remains unchanged, so transcript echo formatting, ordering, and deduplication retain their existing behavior.
- Empty/inaudible results, STT failures, disabled-STT notes, providers, downloads, and message routing are unchanged.

Why it matters:

- The model must know that the text came through speech recognition so it can account for possible recognition errors rather than treating every word as deliberately typed by the user.

Merge protection:

- Preserve the explicit voice-origin marker unless upstream provides equivalent source semantics for successful STT enrichment.
- Do not revive the prefix by changing transcript echo text or platform-specific adapters; the protected behavior belongs to the common Gateway enrichment path.
- Keep empty and failed transcription markers neutral and singular.

Verification:

- 2026-08-15: the three focused regression files reported `10 passed`; the broader Gateway voice/STT selection reported `124 passed, 1 skipped`. Ruff, `py_compile`, and `git diff --check` passed. Warnings were existing third-party deprecations plus two pre-existing unawaited-`AsyncMock` warnings in voice-channel tests.

```bash
./venv/bin/python -m pytest \
  tests/gateway/test_stt_config.py \
  tests/gateway/test_telegram_audio_vs_voice.py \
  tests/gateway/test_telegram_voice_v0_regressions.py \
  -q -o 'addopts='
./venv/bin/python -m py_compile gateway/run.py \
  tests/gateway/test_stt_config.py \
  tests/gateway/test_telegram_audio_vs_voice.py \
  tests/gateway/test_telegram_voice_v0_regressions.py
./venv/bin/ruff check gateway/run.py \
  tests/gateway/test_stt_config.py \
  tests/gateway/test_telegram_audio_vs_voice.py \
  tests/gateway/test_telegram_voice_v0_regressions.py
git diff --check
```

Feature docs: none — this is a small message-enrichment compatibility rule fully described here and protected by focused regressions.

Upstream status: fork-only.

### 27. Per-invocation delegation provider/model/reasoning routing

Status: active

Date: 2026-08-15

Files:

- `tools/delegate_tool.py`
- `tools/async_delegation.py`
- `run_agent.py`
- `tests/tools/test_delegate.py`
- `tests/tools/test_delegate_control_actions.py`
- `tests/tools/test_async_delegation.py`
- `website/docs/user-guide/features/delegation.md`
- `website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/user-guide/features/delegation.md`
- `docs/chantxu64/delegate-per-call-routing/README.md`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- Each `delegate_task` invocation or batch item can select a provider, model, and reasoning effort, with cross-provider credentials/API-mode resolution, truthful same-model reasoning fallback, durable safe route metadata, and bounded Markdown retry suggestions.

What changed:

- Top-level and per-task `provider`, `model`, and `reasoning_effort` fields are exposed and forwarded through both model dispatch paths.
- Model-only calls infer a provider only when the authenticated curated inventory has one unique match; explicit provider/model calls resolve the target-model runtime route and reject known catalog mismatches before spawning.
- Routed children resolve reasoning configuration against the target model. An explicit effort is used only when the production request builder preserves it exactly; otherwise Hermes keeps the selected provider/model and applies that target model's normal override/global/provider reasoning configuration without claiming that the requested value took effect.
- Every batch task is route-validated before any child starts. Safe effective route metadata is present in child results, async status, SQLite task payloads, restart recovery, and completion events; secrets and raw request overrides are excluded.
- Model-route errors keep structured error codes and render bounded Markdown suggestions in the error text. They combine up to 10 recent frequently used routes with up to 10 name-similar routes, deduplicate by `(provider, model)`, and stay within 1,800 characters. Recent usage is read from the active profile's local `state.db` and intersected with the current authenticated curated inventory, so stale historical routes are excluded. No LLM, child launch, forced catalog refresh, reasoning-model scan, or automatic model substitution is involved; the full catalog is never inserted into the persistent tool schema.

Why it matters:

- The parent can deliberately use a different provider/model for one subtask without changing global configuration, while model-only calls remain convenient and ambiguous routes fail closed. Errors contain enough bounded current information to retry without a separate model-directory tool call or permanent prompt bloat.

Merge protection:

- Preserve when upstream does not provide the combined contract of per-invocation/per-task cross-provider routing, target-model reasoning resolution, truthful same-model automatic/default fallback for unsupported explicit effort, all-task prevalidation, durable safe route metadata, and bounded current-availability Markdown suggestions.
- Drop when upstream provides an equivalent public schema, routing precedence, validation behavior, persistence/recovery metadata, and error-result budget with matching regressions.
- Ask user when upstream offers a similar interface but silently chooses ambiguous providers, claims a clamped/mapped reasoning value was honored, switches models for reasoning, omits durable per-task routes, injects a full catalog into the schema, or uses materially different precedence/fallback semantics.

Verification:

- 2026-08-16 revised-contract validation: core delegate/control/async tests reported `125 passed in 16.72s`; adjacent DeepSeek/OpenCode Go/Codex request-builder tests reported `158 passed in 1.82s`; restoration, API Server, Gateway binding, CLI delivery, TUI lifecycle, batch/output-schema, and FD-leak tests reported `65 passed in 7.77s` with seven pre-existing third-party deprecation warnings. Ruff, `py_compile`, and `git diff --check` passed. A read-only live-profile candidate render excluded stale historical `openai-codex` routes and returned only current authenticated picker-inventory routes.

```bash
./venv/bin/python -m pytest \
  tests/tools/test_delegate.py \
  tests/tools/test_delegate_control_actions.py \
  tests/tools/test_async_delegation.py \
  -q -o 'addopts='
./venv/bin/python -m pytest \
  tests/plugins/model_providers/test_deepseek_profile.py \
  tests/plugins/model_providers/test_opencode_go_profile.py \
  tests/agent/transports/test_codex_transport.py \
  tests/agent/test_codex_request_transport_diagnostics.py \
  -q -o 'addopts='
./venv/bin/ruff check tools/delegate_tool.py tools/async_delegation.py \
  run_agent.py tests/tools/test_delegate.py \
  tests/tools/test_delegate_control_actions.py \
  tests/tools/test_async_delegation.py
./venv/bin/python -m py_compile tools/delegate_tool.py \
  tools/async_delegation.py run_agent.py tests/tools/test_delegate.py \
  tests/tools/test_delegate_control_actions.py \
  tests/tools/test_async_delegation.py
git diff --check
```

Feature docs: `docs/chantxu64/delegate-per-call-routing/README.md`

Upstream status: fork-only.

### 28. Current-turn context-aware Smart Approval

Status: active

Date: 2026-08-16

Files:

- `agent/agent_runtime_helpers.py`
- `agent/conversation_compression.py`
- `agent/tool_executor.py`
- `model_tools.py`
- `tools/approval.py`
- `tools/tirith_security.py`
- `tools/terminal_tool.py`
- `tools/code_execution_tool.py`
- `tests/tools/test_smart_approval_context.py`
- `tests/tools/test_smart_approval_injection.py`
- `tests/tools/test_smart_approval_policy.py`
- `tests/tools/test_execute_code_approval_cluster.py`
- `tests/tools/test_tirith_security.py`
- `docs/chantxu64/current-turn-smart-approval/README.md`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- Smart Approval now judges actual risk and current authorization separately,
  using only the latest real user-authored turn, completed Clarify
  question/answer pairs after that turn, the action about to run, and bounded
  contents of directly executed entry scripts.

What changed:

- Both tool-dispatch paths derive the same narrow authorization context and bind
  it per request, so concurrent tool calls cannot borrow another request's user
  authorization.
- Approval context reuses the conversation compressor's canonical real-user
  classification. Compaction summaries, preserved ToDo snapshots, background
  notifications, recovery notices, and bare Skill invocation scaffolding cannot
  become authorization evidence. Runtime-enriched real turns retain only the
  user instruction: Skill bodies, model-switch notices, reply/thread metadata,
  Cron delivery guidance, and pre-run/context-job output are stripped before
  review. A ToDo snapshot appended to a real turn is removed without discarding
  that turn.
- Terminal review receives the real command and resolved execution directory;
  `execute_code` review receives the complete Python source. Direct entry scripts
  such as `python cleanup.py`, `bash deploy.sh`, `./run-task`, and literal script
  launches inside `execute_code` are read from the environment that will execute
  them and sent as bounded evidence. Missing, unreadable, oversized, or excess
  direct-script evidence escalates instead of being silently approved.
- Interpreter stdin and shell here-doc forms such as `python - <<'PY'` are
  treated as inline command content rather than nonexistent external script
  paths. A genuine entry script before a here-doc, such as
  `python reader.py <<'EOF'`, is still collected and reviewed.
- Tirith resolution now validates the Hermes scanner protocol instead of
  trusting any same-named executable found on `PATH`. A binary must support the
  `check` interface and the `--json`, `--shell`, and `--non-interactive` flags;
  incompatible programs are skipped in favor of the managed Hermes binary.
- Package-managed virtual-environment console entry points are not treated as
  opaque custom scripts merely because their executable path is explicit.
  Ordinary test/lint/build commands can therefore pass the normal security
  guards without invoking Smart Approval when Tirith and static checks find no
  risk. Directly launched source scripts remain bounded review evidence, and
  unreadable custom scripts still escalate.
- Smart-review reasons are requested in the latest real user's natural
  language. Chinese sessions also receive localized risk/authorization
  summaries, denial and timeout safeguards, and terminal auto-approval notes;
  structured decision/risk/authorization values remain stable machine enums.
- The reviewer returns `decision`, `risk_level`, `authorization`, and a short
  semantic `reason`. Critical risk is denied; an approval whose risk and
  authorization fields conflict is downgraded to escalation; high-risk exact
  authorization remains eligible for one-operation approval.
- Existing integrations that compare the historical one-word smart decision
  remain compatible. Smart approvals still do not create a permanent broad
  allowlist entry.
- The first version intentionally omits file hashes/version binding, recursive
  dependency graphs, LSP integration, dynamic dependency analysis, and a new
  side-effect fingerprint system.

Why it matters:

- A destructive-looking command can be the exact operation the user just
  authorized, while an innocent-looking script launcher can hide broader side
  effects. The reviewer needs the current scope and direct script contents to
  distinguish those cases without inheriting unrelated history.

Merge protection:

- Preserve when upstream Smart Approval cannot consume the latest-turn/Clarify
  authorization boundary, structured risk and authorization fields, bounded
  direct-script evidence across terminal and `execute_code`, localized approval
  presentation, or protocol-validated Tirith resolution.
- Drop when upstream provides equivalent request isolation, direct-entry-script
  review, package-managed development-tool handling, fail-closed evidence gaps,
  structured outcomes, language-aware presentation, compatible-scanner
  selection, and one-operation persistence behavior with matching regressions.
- Ask the user when upstream uses broader conversation history, omits Clarify
  question scope, treats all high risk as denial, or recursively analyzes a
  materially larger dependency surface.

Verification:

- Approval, terminal, `execute_code`, and Tirith regression coverage:
  `206 passed`, `7 subtests passed`.
- Conversation-compression and real-user provenance coverage: `193 passed`.
- A live local resolver probe skipped the incompatible pipx `py-tirith` 1.0.5
  executable at `~/.local/bin/tirith`, selected the Hermes-managed 0.2.12
  scanner at `~/.hermes/bin/tirith`, and returned `allow` for an ordinary
  `python -m pytest` command.
- Direct evidence probes returned no custom-script evidence for a verified
  virtual-environment console entry point and retained an `unreadable` evidence
  gap for a missing directly launched custom Python script.
- A frozen 50-event historical replay completed serially through the configured
  `openai-api / gpt-5.6-luna` approval route with `medium` reasoning and a
  five-second inter-case delay. All 50 records matched their frozen event/action
  identities and route contract with no model-call failures. The replay is an
  evaluation set, not a claim that every model judgment is correct; manual
  review retained five judgment findings: two unnecessary low-risk read-only
  escalations and three unsafe approvals involving unknown interactive effects,
  an omitted backup overwrite, and an ambiguous target/overwrite scope.
- A separately authorized prompt-only experiment was rejected by its ratchet
  gate. After the five focus findings were initially corrected, 7 of the 45
  previously accepted anchor decisions changed. A final 12-case boundary pass
  met only 8 expected decisions. The experimental prompt and its text-contract
  tests were removed; the frozen R021 prompt hash again exactly matches the
  original 50-event baseline. The code-layer context and here-doc fixes remain.
- Ruff, `py_compile`, and `git diff --check` passed for the changed source and
  tests. Pyright was not installed in this worktree environment.
- This layered-fix validation used local tests only: no paid model replay,
  configuration change, Gateway restart, commit, or push was performed. The
  running Gateway must be restarted separately before these source changes can
  affect live approval requests.

Feature docs: `docs/chantxu64/current-turn-smart-approval/README.md`

Upstream status: fork-only.

## Current fork delta checklist

Compared with the upstream parent of the latest completed fork sync, active fork
deltas are expected in these areas:

- Hindsight Unicode support / manual session retain / synchronous cache-miss recall:
  - `.gitignore`
  - `agent/memory_manager.py`
  - `agent/memory_provider.py`
  - `plugins/memory/hindsight/__init__.py`
  - `hermes_state.py`
  - `tests/plugins/memory/test_hindsight_provider.py`
  - `tests/test_hermes_state.py`
  - `tests/agent/test_memory_session_switch.py`
  - `hermes_cli/commands.py`
  - `tests/hermes_cli/test_commands.py`
  - `cli.py`
  - `tests/fork/test_cli_retain_command.py`
  - `gateway/run.py`
  - `gateway/slash_commands.py`
  - `tests/fork/test_gateway_retain_command.py`
  - `tests/gateway/test_undo_rewind_session.py`
  - `tui_gateway/server.py`
  - `tests/tui_gateway/test_undo_command.py`
  - `docs/chantxu64/hindsight-manual-retain.md`
- Custom STT API / custom Qwen TTS API:
  - `tools/transcription_tools.py`
  - `tools/tts_tool.py`
  - `agent/transcription_registry.py`
  - `agent/tts_registry.py`
  - `tests/tools/test_transcription.py`
  - `tests/tools/test_transcription_dotenv_fallback.py`
  - `tests/fork/test_custom_qwen_tts.py`
- Successful STT voice-origin enrichment:
  - `gateway/run.py`
  - `tests/gateway/test_stt_config.py`
  - `tests/gateway/test_telegram_audio_vs_voice.py`
  - `tests/gateway/test_telegram_voice_v0_regressions.py`
  - `docs/LOCAL_MODIFICATIONS.md`
- Per-invocation delegation provider/model/reasoning routing:
  - `tools/delegate_tool.py`
  - `tools/async_delegation.py`
  - `run_agent.py`
  - `tests/tools/test_delegate.py`
  - `tests/tools/test_delegate_control_actions.py`
  - `tests/tools/test_async_delegation.py`
  - `website/docs/user-guide/features/delegation.md`
  - `website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/user-guide/features/delegation.md`
  - `docs/chantxu64/delegate-per-call-routing/README.md`
  - `docs/LOCAL_MODIFICATIONS.md`
- Current-turn context-aware Smart Approval:
  - `agent/agent_runtime_helpers.py`
  - `agent/conversation_compression.py`
  - `agent/tool_executor.py`
  - `model_tools.py`
  - `tools/approval.py`
  - `tools/tirith_security.py`
  - `tools/terminal_tool.py`
  - `tools/code_execution_tool.py`
  - `tests/tools/test_smart_approval_context.py`
  - `tests/tools/test_smart_approval_injection.py`
  - `tests/tools/test_smart_approval_policy.py`
  - `tests/tools/test_execute_code_approval_cluster.py`
  - `tests/tools/test_tirith_security.py`
  - `docs/chantxu64/current-turn-smart-approval/README.md`
  - `docs/LOCAL_MODIFICATIONS.md`
- Safe command rewrite:
  - `tools/safe_cmd_rewrite.py`
  - `tools/terminal_tool.py`
  - `tests/fork/test_safe_cmd_rewrite.py`
  - `pyproject.toml`
- Disable newly bundled skills by default when configured:
  - `tools/skills_sync.py`
  - `hermes_cli/main.py`
  - `tests/tools/test_skills_sync.py`
- Request-only recall isolation and Codex prompt-cache routing:
  - `agent/chat_completion_helpers.py`
  - `agent/transports/codex.py`
  - `agent/conversation_loop.py`
  - `agent/codex_responses_adapter.py`
  - `agent/turn_context.py`
  - `run_agent.py`
  - `gateway/run.py`
  - `gateway/session.py`
  - `gateway/slash_commands.py`
  - `hermes_cli/cli_commands_mixin.py`
  - `tests/agent/test_api_content_sidecar.py`
  - `tests/agent/test_gateway_turn_sidecar.py`
  - `tests/agent/transports/test_codex_transport.py`
  - `tests/gateway/test_replay_entry_fields.py`
  - `tests/run_agent/test_run_agent_codex_responses.py`
  - `tests/run_agent/test_codex_app_server_integration.py`
  - `tests/fork/test_codex_request_only_memory_context.py`
- Credential cooldown intentional-clear persistence:
  - `agent/credential_pool.py`
  - `hermes_cli/auth.py`
  - `tests/fork/test_codex_credential_pool.py`
  - `tests/fork/test_hermes_state_transcript.py`
- Delivery-ledger session-reset boundary:
  - `gateway/delivery_ledger.py`
  - `gateway/slash_commands.py`
  - `tests/gateway/test_delivery_ledger.py`
  - `tests/gateway/test_session_model_reset.py`
  - `website/docs/user-guide/messaging/index.md`
- Transport disconnect classification:
  - `agent/error_classifier.py`
  - `agent/conversation_loop.py`
  - `tests/agent/test_error_classifier.py`
  - `tests/agent/test_thinking_timeout_guidance.py`
  - `tests/fork/test_transport_disconnect_classification.py`
  - `docs/LOCAL_MODIFICATIONS.md`
- Clarify attachment reply context:
  - `gateway/run.py`
  - `tools/clarify_gateway.py`
  - `tools/clarify_tool.py`
  - `tests/gateway/test_clarify_active_session_bypass.py`
  - `tests/tools/test_clarify_gateway.py`
- Auditable autonomous built-in memory governance:
  - `tools/memory_tool.py`
  - `agent/background_review.py`
  - `agent/prompt_builder.py`
  - `agent/tool_executor.py`
  - `agent/agent_runtime_helpers.py`
  - `tests/agent/test_prompt_builder.py`
  - `tests/agent/test_memory_write_bridge.py`
  - `tests/fork/test_memory_changelog_governance.py`
  - `tests/tools/test_memory_tool.py`
  - `tests/tools/test_memory_tool_schema.py`
  - `tests/tools/test_write_approval.py`
  - `tests/run_agent/test_run_agent.py`
  - `docs/chantxu64/memory-change-governance/README.md`
  - `docs/LOCAL_MODIFICATIONS.md`
- Documentation:
  - `docs/LOCAL_MODIFICATIONS.md`
- Multi Telegram bots (account_id session slots):
  - `gateway/session.py`
  - `gateway/config.py`
  - `gateway/platforms/base.py`
  - `gateway/authz_mixin.py`
  - `gateway/slash_commands.py`
  - `gateway/run.py`
  - `plugins/platforms/telegram/adapter.py`
  - `tests/fork/test_multi_telegram_accounts.py`
  - `tests/gateway/test_background_process_notifications.py`
  - `tests/gateway/test_resume_command.py`
  - `docs/chantxu64/multi-telegram-accounts/README.md`
- First browser navigation opens a fresh tab:
  - `tools/browser_tool.py`
  - `tests/fork/test_browser_first_conversation_tab.py`
  - `docs/LOCAL_MODIFICATIONS.md`
- Telegram tool-progress literal-text rendering:
  - `gateway/run.py`
  - `plugins/platforms/telegram/adapter.py`
  - `tests/fork/test_telegram_tool_progress_literal_text.py`
  - `tests/gateway/test_run_progress_topics.py`
  - `tests/gateway/test_telegram_rich_messages.py`
  - `docs/LOCAL_MODIFICATIONS.md`
- Prompt execution-contract deduplication:
  - `agent/prompt_builder.py`
  - `agent/system_prompt.py`
  - `tests/agent/test_prompt_builder.py`
  - `docs/LOCAL_MODIFICATIONS.md`
- Self-contained Clarify decision cards:
  - `tools/clarify_tool.py`
  - `tests/tools/test_clarify_tool.py`
  - `docs/LOCAL_MODIFICATIONS.md`
- Launchd Gateway open-file ceiling (superseded by upstream configurable
  `runtime.nofile_soft_limit`; fork keeps emission regression only):
  - `tests/fork/test_launchd_open_file_limit.py`
  - `docs/LOCAL_MODIFICATIONS.md`
- OpenAI API Codex-compatible gateway integration:
  - `agent/auxiliary_client.py`
  - `agent/model_metadata.py`
  - `agent/chat_completion_helpers.py`
  - `run_agent.py`
  - `agent/transports/codex.py`
  - `agent/turn_context.py`
  - `plugins/image_gen/openai-codex/__init__.py`
  - `tests/plugins/image_gen/test_openai_codex_provider.py`
  - `tests/fork/test_openai_api_codex_gateway.py`
  - `tests/fork/test_codex_request_only_memory_context.py`
  - `tests/fork/test_image_gen_openai_api.py`
  - `tests/agent/test_non_stream_stale_timeout.py`
  - `docs/LOCAL_MODIFICATIONS.md`


## Summary statistics

Documented entries: 28 major entries.

Active / current entries: 24.

Historical reverted / abandoned / superseded areas: 4.

Fork-only non-merge commits represented here: see
`git log --no-merges upstream/main..HEAD`.

<!--
Add future modifications above this summary, under either:
- Active modifications
- Historical / reverted modifications
-->
