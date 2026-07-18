# Local Modifications — Hermes Agent Fork

Purpose: track deviations from upstream `NousResearch/hermes-agent` so future
upstream merges do not accidentally delete active fork behavior.

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

Date: 2026-05-22

Files:

- `plugins/memory/hindsight/__init__.py`
- `tests/plugins/memory/test_hindsight_provider.py`
- `tests/agent/test_memory_session_switch.py`
- `docs/LOCAL_MODIFICATIONS.md`

What changed:

- Hindsight auto-recall now has a bounded synchronous fallback when the prefetch
  cache is empty, so the first user turn in a new session or after compression can
  receive `<memory-context>` instead of waiting for the previous-turn prefetch.
- Added `recall_sync_on_cache_miss` and `recall_sync_timeout_seconds` provider
  settings. Defaults: enabled, 5 seconds.
- Background prefetch results are guarded by a generation counter so late results
  from an old query/session cannot overwrite newer recall context.
- Shared recall/reflect parameter handling now lives in a single helper used by
  both sync fallback and background prefetch.

Why it matters:

- The user expects `auto_recall=true` to include relevant Hindsight memory on the
  first turn of fresh sessions and compression-created continuation sessions.
- Compression/session switches must still clear stale recall, while allowing the
  next current query to recall safely.

Merge protection:

- Preserve generation checks when refactoring Hindsight prefetch; clearing
  `_prefetch_result` alone does not stop a late background thread from writing
  stale context.
- Preserve a short timeout for synchronous fallback; do not reuse the general
  Hindsight API timeout for first-turn recall.
- Preserve tests covering cache-miss sync recall, tools/auto_recall guards,
  reflect mode, and late prefetch generation discard.

Feature docs: `docs/chantxu64/hindsight-sync-cache-miss-recall/README.md`

Upstream status: fork-only.

### 9. Hindsight P5 recall preprocessor

Date: 2026-07-17

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
- `tests/fork/test_hindsight_provider_regressions.py`
- `tests/fork/test_codex_memory_context_isolation.py`
- `tests/hermes_cli/test_plugin_auxiliary_tasks.py`
- `tests/run_agent/test_run_agent_codex_responses.py`
- `tests/agent/test_auxiliary_client.py`
- `docs/chantxu64/hindsight-p5-recall-preprocessor/README.md`
- `docs/LOCAL_MODIFICATIONS.md`

What changed:

- Hindsight background prefetch now keeps the actual recall query and ordered
  result texts as a structured snapshot alongside the historical formatted
  cache string.
- The current user message, latest completed non-tool assistant response, and
  previous real recall are evaluated by the frozen P5 prompt through the
  standard `auxiliary.hindsight_recall_preprocessor` task. Its configurable
  provider/model/timeout defaults remain `openai-codex / gpt-5.6-luna / 30s`,
  with no generic model/provider fallback.
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
  null `new_query`. A non-null query preserves un-dropped old results and
  appends one new read-only recall. A null query clears current/next recall
  state and suppresses the same turn's legacy background prefetch.
- `MemoryManager` forwards the previous assistant message only to providers
  whose `prefetch` signature opts in, preserving legacy provider compatibility.
- Turn setup also checks the duck-typed manager's `prefetch_all` signature before
  passing the new keyword, preserving older manager substitutes and their memory
  context instead of silently dropping it on `TypeError`.
- The Codex stream consumer captures the terminal response's provider-reported
  model separately from the requested-model compatibility field; P5 validates
  the former before parsing output.
- Preprocessor or generated-recall failures conservatively restore the full old
  cache. Generation and session-switch guards clear and protect both cached
  representations; delayed queue work from an old session is rejected before
  recall and cannot repopulate a new session.

Why it matters:

- Short continuations such as “继续” and “修吧” can inherit a specific target
  from the previous assistant analysis without sending those phrases or the
  full assistant response directly to Hindsight.
- The user accepts occasional duplicate recall and conservative side-topic
  retention, but does not accept a hard coverage gate that suppresses memory
  needed for the next answer.

Merge protection:

- Preserve P5 prompt SHA-256
  `7dfade51638396003e6332f7dbb8da45698d03d715d1d54b2f51658d2edbfa09`
  unless the user explicitly approves and evaluates a successor.
- Preserve the explicit configurable/no-fallback auxiliary task, its evaluated
  Luna/30-second defaults, Codex provider-reported terminal-model validation,
  non-Codex `response.model` validation, and strict schema validation. Continue
  rejecting `auto`, `main`, bare `custom`, and their reserved `custom:*`
  canonical equivalents. Do not treat the Codex adapter's requested-model
  `.model` compatibility field as independent backend-model evidence.
- Keep query/result snapshots under the same generation and session lifecycle
  as `_prefetch_result`; do not preserve one cache representation without the
  other. Preserve session checks on queued prefetch before worker launch and
  before recall.
- Preserve fail-open restoration of old recall and the tools/auto_recall/
  shutdown guards.
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

### 9. Hindsight manual full-session retain

Status: active

Date: 2026-05-21

Files:

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
- `tests/fork/test_hindsight_retain_document_flow.py`
- `tests/gateway/test_undo_rewind_session.py`
- `tui_gateway/server.py`
- `tests/tui_gateway/test_undo_command.py`
- `docs/chantxu64/hindsight-manual-retain.md`

Summary:

- Adds a user-triggered `/retain` command that records a full Hindsight session document from Hindsight's provider-owned retain-turn SQLite store while preserving fork-specific Hindsight document lineage.

What changed:

- Added `hindsight_retain_session` / `/retain` for user-triggered Hindsight session retain.
- Hindsight `sync_turn()` now persists the exact same turn JSON used by automatic retain into a separate SQLite file: `$HERMES_HOME/hindsight/retain_turns.sqlite3`.
- When `MemoryManager` supplies the completed OpenAI-style `messages` transcript, Hindsight `sync_turn(..., messages=...)` rebuilds clean retain turns from that transcript before persisting. This preserves an earlier real user message when a gateway turn is interrupted by a later user correction before the final assistant response, while still filtering tool outputs, assistant tool-call shells, `[Recent Summary ...]`, `Operation interrupted:` notices, empty assistant messages, and intermediate assistant drafts.
- 2026-07-10 clean retain-turn filter expansion / 2026-07-16 async-result correction / 2026-07-18 Recent-Summary boundary correction: `sync_turn()` / `_build_turn_group_from_conversation_messages()` drop or strip synthetic runtime injections before writing `retain_turns.sqlite3`. Covered markers: `[Recent Summary ...]`, `[Session Arc Summary ...]`, `[Durable Summary ...]`, `[Depth-N Summary ...]`, `[Current user objective preserved from compacted history]`, `[Your active task list was preserved across context compression]`, `[Externalized payload: ...]`, `[ASYNC DELEGATION BATCH COMPLETE ...]`, `[ASYNC DELEGATION COMPLETE ...]`, `[OUT-OF-BAND USER MESSAGE ...]`, and the leading `[Note: model was just switched ... Adjust your self-identification accordingly.]` runtime note. For the model-switch note, only the exact leading marker is removed from plain user text or the first text part of a serialized multimodal message; the following real user text and all non-text parts are preserved. Async completion payloads are omitted while the last eligible user-visible assistant response is retained as an assistant-only event; it is not paired with a fabricated user utterance or a prior orphan user. A transcript window consisting only of a synthetic, exact LCM header `[Recent Summary (d0...)]` user row followed by the actual final assistant answer follows the same assistant-only rule; user-authored lookalikes such as `[Recent Summary request]` remain normal conversation content, while standalone Session-Arc/Durable/Depth summary turns remain fully dropped. Other pure-noise messages are skipped, and mixed messages keep only residual real user text. Scalar `sync_turn(user, assistant)` follows the same async/Recent-Summary rule. Assistant-side LCM/interrupt noise is dropped. Manual `/retain` re-sanitizes historical `turn_json` rows on load (clean-on-retain), stripping historical async payloads while preserving their visible assistant response. Filtering is marker-based, not business-topic-based.
- Before appending transcript-derived turns, `sync_turn()` mirrors active persisted rows from `$HERMES_HOME/hindsight/retain_turns.sqlite3` into the in-memory turn buffer. This prevents provider restart/compression replay from duplicating already-persisted active turns when the next sync receives the full transcript plus a new tail turn.
- If a rebuilt transcript is a full replay or a compressed tail window anchored to persisted turns but contains newly retainable async assistant-only events, replay reconciliation merges only the missing events into the full active history in transcript order, soft-deactivates the old local rows, writes one corrected active sequence while preserving matched root/child session ownership, and forces the next automatic retain (including a below-threshold buffer flushed by session switch) to use `replace` rather than `append`. Exact role/content anchors plus timestamped event identity prevent unrelated transcript divergence or legitimate repeated text from silently rewriting persisted history; an overlapping window with no exact anchor is kept out. Legacy rows with an empty `retain_document_id` are soft-deactivated through the resolved lineage fallback.
- Persisted retain rows include `retain_document_id`, a stable logical document id inherited across compression-created child sessions.
- Gateway/CLI `/retain` uses SessionDB only to resolve the active `session_id`, `parent_session_id`, and optional title; SessionDB transcript rows are not a content source.
- Provider-owned `$HERMES_HOME/hindsight/retain_turns.sqlite3` is the sole manual `/retain` content source. It stores the same turn JSON generated by Hindsight `sync_turn()` for automatic retain.
- Gateway/CLI `/retain` directly calls `retain_persisted_session_lineage(...)`; it must not call `retain_conversation_messages(...)`, `SessionDB.get_messages_as_conversation(...)`, or `SessionStore.load_transcript(...)` to build manual retain content.
- Manual retain groups persisted turns by stable `retain_document_id`; this preserves a single logical Hindsight document even when compression/session bookkeeping records continuation sessions as siblings rather than a clean parent chain.
- Persisted retain lookup uses only `active=1` rows, so `/undo`-rewound rows are skipped while retained for local audit. Rewind counts real user turns rather than raw persisted rows and also excludes trailing assistant-only async-result events belonging to the rewound suffix.
- If no persisted rows exist, manual `/retain` returns `No persisted turns to retain.` rather than falling back to SessionDB or LCM/compression summaries.
- Historical note: the 2026-06-15 SessionDB-primary path was added to preserve interrupted/orphan user messages, but it allowed LCM `[Recent Summary ...]` rows to replace Hindsight documents. Do not revive that path in merge conflict resolution.
- Parent-chain lookup remains a fallback for older local rows without `retain_document_id`, and ignores empty stored parents when looking for a prior non-empty parent.
- Persisted turn lookup does not filter by the historical local `bank_id`; `/retain` submits matching lineage turns to the bank configured at retain time.
- Manual `/retain` submits a clean item with `content`, configured `context`, and `update_mode="replace"` when the API supports explicit update modes. Automatic/incremental retain still uses `append` for new-turn deltas.
- Legacy provider buffer flush still tracks one pending append job, a session generation guard, and queued/flushed turn counts so automatic retain and direct provider tests do not regress.
- After upstream `09d66037f` added `_last_retained_turn_count` for append retain deltas, this fork intentionally keeps automatic retain routed through `flush_retained_turns()` instead, so automatic retain and manual/direct flush share the same queued/flushed/pending/generation state machine while still sending only new turns on append-capable APIs.
- When `auto_retain=false`, completed turns are written only to the local retain-turn SQLite file until `/retain` submits them.
- `/undo` now calls a dedicated memory rewind hook in CLI, Gateway, and TUI paths; Hindsight mirrors that rewind by soft-excluding the last N active rows in `hindsight_retain_turns` (`active=0`, `rewound_at`) so future manual `/retain` skips undone turns without hard-deleting audit rows.
- Hindsight rewind handling truncates the in-memory retain buffer and invalidates flush state without running the normal session-switch flush, so `/undo` does not itself push stale buffered turns to Hindsight.
- Only `/retain` is user-facing; no long command aliases are registered.
- Slack native slash generation keeps Telegram-visible canonical commands ahead of low-priority aliases so the extra fork-only `/retain` command keeps a native Slack slot under Slack's 50-command cap.
- Slack routes low-frequency or high-cost commands such as `/billing`, `/blueprint`, `/credits`, `/moa`, `/debug`, `/disk-cleanup` / `/disk_cleanup`, and `/lcm` through `/hermes <command>` on this fork when native slots are exhausted; this preserves native `/retain` while keeping those commands reachable on Slack.
- `hindsight_retain_session` is not registered in model-visible tool schemas; CLI/Gateway call the provider directly via `memory_manager.get_provider("hindsight")`, so manual retain works even when `memory_mode="context"` hides Hindsight tools from the model.

Why it matters:

- The user needs an explicit, user-triggered way to preserve the normal Hindsight session document without changing the automatic retain storage model.
- Manual `/retain` must preserve the exact Hindsight turn payloads that were already captured by the provider, and must not reconstruct content from SessionDB because SessionDB may contain LCM/compression summaries instead of original turns.
- Interrupted/orphan user messages must be captured into `retain_turns.sqlite3` at normal turn-sync time via `sync_turn(..., messages=...)`; `/retain` still must not reconstruct content directly from SessionDB.

Merge protection:

- Preserve provider-owned `$HERMES_HOME/hindsight/retain_turns.sqlite3` as the only manual `/retain` content source and as the stable `retain_document_id` resolver for compression-created logical documents.
- CLI/Gateway `/retain` must not read SessionDB transcript content, must not call `retain_conversation_messages(...)`, and must not fall back to SessionDB when persisted rows are absent.
- SessionDB use in `/retain` is limited to locating the active session and parent/title metadata needed to find persisted rows. LCM `[Recent Summary ...]` rows must never be sent to Hindsight as retained content.
- Future attempts to preserve interrupted/orphan user messages must first persist those turns into `retain_turns.sqlite3`; do not revive the 2026-06-15 SessionDB-primary path.
- Preserve Hindsight `sync_turn(..., messages=...)` as a MemoryManager opt-in path: it must rebuild clean retain turns from the completed transcript and persist the real first user message before later correction turns, without retaining tool outputs, assistant tool-call shells, summaries, `Operation interrupted:` notices, empty assistant messages, intermediate assistant drafts, compression/task-list rehydration markers, externalized payload placeholders, or async delegation completion payloads. The last eligible user-visible assistant response triggered by async completion must remain in order as an assistant-only retained event, never paired with a prior orphan user. When a compressed replay window contains only an exact synthetic `[Recent Summary (d0...)]` LCM header user row and the actual final assistant answer, retain only that assistant answer as an assistant-only event; preserve user-authored lookalikes as ordinary user content, and do not extend this exception to standalone Session-Arc/Durable/Depth summary turns.
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
- Preserve tests proving manual retain persists Hindsight turn payloads to the separate SQLite file, groups compression siblings by root `retain_document_id`, falls back through prior non-empty parent rows for older data, resolves resumed/restarted gateway sessions without cached agents, ignores historical local bank casing/config changes, handles no-persisted-turn sessions, excludes rewound persisted turns by real-user-turn count, keeps legacy buffer flush behavior (pending rejection, failure rollback, generation guard, `memory_mode="context"`), verifies gateway interrupt / multi-user-turn transcript sync preserves the real first user message, drops async-delegation/compression/externalized synthetic payloads while retaining ordered user-visible async assistant results and residual real user text, and keeps Slack/Telegram slash registration parity despite the extra `/retain` command.
- Do not reintroduce upstream's standalone `_last_retained_turn_count` watermark unless the entire fork flush state machine is deliberately replaced and all manual `/retain`, append-delta, pending-failure rollback, and session-switch generation tests still pass. The expected fork behavior is `sync_turn()` persists the turn first, then automatic retain calls `flush_retained_turns()`.

Verification:

- 2026-07-18 Recent-Summary orphan-assistant and monitor-stage correction: the focused regression first failed with zero retained turns, then passed after preserving only the visible assistant event. A fresh negative regression also first reproduced the over-broad `[Recent Summary request]` false trigger, then passed after summary detection was limited to exact LCM header syntax. The full related Hindsight/monitor suite passed with `256 passed`. `python -m py_compile`, `ruff check`, and a live `--defer-state` read-only monitor run passed; the run returned a state checkpoint but left the real monitor state SHA-256 unchanged. Monitor candidates now carry per-candidate StateDB/local-retain/remote counts, stage, all stable locators, and bounded content anchors; inactive+compacted rows can explain existing Document content but cannot become expected missing-content alerts. State cursors and reviewed keys commit atomically only after a successful silent review, successful report delivery, or a no-alert run. Direct evidence checks recovered all six confirmed historical omissions (`201422`, `201464`, `202446`, `202469`, `202520`, `202802`) as candidate-level `before_local_retain`, proved each local active retain payload still matched its remote Document, and excluded inactive false-positive ids `192891`, `197989`, and `197990`.
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

### 10. Custom OpenAI-compatible STT provider

Status: active

Date: 2026-05-22

Files:

- `tools/transcription_tools.py`
- `tests/tools/test_transcription.py`
- `tests/tools/test_transcription_dotenv_fallback.py`
- `hermes_cli/config.py`
- `agent/transcription_registry.py`

Summary:

- Adds a configurable OpenAI-compatible STT provider so the fork can use hosted ASR endpoints such as Alibaba DashScope Qwen STT without hardcoding a vendor-specific provider.

What changed:

- Added `stt.provider: custom_api` dispatch in `tools/transcription_tools.py`.
- Added `stt.custom_api` config for `base_url`, `api_key` / `api_key_env`, `model`, `endpoint`, `mode`, `response_format`, `language`, `prompt`, and `timeout`.
- The custom provider supports both multipart audio uploads and DashScope-style chat-completions audio input, using a single `input_audio` content item with a Base64 Data URL.
- It parses common transcription response shapes: `{text: ...}`, plain text, and `{choices:[{message:{content: ...}}]}`.
- Added tests for provider selection, dotenv/env-key resolution, request construction, response parsing, and `transcribe_audio()` dispatch.

Why it matters:

- The user's gateway can use Alibaba Qwen STT through config only, without hardcoding a vendor-specific provider or credential name.
- Future upstream merges must not collapse custom STT back into OpenAI-only credentials or hardcoded provider names.

Merge protection:

- Preserve explicit `stt.provider: custom_api` behavior and do not silently fall back to another STT provider when custom credentials are missing.
- Preserve `custom_api` as a native/built-in STT provider name in
  `agent/transcription_registry.py`; command providers and plugin providers must
  not shadow the fork's configured custom STT implementation.
- Preserve `api_key_env` lookup through `get_env_value()` so keys in `~/.hermes/.env` work.
- Preserve the Qwen ASR configuration shape: `QWEN_API_KEY`, model `qwen3-asr-flash-2026-02-10`, DashScope compatible base URL, and `/chat/completions` mode.
- Preserve response parsing for both OpenAI-style `{text: ...}` and chat-style `choices[0].message.content` responses unless upstream has a verified equivalent.

Verification:

```bash
python -m py_compile tools/transcription_tools.py hermes_cli/config.py tests/tools/test_transcription.py tests/tools/test_transcription_dotenv_fallback.py
python -m pytest tests/tools/test_transcription.py tests/tools/test_transcription_dotenv_fallback.py tests/tools/test_transcription_tools.py -q -o 'addopts='
```

Feature docs: none — STT provider extension documented in this index.

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
- Preserve the generic `audio_speech` mode unless upstream provides a verified equivalent configurable HTTP TTS provider.
- Preserve Telegram Opus conversion behavior for `custom_api` when voice-compatible delivery is needed.

Verification:

```bash
scripts/run_tests.sh tests/agent/test_tts_registry.py tests/fork/test_custom_qwen_tts.py tests/tools/test_tts_plugin_dispatch.py tests/tools/test_tts_command_providers.py tests/tools/test_tts_opus_routing.py tests/tools/test_tts_max_text_length.py -q
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


### 12. Temporary Codex backend prompt-cache routing workaround

Status: active, temporary

Date: 2026-06-16

Files:

- `agent/chat_completion_helpers.py`
- `agent/transports/codex.py`
- `agent/conversation_loop.py`
- `agent/turn_context.py`
- `agent/codex_responses_adapter.py`
- `agent/memory_manager.py`
- `tests/agent/transports/test_codex_transport.py`
- `tests/agent/test_memory_provider.py`
- `tests/run_agent/test_run_agent.py`
- `tests/run_agent/test_run_agent_codex_responses.py`
- `tests/run_agent/test_codex_app_server_integration.py`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- Adds a short-term fork workaround for Codex prompt-cache regressions where
  consecutive tool calls, compression continuations, or dynamic memory-context
  injection can otherwise lose cache affinity.

What changed:

- Codex Responses requests can pass a stable `prompt_cache_key` separate from
  the physical Hermes `session_id`.
- Gateway sessions prefer the stable `_gateway_session_key` as the Codex cache
  thread, so compression-created continuation sessions do not reset the cache
  scope just because Hermes rotated the physical session row.
- Non-gateway sessions may fall back to compression lineage so compression
  children can share the same cache scope as their root session.
- Codex backend HTTP cache-routing headers keep the fork's stable logical
  prompt-cache routing while aligning the physical-session header spelling with
  upstream:
  - `session_id` = physical Hermes session id
  - `thread-id` = stable `prompt_cache_key`
  - `x-client-request-id` = stable `prompt_cache_key`
- During the 2026-06-18 upstream sync, upstream's official `session_id` /
  `x-client-request-id` header variant was tested and temporarily adopted, but
  Langfuse later showed it did **not** fully preserve cache affinity in long
  gateway sessions. Same-turn follow-up calls could still drop to zero cache
  reads, so the fork restored its own `thread-id`/stable-cache-key routing.
- OpenAI/Codex Responses auto-recall now injects prefetched `<memory-context>`
  as `role="developer"` input items immediately after the relevant user
  messages instead of appending it to the current user text. Prior request-only
  developer memory slots are replayed at the same user-turn positions on later
  turns so the Codex prompt-cache prefix stays append-like across tool loops and
  the following user turn. Non-OpenAI Responses runtimes keep the legacy
  ephemeral user suffix; `codex_app_server` also keeps the suffix because that
  protocol does not expose a per-turn Responses developer input slot.
- Codex Responses chat-to-input conversion and preflight validation accept
  `role="developer"` input items and preserve them as `input_text` content.
- The memory-context system note wording was weakened from
  `authoritative reference data` / `should inform all responses` to: `This is
  the agent's persistent memory from prior sessions, for reference only.`
  Sanitization still strips both the old and new notes.

Why it matters:

- The user observed a sharp prompt-cache drop in Hermes on 2026-06-15 to
  2026-06-16, visible in Langfuse and local session usage.
- The failure lined up with the upstream change that stripped Codex backend
  `extra_headers`, plus Hermes using compression-rotated physical session ids as
  cache keys.
- This workaround keeps Gateway tool-call chains and compression continuations on
  a stable Codex cache thread.
- Keeping recalled memory out of `instructions` and the current user message
  preserves the stable prompt prefix and avoids treating old memory as fresh user
  discourse.
- The user wants persistent memory to remain available as historical reference,
  not promoted into a stronger instruction such as `authoritative` or `should
  inform all responses`.

Merge protection:

- This is intentionally a temporary fork patch, not a long-term fork feature.
- Do **not** discard the fork `session_id` / `thread-id` / stable
  `x-client-request-id` routing merely because upstream has a nominal Codex
  cache-routing fix. The 2026-06-18 upstream `session_id` /
  `x-client-request-id` variant was tested in real Langfuse traces and did not
  fully fix long gateway sessions: same-turn follow-up calls still sometimes
  dropped to `cache_read_input_tokens=0`.
- Preserve the fork's stable `prompt_cache_key` body field and matching
  `thread-id` / `x-client-request-id` headers for gateway and compression
  lineage.
- Preserve after-user `role="developer"` memory-context injection and replay of
  prior request-only developer slots for OpenAI/Codex Responses runtimes. Do not
  move recalled memory back into `instructions`, the current user message text,
  or the request tail unless live cache evidence and behavior tests show the
  replacement is equivalent.
- Preserve the fallback path that appends recalled memory to the current user
  turn only for runtimes that cannot accept Responses developer input.
- Preserve the exact weak memory-context note wording unless the user explicitly
  approves stronger wording: `This is the agent's persistent memory from prior
  sessions, for reference only.`
- Preserve tests that prove developer input items survive Codex Responses
  conversion/preflight, that memory does not enter `instructions` or the current
  user suffix on OpenAI/Codex Responses, and that same-turn plus next-turn tool
  chains do not rewrite prior developer-memory prefix slots.
- Drop this workaround only after an upstream replacement has been checked
  carefully against real long-running gateway telemetry and focused tests. At a
  minimum, verify both cross-turn and same-turn tool-call chains keep high cache
  reads after memory-context injection, retries, and compression boundaries.
- If future upstream code conflicts with this area but has not been proven with
  that telemetry, stop and ask the user instead of assuming the upstream fix is
  equivalent.

Verification:

```bash
git diff --check
python -m pytest tests/agent/transports/test_codex_transport.py tests/run_agent/test_run_agent_codex_responses.py tests/gateway/test_agent_cache.py -q
```

Observed local results:

- 2026-07-10 upstream sync: the Codex app-server external-memory fixture now
  returns an explicit empty prefetch string because the fork's request-only
  memory path calls `prefetch_all()` before dispatch. The fork and
  changed-upstream focused suite reported `2070 passed`.
- 2026-06-16: `202 passed` for the focused pytest command above. After gateway
  restart and a compression boundary, the new continuation session continued
  receiving cache reads instead of staying at zero cache.
- 2026-06-18 upstream sync: upstream official Codex HTTP header names were
  temporarily adopted while retaining stable `prompt_cache_key`; targeted
  Codex/Hindsight/session tests reported `630 passed`, but later Langfuse
  traces showed the official header variant was insufficient in live long
  gateway sessions.
- 2026-06-18 post-sync telemetry: trace
  `59cc7e9d98edd3ff9fe35f8e4980ec88` / session
  `20260618_104142_40390af6` had a same-turn follow-up call drop from
  `input=146,239, cache_read_input_tokens=24,576` to
  `input=176,230, cache_read_input_tokens=0`; trace
  `1829d0bdab5e3cd305d395abea4a594f` showed the older fork routing was mostly
  stable for same-turn follow-ups before the switch, while the official variant
  later produced another same-turn cliff (`cache_read_input_tokens=0`). This
  evidence restored the fork header workaround.
- 2026-06-24 physical-session header spelling alignment: switched the physical
  Hermes session header from `session-id` to upstream spelling `session_id`
  while retaining the stable `thread-id` / `x-client-request-id` values from
  `prompt_cache_key`. `python -m py_compile agent/transports/codex.py tests/agent/transports/test_codex_transport.py tests/run_agent/test_run_agent_codex_responses.py` → passed; `python -m pytest tests/agent/transports/test_codex_transport.py tests/run_agent/test_run_agent_codex_responses.py -q -o 'addopts='` → 139 passed, 1 unrelated `audioop` deprecation warning; `git diff --check` → passed.
- 2026-06-26 OpenAI/Codex Responses developer memory injection: targeted
  request-shape tests for developer recall, chat-completions fallback,
  Responses developer conversion, developer preflight acceptance, and optional
  function-call-id stripping reported `5 passed`; memory-context wrapper tests
  reported `6 passed`; combined scrubber/developer focused regression reported
  `28 passed, 1 unrelated audioop deprecation warning`; `python -m py_compile
  agent/memory_manager.py tests/agent/test_memory_provider.py` and `git diff
  --check` passed.
- 2026-06-29 same-turn Codex tool-loop placement fix: moved that ephemeral
  developer memory item from the request tail to immediately after the current
  user item, so follow-up tool-loop requests keep the memory block before newly
  appended assistant/tool items instead of moving the previous tail. RED test:
  `tests/fork/test_codex_memory_context_isolation.py::test_auto_memory_recall_codex_developer_stays_after_user_across_tool_loop`
  failed with `developer_index == 3` instead of `1` before the fix.
- 2026-06-29 next-turn Codex memory replay fix for Langfuse trace
  `aa645114645bf98925dcb9debfaa92a2`: added a regression that reproduces the
  prior request's developer-memory slot being rewritten by a replayed
  `reasoning` item on the following user turn. RED test
  `tests/fork/test_codex_memory_context_isolation.py::test_auto_memory_recall_codex_replays_prior_turn_developer_for_cache_affinity`
  failed before the fix with the second-turn item at that slot equal to
  `{"type": "reasoning", ...}` instead of the prior `role="developer"` memory
  item. After the fix: `python -m pytest tests/fork/test_codex_memory_context_isolation.py tests/fork/test_codex_prompt_cache.py tests/run_agent/test_run_agent_codex_responses.py -q -o 'addopts='`
  → 93 passed; `python -m pytest tests/fork -q -o 'addopts='` → 300 passed;
  `python -m py_compile agent/conversation_loop.py agent/turn_context.py tests/fork/test_codex_memory_context_isolation.py` and `git diff --check` passed.
- 2026-06-29 repaired-user preflight dump fix for the same trace: live dumps
  after 16:05 still showed tail `role="developer"` because
  `repair_message_sequence_with_cursor()` can remove/merge messages before the
  current user while `current_turn_user_idx` kept its pre-repair value. The
  memory marker then missed the current user and the defensive fallback appended
  the developer memory item at the request tail after reasoning/tool history.
  The fork now re-anchors the current-user cursor after repair and the Codex
  fallback inserts memory after the last user instead of at the tail. RED test
  `tests/fork/test_codex_memory_context_isolation.py::test_auto_memory_recall_codex_preflight_dump_keeps_developer_after_repaired_user`
  captures the final `HERMES_DUMP_REQUESTS` preflight body and failed before the
  fix with the developer item at the tail. Verification: `HERMES_DUMP_REQUESTS=0
  HERMES_HOME=/tmp/hermes-verify-memory-context venv/bin/python -m pytest
  tests/fork/test_codex_memory_context_isolation.py tests/run_agent/test_run_agent_codex_responses.py -q -o 'addopts='`
  → 88 passed; `HERMES_DUMP_REQUESTS=0 HERMES_HOME=/tmp/hermes-verify-fork
  venv/bin/python -m pytest tests/fork -q -o 'addopts='` → 301 passed;
  `venv/bin/python -m py_compile agent/conversation_loop.py agent/turn_context.py tests/fork/test_codex_memory_context_isolation.py`
  and `git diff --check` passed.

Upstream status: upstream official Codex header fix exists but is not equivalent
for this fork's long gateway-session cache behavior; active fork workaround
restored pending real telemetry proof of an upstream replacement.

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
scripts/run_tests.sh tests/fork/test_codex_prompt_cache.py tests/gateway/test_telegram_auth_check.py tests/gateway/test_telegram_callback_auth_fail_closed.py -q
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
- Safe command rewrite:
  - `tools/safe_cmd_rewrite.py`
  - `tools/terminal_tool.py`
  - `tests/fork/test_safe_cmd_rewrite.py`
  - `pyproject.toml`
- Disable newly bundled skills by default when configured:
  - `tools/skills_sync.py`
  - `hermes_cli/main.py`
  - `tests/tools/test_skills_sync.py`
- Temporary Codex backend prompt-cache routing workaround:
  - `agent/chat_completion_helpers.py`
  - `agent/transports/codex.py`
  - `agent/conversation_loop.py`
  - `agent/turn_context.py`
  - `agent/codex_responses_adapter.py`
  - `agent/memory_manager.py`
  - `tests/agent/transports/test_codex_transport.py`
  - `tests/agent/test_memory_provider.py`
  - `tests/run_agent/test_run_agent.py`
  - `tests/run_agent/test_run_agent_codex_responses.py`
  - `tests/run_agent/test_codex_app_server_integration.py`
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


## Summary statistics

Documented entries: 17 major entries.

Active functional areas: 14.

Historical reverted / abandoned areas: 2.

Fork-only non-merge commits represented here: see
`git log --no-merges upstream/main..HEAD`.

<!--
Add future modifications above this summary, under either:
- Active modifications
- Historical / reverted modifications
-->
