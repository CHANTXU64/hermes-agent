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

### Fixed-clock session heartbeats and incremental inbox scanner

- ID: `daily-session-heartbeat`
- Status: active
- Depends on: none (uses upstream heartbeat persistence, ownership and adapter admission)
- Source boundary: logical-only

Files / touchpoints:
- `fork_features/daily_heartbeat.py` — Fork wall-clock validation, next occurrence, shared daily-command parser.
- `hermes_cli/heartbeat.py` — native state serialization, due/status calculation and manager daily setter; existing interval mode remains valid.
- `gateway/slash_commands_goals.py`, `hermes_cli/cli_commands_mixin.py` — thin command entry points; existing pollers own scheduling and session execution.
- `scripts/todo_file_watch.py` — standalone local inbox discovery/acknowledgement, no scheduler or model; writes state outside business files.
- `tests/fork/test_daily_heartbeat.py`, `tests/fork/test_todo_file_watch.py` — behaviour and real temporary storage/adapter tests.

Intent / invariants:
- `/heartbeat daily HH:MM,HH:MM IANA-timezone <prompt>` runs at named local times in the original session/model; it is not an isolated judge or a cron delivery. Dates/timezones are independent of host TZ. Repeated identical active setup preserves timing; pause/resume reanchors; missed ticks coalesce. Existing reset/suspend/compression ownership rules remain authoritative.
- `/heartbeat weekly <Mon-Fri times> <Sat-Sun times> IANA-timezone <prompt>` uses separate weekday/weekend times without a holiday calendar or make-up-workday rules. Weekly mode does not replay a previous day's missed tick before today's first slot; same-day delayed ticks still coalesce under native idle-session admission.
- Daily/weekend fields must survive JSON reload and compression migration. Never use zero interval as a periodic fallback for daily state. Old interval-only state still loads and executes unchanged.
- Inbox initialization is explicit and idempotent. Only first-seen paths become pending, with local unchanged renames recognized; acknowledged ordinary edits are not new arrivals. Pending content changes invalidate old acknowledgement IDs. Discovery is not business completion: pending survives read/analysis/recording failure until an explicit durable record reference is provided. Missing/corrupt state never silently rebaselines. Ignore temporary files and symlinks; business files are never changed.
- Gateway/CLI text commands are the supported daily entry points; no Desktop daily-schedule editor or new external wake API is provided. Profile shortcuts and business prompt are deployment configuration, not global defaults.

Merge decision:
- Preserve when: upstream only supports elapsed intervals, or its replacement loses original-session execution, timezone, repeated-enable or pending/ack contracts.
- Drop when: an upstream replacement is behaviour-tested against these invariants and the user accepts it.
- Ask user when: upstream introduces a different schedule/session lifecycle or a second-model heartbeat design; PR #92656 only changes heartbeat model routing and is not equivalent.

Verification:
```bash
scripts/run_tests.sh tests/fork/test_daily_heartbeat.py tests/fork/test_todo_file_watch.py tests/hermes_cli/test_heartbeat.py tests/gateway/test_heartbeat_poller.py tests/gateway/test_heartbeat_watch_restore.py tests/gateway/test_heartbeat_execution_ownership.py tests/gateway/test_heartbeat_acceptance.py tests/gateway/test_heartbeat_session_boundaries.py tests/gateway/test_heartbeat_watch_lifecycle.py
```

- Upstream status: fork-only; related non-equivalent candidate https://github.com/NousResearch/hermes-agent/pull/92656
- Last validated: upstream inspected `2f21d29f4446134b51b7e6b1d2f515502bc0ae5e`; Fork base `694d9251cd17ece85cdb758c9adb64ea6434385b` plus working changes. Local validation only; running Gateway requires user-controlled restart.
- Feature docs: none — focused contracts and deployment-specific task instructions suffice.

### 1. Hindsight Chinese / Unicode support

Status: production boundary decoupled; fork contract only (2026-08-30)

Date: 2026-04-20; contract isolated 2026-08-30

Commit: `7428b0da`

Files:

- `plugins/memory/hindsight/__init__.py` — production serializer
- `tests/fork/test_hindsight_unicode_contract.py` — fork behavior contract

What changed:

- `json.dumps` in the Hindsight memory plugin uses `ensure_ascii=False`.
- Chinese and other Unicode text are stored/read as real characters instead of
  escaped `\uXXXX` sequences.
- The fork no longer carries a separate Unicode production implementation. A
  dedicated contract test observes the actual retain payload at the provider
  boundary instead of coupling this behavior to the retired manual-retain tests.

Why it matters:

- The user relies on Chinese memory content being readable and not escaped.
- Future merges must not silently restore ASCII-escaped Hindsight payloads.

Merge protection:

- Keep the dedicated contract test even when the production serializer moves.
- Verify the actual retain payload contains literal Chinese text and does not
  contain `\uXXXX`; keyword inspection alone is insufficient.
- Do not create a fork serializer, runtime hook, or release-time text patch for
  this one-line contract while the provider boundary satisfies it directly.

Verification:

```bash
./venv/bin/python -m pytest tests/fork/test_hindsight_unicode_contract.py -q -o 'addopts='
```

Production ownership: normal Hindsight provider path; fork ownership is the
Unicode behavior contract.

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

Status: policy extracted to `fork_features` (2026-08-30)

Date: 2026-05-09; boundary refactored 2026-08-30

Files:

- `hermes_cli/update_cmd_maint.py` — `hermes update` reports the `auto_disabled` set (count line
  plus the named list) so a silently disabled new skill stays visible.
- `fork_features/bundled_skills_policy.py` — fork-owned policy
- `tools/skills_sync.py` — one post-copy call and result handoff
- `hermes_cli/update_cmd.py` — reports the generic `auto_disabled` result
- `tests/fork/test_bundled_skills_policy.py`
- `tests/fork/test_skills_auto_disable.py`
- `docs/LOCAL_MODIFICATIONS.md`

What changed:

- `skills.auto_enable_new_bundled` remains an optional config with default
  behavior `true` when absent.
- When set to `false`, newly discovered bundled skills are still copied into
  `~/.hermes/skills/` and recorded in `.bundled_manifest`, but their names are
  appended to `skills.disabled` during that first sync.
- Existing bundled skills, updated bundled skills, user-modified bundled skills,
  user-deleted bundled skills, hub-installed skills, and user-created skills are
  left alone.
- `hermes update` output reports when new bundled skills were disabled by this
  config.
- The config decision and write now live in the fork-owned policy module.
  `tools/skills_sync.py` passes only the names copied in the current sync through
  one thin post-copy seam, so installs, updates, gateway startup, and named
  profile syncs retain identical behavior.

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
- Keep policy logic out of `tools/skills_sync.py`; that upstream synchronizer may
  collect copied names, call the fork policy once, and return its result only.
- Run the fork policy and integration tests after conflicts touching skill sync,
  config persistence, profile seeding, or update reporting.

Verification:

```bash
./venv/bin/python -m pytest tests/fork/test_bundled_skills_policy.py tests/fork/test_skills_auto_disable.py tests/tools/test_skills_sync.py -q -o 'addopts='
```

Upstream status: fork-only.

### 8. Hindsight synchronous cache-miss recall

Date: 2026-05-22; cache lifecycle boundary migrated 2026-08-30

Files:

- `agent/memory_manager.py`
- `agent/memory_provider.py`
- `hermes_state.py`
- `tui_gateway/methods_tools.py`
- `tests/fork/test_hindsight_rewind.py`
- `tests/gateway/test_undo_rewind_session.py`
- `tests/tui_gateway/test_undo_command.py`
- `fork_features/hindsight_recall_cache.py`
- `plugins/memory/hindsight/__init__.py`
- `tests/fork_features/test_hindsight_recall_cache.py`
- `tests/fork/test_hindsight_provider_regressions.py`
- `tests/fork/test_hindsight_recall_preprocessor.py`
- `tests/fork/test_hindsight_manual_retain_removed.py`
- `tests/plugins/memory/test_hindsight_provider.py`
- `tests/agent/test_memory_session_switch.py`
- `tests/agent/test_memory_sync_interrupted.py`
- `docs/LOCAL_MODIFICATIONS.md`

What changed:

- Hindsight auto-recall has a bounded synchronous fallback when no carried prior
  snapshot exists, so the first user turn in a new session or after compression
  can receive `<memory-context>` immediately.
- Added `recall_sync_on_cache_miss` and `recall_sync_timeout_seconds` provider
  settings. Defaults: enabled, 5 seconds.
- Current-turn recall snapshots are guarded by a generation counter so a late
  result from an older turn/session cannot overwrite newer recall context.
- The Fork-owned cache lifecycle now lives in
  `fork_features/hindsight_recall_cache.py`: atomic consume, generation-checked
  carry, matching-turn timeout invalidation, Session rotation, Undo invalidation,
  and the synchronous-miss gate. The Hindsight Provider owns API calls, query
  truncation, result formatting, sync-fallback trigger/callback and snapshot
  carry; P5 model decisions and result orchestration live in
  `plugins/memory/hindsight/recall_preprocessor.py`. `MemoryManager` remains
  provider-neutral and only supplies the outer timeout/thread boundary.
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
- Fixed upstream `66666f6e2eca0ae883195a34c66131985ea7dd06` has a different
  `recall_sync` option: it synchronously recalls every eligible turn and defaults
  off. It is not equivalent to this Fork's default-on, cache-miss-only fallback.
  Do not replace one with the other without a separate behavior decision.

Feature docs: `docs/chantxu64/hindsight-sync-cache-miss-recall/README.md`

Verification after the 2026-08-30 boundary migration:

Canonical focused gate, rerun on 2026-08-31:

```bash
scripts/run_tests.sh tests/fork_features/test_hindsight_recall_cache.py tests/fork/test_hindsight_provider_regressions.py tests/plugins/memory/test_hindsight_provider.py tests/agent/test_memory_session_switch.py tests/run_agent/test_memory_sync_interrupted.py -q
```

- Current canonical result: `84 passed`, `0 failed` across `5` files.

- Seven Fork state contracts were observed RED before each capability existed,
  then GREEN for Session-scoped consume, stale-generation carry rejection,
  matching-turn timeout invalidation, Session rotation, non-invalidating Session
  rebind, Undo invalidation and all synchronous-miss skip conditions. A separate
  structure contract rejects reintroduced legacy fields or compatibility
  properties on the Provider.
- Hindsight/Fork/provider/MemoryManager/Session/Request-only/compression/Gateway
  memory regression set: `384 passed` with `7` third-party deprecation warnings.
- One historical fixed-SHA helper simulation reported `2 → 2` text conflict
  regions. A later fresh review using raw `merge-file` conflict-marker counting
  reported `0 → 0` for the same Provider snapshots and could not reproduce that
  exact count. These method-specific figures are not a stable maintenance
  metric; no conflict-count reduction is claimed. The high-frequency Provider
  file changed by `+56/-99`, moving the concrete state policy behind one Fork
  object.
- Independent `xai-oauth/grok-4.6` `xhigh` review returned `PASS` with `0`
  blocking findings. Its applicable findings were closed before commit:
  `sync_turn` now rebinds the cache Session without clearing carried Recall,
  the structure contract also rejects properties, fixture-only `seed()` is
  labeled, and the verification/file indexes were corrected.

Upstream status: intentional Fork divergence from upstream's every-turn
`recall_sync` option.

### 9. Hindsight P5 recall preprocessor

Date: 2026-07-17; external-prefetch timeout compatibility fix 2026-07-19;
configured model fallback 2026-08-09; orchestration boundary migrated 2026-08-30

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
- `tests/fork_features/test_hindsight_p5_policy.py`
- `tests/fork/test_hindsight_recall_preprocessor.py`
- `tests/agent/test_memory_provider.py`
- `tests/fork/test_hindsight_provider_regressions.py`
- `tests/hermes_cli/test_plugin_auxiliary_tasks.py`
- `tests/agent/test_run_agent_codex_responses.py`
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
- The Fork-only `recall_preprocessor.py` now owns both the model decision and
  the pure result orchestration: old-ref filtering, optional recall callback,
  complete-old-cache restoration, current-query fallback signaling and actual
  snapshot construction. The high-frequency Hindsight Provider supplies the
  bounded recall callback and carries the returned snapshot; it no longer
  imports the raw preprocessor runner or duplicates the P5 branches. Existing
  `MemoryManager`, TurnContext, auxiliary-task registration and Codex model
  provenance remain generic extension seams and were not modified by this
  boundary migration.
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
- Keep old-ref selection, new-query recall and failure restoration in the
  Fork-only P5 module. Do not re-export `run_recall_preprocessor` from the
  Provider or copy those branches back into `plugins/memory/hindsight/__init__.py`.
- The P5 orchestration commit `541b8f1083` statically depends on the Recall cache
  lifecycle from `b7cf9981c7`. Revert P5 first and the cache lifecycle second;
  retaining P5 after removing the cache unit is unsupported.
- Preserve provider-specific prefetch budgeting. Do not replace the generic
  external-provider 8-second guard with a Hindsight-specific global constant,
  and do not let that generic guard silently override P5/recall stage timeouts.
- Do not reintroduce rejected P6 behavior that forces `new_query=null` merely
  because old results appear to cover the target.
- Run the focused command documented in the feature README after conflicts
  touching memory prefetch, turn context, Hindsight, or auxiliary routing.

Verification after the 2026-08-30 orchestration migration:

- Six policy/boundary contracts were observed RED then GREEN: missing
  orchestration entry, P5-route failure restoration, generated-query Recall
  failure restoration, all-old-refs chain clearing, and removal of the Provider
  raw-runner alias, plus rejection of a non-fallback outcome without a snapshot.
  Four adjacent contracts for no-old-result fallback, null reuse and successful
  empty Recall were GREEN through those shared branches.
- Historical direct-pytest P5 focused integration gate: `302 passed`.
- Historical direct-pytest Hindsight/MemoryManager/Request-only/compression/
  Session/Gateway expanded gate: `394 passed` with `7` third-party deprecation
  warnings.
- Prompt SHA-256 remained
  `b9b182478b41ab593398bb1649b8a318ab7f59464cd4abe5681a7add6481106f`.
- The historical helper simulation reported `2 → 2` text conflict regions and
  attributed both to unrelated retain/observation areas. A later raw
  `merge-file` marker count reported `0 → 0` and could not reproduce that exact
  count. The figures are method-specific and are not used as a stable metric;
  no conflict-count reduction is claimed. The high-frequency Provider changed
  by `+17/-61`, the Fork-only P5 module by `+76/-1`, and no `agent/`,
  `hermes_cli/`, or generic memory-plugin bridge file changed in this unit.
- Independent `xai-oauth/grok-4.6` `xhigh` review returned `PASS` with `0`
  blocking findings. Its two applicable findings were closed before commit:
  Runtime Flow now assigns filtering/recall/outcome work to the P5 module, and
  the Provider explicitly rejects a non-fallback outcome without a snapshot
  instead of silently reusing old text.
- A later full fourth-batch review using `openai-codex/gpt-5.6-sol` with `max`
  reasoning returned `PATCH` for documentation only and `0` blocking findings;
  all three code commits were recommended for retention. The responsibility map,
  static dependency/revert order and canonical test commands were corrected.
- Canonical `scripts/run_tests.sh` follow-up on 2026-08-31 passed all three
  focused gates: request-only/cache routing `345`, cache-miss lifecycle `84`,
  and P5 integration `480`, with `0` failures.
- A fresh final-state `xai-oauth/grok-4.6` `xhigh` review independently returned
  `PASS` with `0` blocking findings and recommended retaining all four commits.
  It did not rerun the canonical tests or perform Gateway/external-API runtime
  validation.

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
- `tests/agent/test_background_review.py`
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

### 16. Prompt execution-contract deduplication

Status: historical / reverted per user decision

Date introduced: 2026-07-17

Date reverted: 2026-08-30

Historical files:

- `agent/prompt_builder.py`
- `agent/system_prompt.py`
- `tests/agent/test_prompt_builder.py`
- `docs/LOCAL_MODIFICATIONS.md`

Historical behavior:

- Replaced upstream's repeated persistence language with a bounded universal
  execution contract and a narrower same-response tool-follow-through block.
- Removed the extra Gemini/Gemma `Keep going` directive.

Current status:

- The user explicitly chose to restore current `upstream/main` after being told
  that it still contains the stronger `Keep working until the task is actually
  complete` and Gemini/Gemma `Keep going` directives.
- Prompt constants, their assembly comment, and tests now follow the current
  upstream behavior for this area.

Merge protection:

- Do not restore the former bounded execution contract during an upstream merge
  unless the user explicitly requests that behavior again.
- Keep current upstream Prompt behavior canonical for this area.

Upstream status: restored to current upstream per user decision.

## Active modifications (continued)

### 9. Hindsight manual full-session retain

Status: obsolete (retired 2026-08-25)

Date introduced: 2026-05-21

Retirement decision:

- Removed the fork-owned Hermes `/retain` command, `retain_on_new` / `retain_on_new_timeout_seconds`, retain-before-`/new` / `/reset` gates, TUI boundary RPC/state, provider-owned `retain_turns.sqlite3` readers/writers, submission ledger/replay/lineage machinery, and Hindsight-specific persistent rewind bookkeeping.
- Do not resurrect this chain during upstream merges. The historical local `$HERMES_HOME/hindsight/retain_turns.sqlite3` file is intentionally left untouched as inert audit data; runtime code must not create, read, migrate, or write it.
- Preserve the official model-visible `hindsight_retain`, `hindsight_recall`, and `hindsight_reflect` tools. `hindsight_retain` continues to use the configured Bank plus normal tags, context, source, and metadata behavior.
- Preserve the official automatic Retain implementation for upstream compatibility, but this installation keeps `auto_retain=false`; `/new`, `/reset`, and `/undo` no longer cause fork-owned Hindsight persistence.
- Preserve Unicode-safe official turn serialization (`json.dumps(..., ensure_ascii=False)`), normal Bank selection, Recall/P5/cache behavior, generic `MemoryProvider` / `MemoryManager` rewind lifecycle, SessionDB active-only rewind, and normal session switching.
- Preserve shared `messages=` support in the generic memory pipeline because providers other than Hindsight use it; Hindsight no longer consumes it for a private ledger.
- The active Langfuse candidate generator and delayed writer are Fork-owned under `fork_features/hindsight_retain/`. The separate config-defined Quick Command consumer below owns their Hindsight write/monitor contract and does not revive this retired provider-owned chain, the legacy SQLite ledger, `/new` integration, replay, or provider lifecycle hooks.

Retirement verification:

- Anti-resurrection regression: `tests/fork/test_hindsight_manual_retain_removed.py`.
- Recall/P5 fork regressions remain in `tests/fork/test_hindsight_provider_regressions.py`; official provider tests continue to own automatic Retain and tool behavior.
- Verification completed on 2026-08-25: Hindsight provider, anti-resurrection,
  Recall/P5, and rewind suites passed `74`; adjacent CLI/Gateway/TUI-Gateway
  session-switch/reset/undo suites passed `86`; Fork + command-registry + Slack
  suites passed `698`; focused TUI suites passed `83` plus TypeScript typecheck
  and ESLint. `py_compile` and `git diff --check` passed. Production residual
  scan found zero legacy command/ledger symbols; only negative guard assertions
  and this retirement record retain their names.
- Independent diff review initially blocked on two stale production comments and
  an active-delta checklist that still named retired files. Those references were
  corrected; the exact stale-reference and provider-ledger scans then returned
  zero, and the post-review command/Slack/anti-resurrection set passed `225`.

Upstream status: fork-only feature retired; official Hindsight tools and automatic Retain implementation retained.

### 9a. Gateway session-aware exec Quick Commands

- ID: `gateway-session-aware-quick-command`
- Status: active
- Depends on: none — reuses the existing Gateway `SessionStore` routing map
- Source boundary: logical-only

Files / touchpoints:

- `fork_features/hindsight_retain/__init__.py` — package marker for the delayed-Retain feature.
- `gateway/run.py` — upstream Gateway Quick Command exec seam
- `tests/fork/test_gateway_quick_command_session_env.py` — Fork-owned behavior and cross-route concurrency coverage
- `fork_features/hindsight_retain/retain_integrity.py` — Fork-owned delayed Retain scheduler, writer, receipt scanner, and remote verifier
- `fork_features/hindsight_retain/langfuse_hindsight_export.py` — Fork-owned Langfuse candidate exporter with request-time cutoff filtering
- `tests/fork/test_hindsight_retain_integrity.py` and `tests/fork/test_langfuse_hindsight_export.py` — Fork-owned Retain/exporter regression coverage
- Machine-local consumers outside this repository: `~/.hermes/config.yaml`, `~/.hermes/hindsight/config.json`, `~/.hermes/scripts/check-hermes-hindsight.py`, and `~/.hermes/scripts/hindsight_monitor_html.py`

Intent / invariants:

- A config-defined Gateway `type: exec` Quick Command can opt in only with the strict boolean `session_env: true`; only that command receives the active durable Hermes session ID as `HERMES_SESSION_ID`. Strings, numbers, false, null, and an absent key do not opt in.
- Gateway resolves the current message's complete route key through the existing `SessionStore` map; it must not create a session, choose a recent session, or fall back across platform, chat/thread/topic, profile, or Telegram `account_id` boundaries.
- Every Gateway Quick Command child env first drops any process-global `HERMES_SESSION_ID`; strict opt-in then adds the exact mapped session only to that child dictionary. The implementation must never mutate process-global `os.environ`, so concurrent channels and accounts cannot overwrite one another.
- Commands without `session_env: true` receive no Hermes session ID and otherwise preserve upstream Quick Command behavior. A session-aware command with no current mapping fails before spawning its child process. CLI, TUI, and Desktop remain outside this Gateway-only unit and are not modified by it.
- `/retain` calls the Fork-owned `schedule` command. It validates the configured Bank, records a lock-protected, append-only, fsynced `scheduled` receipt containing the exact triggering session, request-time cutoff, and `due_at=requested_at+1200s`, then starts a detached child with closed stdio and returns `status=scheduled` immediately. The CLI keeps JSON as its default machine-readable output; the profile-local Gateway Quick Command explicitly selects `--output-format text`, which turns schedule success or failure into a concise user-facing receipt instead of exposing raw JSON in chat. The user explicitly chose this simple non-durable worker: Gateway or machine shutdown can lose it; no Cron, replay, or restart recovery is added. The scanner reports a high alert when a scheduled attempt is past due plus grace and has no `started` receipt.
- The detached child waits only until its fixed `due_at`, then records `started` and reads Langfuse. Candidate filtering is per message timestamp and includes only content at or before the request-time cutoff; waiting 20 minutes is solely for Langfuse synchronization and must not include later messages from the same session. Because sanitized Langfuse v4 exports can contain only `Hermes turn` chains, candidate construction also reads the exact session's StateDB in query-only mode: it supplements de-duplicated user rows carrying a direct `platform_message_id`, visible assistant replies from active or compacted history, and successful `clarify` question/choice/response events. Assistant tool calls, hidden/internal notifications, runtime wrappers, standalone compression summaries, empty rows, and physical active/compacted copies are excluded; externally submitted text is forcibly secret-redacted. Missing events are inserted by source order/timestamp without replacing Langfuse final-answer evidence. Artifacts live under `<session_id>/<attempt_id>` with directory mode `0700` and file mode `0600`, so repeated calls cannot overwrite evidence for an earlier attempt. Before `export_succeeded`, one validator proves session/Document identity, canonical UUID, `turns == json.loads(document_content)`, recomputed counts and SHA-256, fsyncs every manifest artifact and the directory, and binds that immutable hash into later receipts. Before any expected remote POST, the writer requires this StateDB reconciliation to consume each matching candidate occurrence at most once and to report every source event matched or added with zero uncovered events. An independent cutoff snapshot then enforces separate user/assistant lower bounds, including projected `clarify` replies/questions, so a single missing visible event blocks before submit; the conservative severe-total-gap rule remains as an additional guard. Each blocked case records its own receipt and returns without loading remote config or sending data; export-only runs still preserve the local candidate for investigation.
- The verified writer targets the literal URL `https://hindsight-api.chantx.top`; it reads `bank_id` and optional `retain_context` from `~/.hermes/hindsight/config.json` before any remote-write receipt or POST. Missing, empty, wrong-type, or URL-unsafe Bank values fail closed with no environment-variable or hard-coded fallback. A missing `retain_context` uses the retired provider's default `conversation between Hermes Agent and the User`; null/empty omits it. The Hindsight item otherwise matches the retired manual `/retain` contract: `content`, optional configured `context`, `document_id=session_id`, and `update_mode=replace`; it sends no Retain `tags`, `metadata`, `timestamp`, or `occurred_at`. Attempt/schema/hash/count facts stay only in the local candidate, manifest, and journal. Submission remains asynchronous with `operation_id=attempt_id`.
- The remote receipt sequence is `remote_write_started` before POST, then `remote_write_accepted` only after a matching Bank/item/async/operation response. Definite 4xx rejection and transport/response uncertainty are distinct receipts. An uncertain POST is never retried; the monitor queries the original deterministic operation. The Quick Command returns `scheduled`; later receipts separately prove extraction, acceptance, operation completion, and exact Document content.
- The daily Hindsight attempt monitor takes a shared journal lock, preserves earlier valid attempts after a torn final JSONL line while emitting a dedicated high alert, and distinguishes lost scheduled workers, local interruption, unverified-reconciliation blocks, single-role visible-event gaps, severe total gaps, invalid confirmed-only repair scopes, write-not-started/rejected/uncertain, operation missing/pending/stalled/failed/unavailable/identity or metadata mismatch, extraction errors, Document identity/missing/unavailable, severe content loss, and exact-hash mismatch. Either blocked-candidate receipt produces one explicit alert without a second false “write not started” alert. A historical confirmed-only repair may bypass the live full-session size comparison only when its candidate audit proves one unchanged base hash, unique selected occurrence IDs, `old+inserted=candidate` message counts, and preservation of all old messages as an ordered subsequence; malformed or partial repair receipts fail closed. When an older unguarded attempt completed and the remote Document exactly matches its severe incomplete candidate, that same candidate alert carries the completed remote status so reports state that the remote Document was overwritten rather than calling the remote impact unknown. Remote success requires a completed `retain`/`batch_retain` operation with an explicit integer `extraction_errors_count=0`, matching operation and Document identities, and exact `Document.original_text` hash. A later `remote_write_started` generation supersedes older current-Document comparisons without claiming that the newer operation succeeded.
- New writer Documents are owned by this attempt/operation/hash audit and are excluded from the retired provider-ledger/unmapped Document audit. Legacy StateDB and SQLite ledgers are opened with URI `mode=ro` plus `PRAGMA query_only=ON`; writer, remote Document audit, and legacy shared-bank audit all use the Bank loaded from the default profile's Hindsight config. StateDB is cross-evidence only and cannot substitute for the fsynced attempt intent.
- Retain StateDB cross-evidence treats the configured `--state-db` as the entry point to one Hermes home: it matches the exact session ID against the default StateDB and every `profiles/*/state.db`, then passes that exact resolved database to both the request-cutoff snapshot and candidate exporter. A previously recorded `session_found=false` snapshot is rebuilt at its original cutoff when the exact session is now found, so fixing profile discovery restores the content check instead of merely suppressing the missing-session alert. It never chooses a recent session, combines profiles, changes the remote write target, or suppresses a session that is absent from every StateDB.
- The writer/exporter are managed Fork components. The machine-local daily monitor and deployment configuration remain profile-local; all stay outside the retired Hindsight provider chain, `/new`, generic `/undo`, and CLI/TUI/Desktop lifecycles.

Merge decision:

- Preserve when: a local exec Quick Command needs the exact current Hermes session identity without a dedicated built-in command.
- Drop when: upstream provides an equivalent opt-in child-process context that passes the same isolation, no-fallback, and concurrency tests on Gateway.
- Ask user when: changing the environment variable, Bank, stable ID mapping, or replace semantics; adding route fallback/session creation, automatic retry/replay, other surfaces, or coupling to `/new` or memory-provider lifecycles.

Verification:

```bash
.venv/bin/python -m pytest -q -o 'addopts=' tests/fork/test_gateway_quick_command_session_env.py tests/fork/test_hindsight_retain_integrity.py tests/fork/test_langfuse_hindsight_export.py tests/cli/test_quick_commands.py
python3 -m py_compile gateway/run.py fork_features/hindsight_retain/retain_integrity.py fork_features/hindsight_retain/langfuse_hindsight_export.py tests/fork/test_gateway_quick_command_session_env.py tests/fork/test_hindsight_retain_integrity.py tests/fork/test_langfuse_hindsight_export.py /Users/robot/.hermes/scripts/check-hermes-hindsight.py /Users/robot/.hermes/scripts/hindsight_monitor_html.py
git diff --check
```

- Upstream status: fork-only
- Last validated: upstream `64a6f42cb38def7ad6524bdfe640a16997c88760`; Fork working tree based on `4e00fb68f5fe583cb7c44a124fa59c91bf40aa0f` (uncommitted)
- Feature docs: this maintenance entry; Fork-owned scripts expose `schedule`, internal `execute-scheduled`, legacy immediate `export`, and read-only `scan`

### Telegram quick-command menu discovery

- ID: `telegram-quick-command-menu`
- Status: active
- Depends on: none — uses upstream config-defined Quick Commands and Telegram menu pipeline
- Source boundary: logical-only

Files / touchpoints:
- `hermes_cli/commands_platforms.py` — menu discovery and candidate composition only
- `tests/fork/test_telegram_quick_command_menu.py` — config-to-menu and real adapter registration seam

Intent / invariants:
- Telegram menus include valid config-defined `exec` and `alias` Quick Commands without executing them. Only literal `[a-z0-9_]{1,32}` names are published: no sanitization/truncation that would break exact execution lookup. Malformed entries are skipped; descriptions use the configured text or a generic fallback, never the shell command or alias target.
- Built-in names and aliases retain precedence; same-name quick commands do not duplicate or replace their menu entry. Quick Commands precede colliding plugins/skills, matching dispatch. Explicit menu priorities and upstream common-command priorities remain first, then configured Quick Commands before unprioritized built-ins/plugins/skills. Existing menu cap and hidden-count behavior remain shared.
- Default/private/group/forum registration all use the same generator. Discovery uses the current Hermes home's read-only config loader; no global bot registration, command execution, permission, restart, Retain, CLI/TUI/Desktop or other-platform behavior is changed. Menu refresh follows the existing adapter registration lifecycle, not a new hot-reload mechanism.

Merge decision:
- Preserve when: upstream Telegram discovery omits config-defined Quick Commands.
- Drop when: upstream covers the same names, precedence, priority and adapter-level behavior and the user accepts replacement.
- Ask user when: changing dispatch, adding name rewriting or automatic menu refresh, or changing which commands win collisions.

Verification:
```bash
scripts/run_tests.sh tests/fork/test_telegram_quick_command_menu.py tests/hermes_cli/test_commands.py tests/gateway/test_telegram_forum_commands.py tests/cli/test_quick_commands.py tests/fork/test_gateway_quick_command_session_env.py tests/fork/test_multi_telegram_accounts.py
```
- Upstream status: fork-only
- Last validated: upstream `72a3277cd7937fd0f0a2a3e3fddbed21d7b1c8bd` (local reference, no fetch); Fork based on `85dcb613d4` plus this change. After the user restarted Gateway, Telegram `getMyCommands` confirmed `retain`, `doctor`, `disk` and exactly one built-in `restart` in the default/private menus of all four configured bots; no live command execution was triggered.
- Feature docs: none — small menu-only change; this entry is the complete maintenance contract

### 10. Custom hosted STT provider

Status: active; plugin-owned boundary (2026-08-30)

Date: 2026-05-22; Qwen Audio 3.0 update 2026-08-01; custom keyword
context 2026-08-02; moved to the official transcription-provider plugin seam
2026-08-30

Files:

- `plugins/qwen_stt/plugin.yaml`
- `plugins/qwen_stt/__init__.py`
- `tools/transcription_tools.py` (generic plugin dispatch only)
- `agent/transcription_registry.py` (generic plugin registry only)
- `tests/fork/test_custom_stt.py`
- `tests/fork/test_qwen_stt_plugin.py`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- Provides the existing `stt.provider: custom_api` behavior through the bundled
  `qwen-stt` plugin. The plugin supports generic OpenAI-compatible multipart and
  chat-completions endpoints plus Alibaba DashScope multimodal ASR, with
  `qwen-audio-3.0-asr-flash` as the Fork default.

What changed:

- Moved all `stt.custom_api` defaults, credential resolution, request
  construction, keyword/Prompt mapping, response parsing and error envelopes
  from `tools/transcription_tools.py` into `plugins/qwen_stt/__init__.py`.
- The plugin registers the historical Provider name `custom_api` through
  `PluginContext.register_transcription_provider()`. Existing `stt.provider`,
  `stt.custom_api`, `STT_CUSTOM_API_*` and `QWEN_API_KEY` configuration remains
  valid; no user STT configuration migration is required.
- Removed `custom_api` from the core built-in/reserved Provider sets and removed
  its special dispatch branch. `tools/transcription_tools.py` now reaches it only
  through Hermes' generic plugin dispatcher.
- Removed the custom STT schema/default block from core `DEFAULT_CONFIG`; the
  plugin owns mode-aware defaults while `load_config()` continues to preserve
  the user's `stt.custom_api` mapping.
- Core `DEFAULT_CONFIG` also follows upstream's strict selection behavior and
  does not seed `stt.provider`. Existing explicit `stt.provider: custom_api`
  remains authoritative and continues through the bundled plugin.
- Preserved generic multipart uploads, DashScope-style chat completions and
  `dashscope_multimodal`. The latter sends an ordered, de-duplicated keyword list
  and optional Prompt as an `input_text` context message immediately before the
  Base64 audio message, requests a non-SSE response and reads `output.text`.
- Preserved common response parsing, dotenv/API-key lookup, configuration-over-
  environment precedence, legacy endpoint mode inference and pre-request
  rejection of unsupported mode names.
- Added a public-boundary regression that enables the real bundled plugin through
  `PluginManager`, verifies `custom_api` registration and calls
  `transcribe_audio()` through the existing Gateway-facing path.

Why it matters:

- Qwen/custom STT remains behavior-compatible while vendor HTTP logic no longer
  lives in the high-churn 3,000+ line core transcription dispatcher.
- Future upstream merges only need to preserve the general plugin registration
  and dispatch seam; Qwen request semantics are reviewed in one isolated plugin.

Merge protection:

- Preserve `plugins/qwen_stt` and its registration name `custom_api`; do not
  restore Qwen/custom HTTP logic as a core built-in Provider.
- Preserve explicit `stt.provider: custom_api` behavior and do not silently fall
  back to another STT Provider when the plugin is unavailable.
- Preserve `api_key_env` lookup through `get_env_value()` so keys in
  `~/.hermes/.env` work.
- Preserve the Qwen Audio 3.0 configuration shape: `QWEN_API_KEY`, model
  `qwen-audio-3.0-asr-flash`, base URL `https://dashscope.aliyuncs.com`, endpoint
  `/api/v1/services/aigc/multimodal-generation/generation`, and
  `dashscope_multimodal` mode.
- Preserve `stt.custom_api.prompt`/`keywords` and the DashScope `input_text`
  context mapping; do not move keywords into the audio item or send an empty
  context message.
- Preserve the generic plugin dispatcher in `tools/transcription_tools.py` and
  the registration hook in `agent/transcription_registry.py`; Provider-specific
  behavior belongs in the plugin.

Verification:

```bash
./venv/bin/python -m pytest tests/fork/test_custom_stt.py tests/fork/test_qwen_stt_plugin.py tests/tools/test_transcription.py tests/tools/test_transcription_dotenv_fallback.py tests/hermes_cli/test_config.py tests/gateway/test_stt_config.py -q -o 'addopts='
./venv/bin/python -m py_compile plugins/qwen_stt/__init__.py tools/transcription_tools.py agent/transcription_registry.py
```

Feature docs: none — this focused Provider plugin is fully described here.

Upstream status: fork-only plugin; generic registration/dispatch seam is upstream.

### 11. Custom Qwen/DashScope TTS provider

Status: removed on user request (2026-08-30); do not revive

Date introduced: 2026-05-28; removed 2026-08-30

Current files:

- `tests/fork/test_custom_tts_removed.py`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- The entire core `custom_api` TTS Provider was removed, including both the
  Qwen/DashScope multimodal mode and the generic custom HTTP `/audio/speech`
  mode. The default Profile now uses the upstream Edge Provider and no longer
  contains a `tts.custom_api` block.

What was removed:

- `custom_api` from `BUILTIN_TTS_PROVIDERS`, Provider length limits,
  `agent.tts_registry` reserved names, availability detection, synthesis
  dispatch and hard-coded Opus routing.
- Qwen/custom TTS defaults, configuration resolution, request construction,
  bounded response parsing, Base64/URL audio extraction and active Fork tests.
- The old `tests/fork/test_custom_qwen_tts.py` active behavior suite.

Why it matters:

- The user no longer needs this feature. Removing the complete maintenance unit
  eliminates recurring merge work in the high-churn TTS tool instead of leaving
  disabled dead code or a partial generic mode.

Merge protection:

- Do not restore `custom_api` as a core/native TTS Provider during conflict
  resolution or from older Fork history.
- Do not restore Qwen/DashScope defaults, `tts.custom_api`, generic custom HTTP
  TTS, Qwen-specific Opus routing, or the deleted active behavior tests unless
  the user explicitly requests the feature again.
- Keep the removal regression: the core TTS module must not contain the removed
  Provider name, and `agent.tts_registry` must not reserve it.

Verification:

```bash
./venv/bin/python -m pytest tests/fork/test_custom_tts_removed.py tests/agent/test_tts_registry.py tests/tools/test_tts_plugin_dispatch.py tests/tools/test_tts_command_providers.py tests/tools/test_tts_opus_routing.py tests/tools/test_tts_max_text_length.py -q -o 'addopts='
```

Feature docs: none — historical removal is recorded here to prevent revival.

Upstream status: reverted Fork-only feature.


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

Fourth-batch responsibility boundary (2026-08-30):

- The Fork-owned placement and legacy-sidecar policy now lives in
  `fork_features/request_context.py`. The conversation loop, max-iteration
  helper, and focused tests import that policy directly; `agent/turn_context.py`
  only collects the current values and no longer defines or re-exports the
  provider-specific placement rules.
- The Fork-owned logical-scope priority, explicit-key precedence, and Codex
  `session_id` / `thread-id` / `x-client-request-id` alignment now live in
  `fork_features/prompt_cache_routing.py`. The Responses transport retains its
  provider-neutral key hashing and delegates only the Fork routing decision.
- `fork_features/request_fork` remains a separate compression-checkpoint service
  for frozen provider-native requests. It is not the request-only Recall
  container and must not absorb this policy.
- Fixed upstream `66666f6e2eca0ae883195a34c66131985ea7dd06`
  intentionally persists `api_content` and its own
  `test_next_turn_replays_previous_turn_bytes` requires exact next-turn replay.
  That is an upstream cache-first contract, not an accidental omission. This
  Fork deliberately keeps the current-turn-only contract instead.
- The same upstream snapshot has a reusable compression-lineage resolver, but
  adopting it wholesale would change this Fork's real routing: upstream feeds
  physical/root scope into content hashing, while the Fork gives Gateway keys
  and compression roots explicit logical-key precedence and mirrors that key in
  `thread-id`. Keep the exact existing Fork outputs until live evidence and a
  separate user decision justify a cache-bucket migration.

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

- `agent/agent_init.py`
- `hermes_state_messages.py`
- `tests/agent/test_turn_finalizer_cleanup_guard.py`
- `tests/hermes_state/test_hermes_state.py`
- `fork_features/request_context.py` (Fork placement and sidecar policy)
- `fork_features/prompt_cache_routing.py` (Fork scope/header policy)
- `agent/turn_context.py` (`build_turn_context` collection and
  `build_api_messages` request-copy application seam)
- `agent/conversation_loop.py` (active-redirect request marker, turn-state
  propagation, and early-return cleanup)
- `agent/turn_iteration_prep.py` (current-user index re-anchoring after repair)
- `agent/turn_request_assembly.py` (MoA reference/aggregator request view)
- `agent/codex_responses_adapter.py`
- `agent/chat_completion_helpers.py` (summary/cache seam)
- `agent/model_metadata.py`
- `agent/session_persistence.py`
- `agent/turn_finalizer.py`
- `agent/transports/codex.py` (one routing-policy call)
- `run_agent.py`
- `gateway/run.py`
- `gateway/session_transcript.py`
- `gateway/slash_commands.py`
- `hermes_cli/cli_commands_mixin.py`
- `hermes_state.py` / `hermes_state_messages.py` (schema compatibility and
  rewrite-time sidecar exclusion)
- `tests/agent/test_api_content_sidecar.py`
- `tests/agent/test_model_metadata.py`
- `tests/agent/test_gateway_turn_sidecar.py`
- `tests/agent/transports/test_codex_transport.py`
- `tests/gateway/test_replay_entry_fields.py`
- `tests/agent/test_steer.py`
- `tests/agent/test_turn_finalizer_iteration_limit_exit.py`
- `tests/agent/test_run_agent_codex_responses.py`
- `tests/agent/test_codex_app_server_integration.py`
- `tests/agent/test_codex_request_only_memory_context.py`
- `tests/fork_features/test_request_context_policy.py`
- `tests/fork_features/test_long_task_continuity_recovery.py`

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
- Preserve Hindsight P5/synchronous recall, the model-visible `hindsight_retain`
  tool, generic `/undo`, multi-Telegram account routing, and upstream Gateway
  lifecycle improvements. Do not revive the retired built-in/provider-owned
  `/retain` chain; the config-defined session-aware Quick Command is a separate
  connector and must remain outside Hindsight, `/new`, and `/undo` lifecycles.
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

- The 2026-08-30 fourth-batch boundary migration was observed RED before the
  policy modules existed (`ModuleNotFoundError` for
  `fork_features.prompt_cache_routing`). After migration, direct old/new
  comparisons matched for 720 request-placement combinations, 16 content
  combinations, 24 logical-scope cases, and 8 Codex body/header routing cases.
- Current canonical focused command, rerun on 2026-08-31:

  ```bash
  scripts/run_tests.sh tests/fork_features/test_request_context_policy.py tests/fork_features/test_long_task_continuity_recovery.py tests/agent/test_api_content_sidecar.py tests/agent/test_model_metadata.py tests/agent/test_gateway_turn_sidecar.py tests/agent/transports/test_codex_transport.py tests/gateway/test_replay_entry_fields.py tests/run_agent/test_run_agent_codex_responses.py tests/run_agent/test_codex_app_server_integration.py tests/agent/test_codex_request_only_memory_context.py -q
  ```

  Result: `345 passed`, `0 failed` across `10` files.
- The historical direct-pytest pre-review focused request-only, Codex, Gateway
  replay, app-server, long-task request-context, transport, and summary suite
  reported `382 passed` with `7` third-party deprecation warnings. The adjacent
  MoA, Fork, compression, replay, Session/branch/Undo, lineage, and state suite
  reported `1155 passed` plus `4` existing failures; a detached clean `HEAD`
  reproduced all four failures exactly, so they are not part of this maintenance
  unit.
- Independent `xai-oauth/grok-4.6` `xhigh` read-only review returned `PASS`
  with `0` blocking findings. Its only applicable non-blocking finding was the
  missing boundary-test entries in this section's Primary files; both entries
  were added before commit.

Upstream status: intentional fork divergence from persistent `api_content`
replay; compatible upstream schema and content-addressed key hardening retained.


### 13. Multi Telegram bots in one profile (account_id session slots)

Status: active; Fork policy isolated (2026-08-31)

Date: 2026-07-13; boundary refactored 2026-08-31; restored completion
routing fixed 2026-09-22

Stable ID: `F-telegram-multi-account`

User value:

- One Hermes profile can run a primary Telegram bot plus named bots. They share
  config, models, Skills, plugins, and long-term memory while keeping independent
  current sessions, exact return-bot routing, cross-bot resume, and independent
  failure/reconnect ownership.

Upstream equivalence:

- None at fixed review point
  `upstream/main@a9c783f21995723c812dcb2f8ae58bc6a4323e2f`.
- Official multi-profile gateways isolate profile configuration and memory, so
  they do not replace the same-profile shared-brain contract.

Decision: `retain-fork`

- Preserve all existing behavior and isolate its policy. Do not replace it with
  multi-profile gateways, rename the account identity generically, or introduce
  an Adapter registry without a second real consumer.

Fork-owned boundary:

- `fork_features/multi_telegram_accounts/identity.py`: environment discovery,
  account normalization, and account-aware session-key suffixes.
- `fork_features/multi_telegram_accounts/runtime.py`: named-adapter lookup,
  startup, fatal handoff, independent reconnect, and shutdown.
- `fork_features/multi_telegram_accounts/session_routing.py`: cross-bot resume
  ownership checks, route transfer, and stale-route cleanup.

True upstream host seams:

- `gateway/config.py` supplies the active secret scope and stores the discovered
  named-account configs.
- `gateway/session.py`, `gateway/platforms/base.py`, and
  `plugins/platforms/telegram/adapter.py` preserve account provenance from an
  inbound Telegram event through runtime routing and the durable account-aware
  session key; the account field itself remains runtime-only.
- `gateway/authz_mixin.py` asks the Fork runtime for the exact named adapter and
  restores post-restart account provenance from the trusted durable key; it
  fails closed while that configured bot is disconnected.
- `gateway/slash_commands.py` delegates cross-bot resume policy and keeps account
  provenance in restart metadata.
- `gateway/run.py` creates the runtime and keeps only thin startup, fatal,
  reconnect-watcher, shutdown, and status handoff calls.

Primary files:

- `gateway/session_recovery.py` — recovery compares routing identity via
  `split_account_session_key`, so a row for another account_id is never adopted.
- `gateway/run_adapters.py` — `_queue_retryable_fatal_adapter` re-queues a retryable fatal adapter
  instead of dropping that account's slot. Pinned by `tests/fork/test_multi_telegram_accounts.py`.
- `gateway/config_env.py`
- `gateway/run_startup.py`
- `gateway/run_notifications.py`
- `fork_features/multi_telegram_accounts/__init__.py`
- `fork_features/multi_telegram_accounts/identity.py`
- `fork_features/multi_telegram_accounts/runtime.py`
- `fork_features/multi_telegram_accounts/session_routing.py`
- `gateway/session.py`
- `gateway/config.py`
- `gateway/platforms/base.py`
- `gateway/authz_mixin.py`
- `gateway/slash_commands.py`
- `gateway/run.py`
- `plugins/platforms/telegram/adapter.py`
- `tests/fork/test_multi_telegram_accounts.py`
- `tests/fork_features/test_multi_telegram_accounts_boundary.py`
- `tests/fork_features/test_multi_telegram_accounts_identity.py`
- `tests/fork_features/test_multi_telegram_accounts_runtime.py`
- `tests/fork_features/test_multi_telegram_accounts_session_routing.py`
- `tests/gateway/test_background_process_notifications.py`
- `tests/gateway/test_resume_command.py`
- `tests/gateway/test_restart_notification.py`
- `tests/gateway/test_runner_fatal_adapter.py`
- `tests/gateway/test_platform_reconnect.py`
- `tests/gateway/test_shutdown_cache_cleanup.py`
- `tests/gateway/test_telegram_auth_check.py`
- `tests/gateway/test_telegram_callback_auth_fail_closed.py`
- `docs/chantxu64/multi-telegram-accounts/README.md`
- `docs/LOCAL_MODIFICATIONS.md`

Behavior contract:

- `TELEGRAM_BOT_TOKEN_<ACCOUNT>` retains its existing naming, account
  validation, duplicate-token, and primary-token rules.
- The primary token keeps the legacy session key and default adapter slot.
- Named bots use `SessionSource.account_id` plus `:account:<id>` session-key
  suffixes while real Telegram user/chat IDs remain available for ownership.
- Async completion reconstructs a missing runtime account from the trusted
  session-key suffix after persisted-source reload, rejects conflicting account
  data, and never borrows the primary Bot when the named adapter is offline.
- Streaming, typing, media, busy replies, background completion, restart notices,
  authorization, and normal replies return through the originating bot.
- Cross-bot `/resume` transfers only an idle transcript to the current bot route;
  a running target remains rejected.
- A named bot enters only its own fatal/reconnect slot and never replaces,
  disconnects, or populates the primary Telegram retry slot.
- Named bots remain ordinary DM sessions; Telegram DM Topics and per-account
  `/update` lifecycle routing remain outside this feature.

Effective exposure and lifecycle evidence:

- All seven host paths changed upstream during the measured history window;
  `gateway/run.py` was the hottest at 465 upstream commits.
- The refactor changed host policy by `gateway/run.py +42/-351`,
  `gateway/config.py +17/-71`, `gateway/session.py +5/-43`, and
  `gateway/slash_commands.py +19/-37` relative to the pre-refactor Fork.
- At the same fixed upstream SHA, the candidate added zero conflict paths, zero
  conflict hunks, and zero Telegram-policy conflict hunks. Existing unrelated
  conflicts in `gateway/run.py` and `gateway/slash_commands.py` remain visible;
  they are not claimed as fixed by this unit.
- Pre-refactor focused baseline: `125 passed`.
- Disposable sample focused suite: `148 passed`.
- Final host-integrated canonical suite, including shutdown: `151 passed`.
- 2026-09-22 named-account completion regression: the combined canonical
  delegation and multi-account suite reported `427 passed`; it covers direct
  reconstruction, post-restart origin reconstruction, actual named-Bot
  injection, conflicting identity, and offline fail-closed behavior.
- Input-transformation counterexamples cover blank/case/invalid account IDs,
  duplicate primary and named tokens, and valid/invalid existing key suffixes.
- Broad `ty` comparison changed `399` baseline diagnostics to `398` candidate
  diagnostics with zero new unique diagnostics; the new Fork package itself
  passes a direct type check.

Verification:

```bash
scripts/run_tests.sh tests/fork_features/test_multi_telegram_accounts_boundary.py tests/fork_features/test_multi_telegram_accounts_identity.py tests/fork_features/test_multi_telegram_accounts_runtime.py tests/fork_features/test_multi_telegram_accounts_session_routing.py tests/fork/test_multi_telegram_accounts.py tests/gateway/test_background_process_notifications.py tests/gateway/test_resume_command.py tests/gateway/test_restart_notification.py tests/gateway/test_runner_fatal_adapter.py tests/gateway/test_platform_reconnect.py tests/gateway/test_shutdown_cache_cleanup.py tests/gateway/test_telegram_auth_check.py tests/gateway/test_telegram_callback_auth_fail_closed.py -q -o 'addopts='
```

Merge-time semantic review:

- Recheck the primary legacy key, account suffix, trusted-key restoration of
  runtime account provenance, exact named-adapter lookup, all return-path metadata,
  cross-bot resume ownership, independent named reconnect, and Gateway survival
  when the primary bot is down but a named bot remains live or queued.

Rollback and deletion:

- Roll back the Fork package, five thin host-policy edits, and related tests as
  one maintenance unit. Do not remove the runtime call while leaving account
  suffixes or provenance fields behind.
- Delete only after upstream provides an equivalent same-profile multi-bot,
  shared-brain, independent-session and independent-reconnect design and this
  behavior contract passes against it.

Feature docs: `docs/chantxu64/multi-telegram-accounts/README.md`

Upstream status: fork-only; no equivalent merged at the fixed review point.

### 15. Telegram tool-progress literal-text rendering

Status: policy extracted to `fork_features` (2026-08-30)

Date: 2026-07-16; boundary refactored 2026-08-30

Files:

- `gateway/run_turn.py`
- `fork_features/telegram_tool_progress.py`
- `gateway/run.py`
- `gateway/run_turn_runner.py`
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

- `fork_features/telegram_tool_progress.py` owns the Telegram-only metadata
  decision. It copies Telegram metadata before adding `plain_text=True` and
  returns every non-Telegram metadata object unchanged.
- `GatewayRunner` and `TurnRunner` keep progress creation, accumulation, topics,
  reply metadata, edits, rollover, typing, approvals, and final replies. The
  progress delivery call uses a thin alias imported from the Fork policy module;
  the core no longer contains a Telegram platform branch for this behavior.
- The Telegram adapter bypasses both rich-message delivery and MarkdownV2
  conversion for that marker, including finalized accumulated bubbles and
  overflow continuations. Regexes, code fragments, URLs, backticks, pipes, and
  spoiler-like tokens therefore display literally.
- A fork-protection test keeps the Telegram-only metadata contract and literal
  send/edit behavior visible during future upstream merges.
- `TurnRunner` excludes Telegram from the generic fenced terminal-block path.
  Telegram terminal command previews normalize whitespace while retaining the
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

- Keep the Telegram-only tool-progress metadata decision in
  `fork_features/telegram_tool_progress.py`; do not move the platform branch
  back into `gateway/run.py`. Keep generic `plain_text` rendering in the
  Telegram adapter.
- Keep the Fork-owned behavior tests that pass Terminal progress through
  `TurnRunner` and assert Telegram emits no fenced block in normal or verbose
  mode. Generic fenced-block tests must use a non-Telegram Markdown platform.
- Preserve when: Telegram tool-progress still routes dynamic arguments through
  a Markdown or rich-message parser without an equivalent literal-text guard.
- Drop when: upstream supplies equivalent all-tool Telegram literal delivery
  with coverage for normal sends, edits, and overflow continuation.
- Ask user when: upstream introduces a platform-wide message-kind or
  per-tool-display system with different progress metadata semantics.

Verification:

```bash
scripts/run_tests.sh tests/fork/test_telegram_tool_progress_literal_text.py tests/gateway/test_run_progress_topics.py tests/gateway/test_telegram_rich_messages.py
.venv/bin/python -m py_compile fork_features/telegram_tool_progress.py gateway/run.py gateway/run_turn_runner.py plugins/platforms/telegram/adapter.py tests/fork/test_telegram_tool_progress_literal_text.py tests/gateway/test_run_progress_topics.py tests/gateway/test_telegram_rich_messages.py
git diff --check
```

Feature docs: none — a Fork metadata policy, one Gateway alias, generic adapter
rendering, and focused runtime contracts fully define this behavior.

Upstream status: fork-only.

### 17. Self-contained Clarify decision cards

Status: policy extracted to `fork_features` (2026-08-30)

Date: 2026-07-16; boundary refactored 2026-08-30

Files:

- `apps/desktop/src/store/clarify.ts` — choice validation (non-empty, <=200 chars after the
  recommended-marker strip, no newlines) for the decision card.
- `apps/desktop/src/components/assistant-ui/clarify-tool.tsx` — desktop rendering of the card.
- `fork_features/clarify_decision_card.py`
- `tools/clarify_tool.py`
- `gateway/run_turn_runner.py`
- `hermes_cli/cli_modal_mixin.py`
- `tui_gateway/agent_callbacks.py`
- `tui_gateway/server.py`
- `tests/fork_features/test_clarify_decision_card.py`
- `tests/tools/test_clarify_tool.py`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- Clarify tool calls must carry enough context in the rendered question for the
  user to make the decision without unseen or earlier assistant prose.

What changed:

- `fork_features/clarify_decision_card.py` owns the self-contained,
  decision-first, scope/impact/recommendation, and standalone-choice guidance.
- `tools/clarify_tool.py` keeps the official batch Schema and one pure
  `apply_decision_card_policy` call. Canonical choices remain unchanged strings;
  the first-choice recommendation is separate callback metadata.
- Direct policy transformation tests live with `fork_features`; the Host test
  file retains rendered Schema, callback and registry integration coverage.
  Moving the policy tests does not replace Host integration with policy-only tests.
- Applying the policy returns a deep copy and extends the official
  `questions[]` shape additively without changing that API.
- Action and approval questions must briefly state the current situation,
  proposed action and scope, material impact or trade-off, and a recommendation
  when one exists.
- References such as `above`, `earlier`, or `the recommended scope` cannot stand
  in for the omitted context.
- Selectable answers remain separate `choices`. Gateway, CLI and TUI callbacks
  receive the recommendation index separately and add the display label only at
  their rendering boundary, so callback values and returned answers stay clean.

Why it matters:

- A real Clarify call asked whether to apply “the recommended scope” while its
  assistant message contained no visible prose. The user could not know what
  was being approved and had to ask for the recommendation separately.

Merge protection:

- Keep Fork decision-card text and composition logic in
  `fork_features/clarify_decision_card.py`; the Clarify tool may retain only its
  base Schema and one policy application call.
- Preserve when: upstream Clarify guidance still permits context-dependent
  questions that messaging surfaces can render alone.
- Drop when: upstream supplies an equivalent or stronger self-contained
  decision-card contract while keeping choices independently selectable.
- Ask user when: upstream replaces question text with structured context,
  impact, recommendation, or approval fields. Upstream's current `questions[]`
  container alone is already supported by the policy and does not justify
  moving the Fork text back into core.

Verification:

```bash
.venv/bin/python -m pytest tests/fork_features/test_clarify_decision_card.py tests/tools/test_clarify_tool.py -q -o 'addopts='
.venv/bin/python -m py_compile fork_features/clarify_decision_card.py tools/clarify_tool.py tests/fork_features/test_clarify_decision_card.py tests/tools/test_clarify_tool.py
git diff --check
```

Feature docs: none — a Fork policy module, one Schema composition seam, and
behavior contracts fully define the unchanged user-visible guidance.

Upstream status: fork-only.

### 18. First browser navigation opens a fresh tab

Status: policy extracted to `fork_features` (2026-08-30)

Date: 2026-07-16; boundary refactored 2026-08-30

Files:

- `fork_features/browser_first_navigation.py`
- `tools/browser_tool.py`
- `tests/fork/test_browser_first_conversation_tab.py`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- The first `browser_navigate` call in each conversation opens and activates a
  new tab before loading the requested URL; later calls keep their existing
  navigation behavior.

What changed:

- `fork_features/browser_first_navigation.py` owns the conversation marker,
  per-conversation locks, complete-navigation serializer, first-call `tab new`
  policy, failure retry semantics, and model-visible description fragment.
- `tools/browser_tool.py` retains URL safety, backend/session selection, command
  execution, navigation result/snapshot handling, one decorator, and one
  `ensure_first_conversation_tab` call. It no longer stores Fork navigation
  state or implements the tab decision inline.
- Calls for the same task/session ID remain serialized through the complete
  `browser_navigate` result, while different conversations keep independent
  locks. A failed `tab new` does not mark the conversation initialized.
- On the first call only, the policy runs `tab new` before the existing
  `open <url>` command. `agent-browser` activates the new tab as part of that
  command, and the tool description still states the actual behavior.
- This does not bind later backend reconnects to the created tab. Subsequent
  target selection remains unchanged, matching the intentionally minimal scope.

Why it matters:

- The first navigation in a new conversation must not replace a useful page
  that was already open in the connected browser.

Merge protection:

- Keep the marker, locks, serializer, description fragment, and first-tab
  decision in `fork_features/browser_first_navigation.py`; do not move their
  state or policy back into `tools/browser_tool.py`.
- Preserve the one-time marker separately from backend session `_first_nav`
  because Browser resources are cleaned after every agent turn.
- Preserve the command order `tab new` then `open <url>` on the first call and
  plain `open <url>` on later calls in the same conversation.

Verification:

```bash
.venv/bin/python -m pytest tests/fork/test_browser_first_conversation_tab.py -q -o 'addopts='
.venv/bin/python -m py_compile fork_features/browser_first_navigation.py tools/browser_tool.py tests/fork/test_browser_first_conversation_tab.py
git diff --check
```

Feature docs: none — a Fork policy module, one Browser tool seam, and focused
runtime contracts fully define this behavior.

Upstream status: fork-only.


### 19. Clarify attachment replies preserve media paths

Status: policy extracted to `fork_features` (2026-08-30)

Date: 2026-07-28; boundary refactored 2026-08-30

Files:

- `fork_features/clarify_attachment_reply.py`
- `gateway/run_inbound.py`
- `tools/clarify_gateway.py`
- `tools/clarify_tool.py`
- `tests/fork/test_clarify_attachment_reply.py`
- `tests/gateway/test_clarify_active_session_bypass.py`
- `tests/tools/test_clarify_gateway.py`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- A pending Gateway Clarify preserves attachment paths as separate response
  context without corrupting the user's canonical text or selected choices.

What changed:

- `fork_features/clarify_attachment_reply.py` owns the three-way reply
  disposition (pass through, retain pending, resolved) and wraps canonical
  answers with separate attachment context only after successful normalization.
- The Gateway retains authorization, control/update precedence, pending-entry
  lookup, audio transcription, agent-visible media placeholder construction,
  logging, and typing restoration. Its Clarify block now prepares those inputs
  and makes one `resolve_pending_clarify_reply` policy call.
- `tools/clarify_gateway.py` retains numeric, label, and multi-select
  normalization, then uses one `attach_clarify_response_context` call. Thus
  `user_response` keeps its original string/list shape.
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

- Keep empty-audio retention, slash bypass, attachment-aware resolution, and
  response-context wrapping in `fork_features/clarify_attachment_reply.py`;
  do not move those Fork decisions back into `gateway/run_inbound.py` or concatenate
  media context before `tools/clarify_gateway.py` normalizes the response.
- Preserve until upstream's pending-Clarify interception carries agent-visible
  attachment paths in a field separate from canonical choice/text responses.
- Do not move this after normal media processing: the active agent is blocked
  waiting for the Clarify answer, so the early interception must retain the
  media context itself.
- Preserve the string-only callback path when no attachment is present, so
  native platform button adapters remain backward compatible.

Verification:

```bash
.venv/bin/python -m pytest tests/fork/test_clarify_attachment_reply.py tests/gateway/test_clarify_active_session_bypass.py tests/tools/test_clarify_gateway.py tests/tools/test_clarify_tool.py -q -o 'addopts='
.venv/bin/python -m py_compile fork_features/clarify_attachment_reply.py gateway/run_inbound.py tools/clarify_gateway.py tests/fork/test_clarify_attachment_reply.py tests/gateway/test_clarify_active_session_bypass.py tests/tools/test_clarify_gateway.py
git diff --check
```

Feature docs: none — a Fork policy module, two thin host seams, and runtime
contracts define this narrow authenticated Gateway interception behavior.

Upstream status: fork-only.


### 20. Credential cooldown intentional-clear persistence

Status: reverted

Date: 2026-07-28
Reverted: 2026-08-31

Decision:

- The user chose current upstream behavior as authoritative and explicitly
  retired the Fork's stricter intentional-clear semantics.
- Removed the Fork's older 30-minute per-entry Codex probe, its persisted
  `codex_probe_at` state, the generation-matched
  `status_clear_preconditions` writer extension, and the Fork-owned Codex pool
  regression file.
- Retained upstream's `_codex_quota_restored_upstream()` probe,
  `clear_codex_pool_quota_cooldowns()` persistence path, stale-snapshot merge
  protection, and current upstream issue `#43747` regressions.
- The timestamp fixture adjustment originally committed beside this feature is
  unrelated merge compatibility and remains unchanged; it is not part of this
  active maintenance unit.

Merge protection:

- Do not resurrect the retired Fork probe, `codex_probe_at`, or
  generation-matched writer API during future merges. Follow upstream unless
  the user makes a new behavior decision.

Verification:

- A structural retirement check failed before the edit on nine Fork-specific
  references and passed after removal.
- Canonical upstream quota-probe, credential routing, credential-pool, and
  stale-snapshot merge suites passed: `86 passed, 0 failed`.
- `git diff --check` and Python syntax checks passed.

Upstream status: upstream-equivalent accepted at upstream `main`
`26350357d76e4508c8df9304a3374bdc5a6f6220`.


### 30. Plugin-state compare-and-set

Status: active

Date introduced: recorded 2026-09-20 during the upstream-sync audit; the code predates this entry.

- ID: `fork-plugin-state-cas`
- Depends on: none
- Source boundary: logical-only — one method on the existing upstream runtime store

Files:

- `hermes_cli/plugins_state.py` — `compare_and_set(key, *, expected, value)` writes only when the
  on-disk value still equals `expected`, via `utils.atomic_json_write`. A stale expected value is
  rejected instead of clobbering a concurrent writer's state.
- `tests/fork_features/test_plugin_state_cas.py` — pins the stale-expected rejection.

Behavior contract:

- Returns True on a committed write, False when the stored value moved. Callers must treat False as
  "retry or abandon", never as success.

Upstream status: no equivalent at fixed review point `upstream/main@9573f44c`; upstream's store has
only unconditional writes.

Note: this entry was created because the 2026-09-20 sync found the file carrying Fork delta with no
index coverage. The behavior above is read from the code and its test; the original intent was not
recorded at the time and is worth confirming with the author.

### 21. Delivery-ledger session-reset boundary

Status: active fork maintenance

Date: 2026-07-29
Refactored: 2026-08-31

Files:

- `ui-tui/src/app/slash/commands/core.ts` — `/reset` is an alias of `/new` on the TUI surface and
  takes the same fresh-session path (`startFreshSession`), so the boundary fires identically.
- `ui-tui/src/app/submissionCore.ts` — `enqueue(text, display?)` carries a display override so a
  queued submission renders the user's text, not the rewritten command.
- `ui-tui/src/__tests__/createSlashHandler.test.ts`
- `ui-tui/src/__tests__/submissionCore.test.ts`
- `fork_features/delivery_session_boundary.py`
- `gateway/delivery_ledger.py`
- `gateway/slash_commands_session.py` (stable host seam only)
- `tests/fork_features/test_delivery_session_boundary.py`
- `tests/gateway/test_delivery_ledger.py`
- `tests/gateway/test_session_model_reset.py`
- `tests/fork/test_multi_telegram_accounts.py`
- `website/docs/user-guide/messaging/index.md`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- A successful explicit `/new` or `/reset` transitions undelivered final
  responses for the replaced conversation route to terminal `superseded`
  state. Gateway startup recovery cannot inject those old answers into the
  fresh Hermes session.
- Normal same-session crash/restart redelivery remains unchanged. The boundary
  update uses the complete route-qualified `session_key`, leaves other chats
  and Telegram Bot accounts untouched, and is terminal against late send
  acknowledgements or failures from the old turn.

Responsibility boundary:

- `fork_features/delivery_session_boundary.py` owns the Fork policy entry
  point, event-loop offload, best-effort failure handling, and boundary logs.
- `gateway/delivery_ledger.py` continues to own the SQL state transition and
  terminal-state protection. Moving private connections or SQL into
  `fork_features` would only hide data-layer coupling and is not allowed.
- The high-churn reset implementation calls the Fork policy once, only after a
  replacement Session exists. It has no direct Delivery Ledger dependency.

Why it matters:

- Upstream's delivery ledger persists `session_key`, which identifies a stable
  platform route, but not the Hermes Session generation behind that route.
  `/new` intentionally reuses the route, so a failed old response could be
  recovered after restart even though the user had explicitly started fresh.
- At upstream SHA `4f22543509d1b91dc45bcb369447126c5eb14fb7`, the
  maintenance profile counted one path touch per commit from
  `git log --since=2026-05-01 --name-status --find-renames`: 178 touches to
  `gateway/slash_commands.py` versus 6 to `gateway/delivery_ledger.py`. No
  structured sync-conflict records exist for this unit, so the refactor is
  justified as churn isolation rather than a measured conflict reduction.

Merge protection:

- Preserve the one host call after successful replacement-Session creation
  until upstream associates delivery obligations with the originating Hermes
  Session or provides an equivalent terminal supersession mechanism.
- Do not disable ordinary startup redelivery or remove the recovered-reply
  ambiguity marker as a substitute for this boundary check.
- Do not reduce the boundary key to platform or chat id. Named Telegram Bots
  require the existing `:account:<id>` suffix so `/new` on one Bot cannot
  supersede another Bot's pending reply.

Verification:

- TDD RED established three missing-policy-module failures, followed by two
  host-boundary failures while the reset path still called the Ledger directly.
- Canonical focused and adjacent suite: `70 passed, 0 failed`, covering Fork
  policy, exact account-qualified Bot isolation, both `/new` and `/reset`,
  route-scoped supersession, startup-sweep exclusion, late-state terminality,
  reset cleanup, title handling, async delegation, and streaming.
- Python compilation and `git diff --check` passed.

Feature docs: none — this is a narrow lifecycle invariant covered by the
Delivery documentation, regression tests, and this merge note.

Upstream status: the durable delivery ledger is upstream; the explicit
Session-reset boundary remains fork-only.


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

Status: Fork policy/audit isolated behind a Store transaction seam; active after
a process loads this version

Date: 2026-08-08; responsibility boundary refactored 2026-08-31

Files:

- `tools/memory_tool_store.py` — Fork-owned Store surface the governance context reads through:
  `read_entries_checked`, `read_target_entries_checked`, `sanitize_entries_for_snapshot` (threat
  scan), `preview_entries`, `path_for_target`, and the `transaction` / `_mutate` coordinator that
  makes an audited change atomic. Consumed by `background_review.build_memory_governance_context`.
- `agent/inline_tool_executors.py` — inline memory calls go through
  `fork_features.memory_governance.forwarded_memory_kwargs(args)`, so an inline invocation carries
  the same governance kwargs as the tool path.
- `tools/memory_tool.py`
- `fork_features/memory_governance.py`
- `fork_features/memory_audit.py`
- `agent/background_review.py`
- `agent/prompt_builder.py`
- `agent/tool_executor.py`
- `agent/agent_runtime_helpers.py`
- `tests/agent/test_prompt_builder.py`
- `tests/agent/test_memory_write_bridge.py`
- `tests/fork/test_memory_changelog_governance.py`
- `tests/fork_features/test_memory_governance_boundary.py`
- `tests/tools/test_memory_tool.py`
- `tests/tools/test_memory_tool_schema.py`
- `tests/tools/test_write_approval.py`
- `tests/agent/test_run_agent.py`
- `docs/chantxu64/memory-change-governance/README.md`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- Keep autonomous `MEMORY.md` / `USER.md` maintenance while recording the
  evidence and exact semantic loss behind every public mutation.

What changed:

- `MemoryStore` and its private file/lock implementation remain Host-owned.
  `MemoryStore.transaction(...)` exposes one stable coordinator-lock then
  target-lock transaction object. The Fork mutation path reaches private Store
  mutation and rollback state only through that Host-owned transaction; history
  and background context use public read-only Store readers. Fork mutation
  policy no longer imports `tools.memory_tool` or calls a bundle of private
  Store methods.
- `fork_features/memory_governance.py` owns reason/evidence and typed-deletion
  policy, exact trace/rollback orchestration, public memory Schema/Prompt policy,
  background live-context rendering, and the shared governance metadata field
  list. `fork_features/memory_audit.py` owns JSONL baseline/append/parse,
  merge-only lineage, threat scanning and the 8,000-character result bound.
- High-change Prompt/background/execution files now keep only thin composition
  or forwarding calls; the evaluated main Prompt, memory-review Prompt,
  combined-review Prompt and public Schema remain byte/value equivalent to the
  pre-refactor baseline.
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

- Preserve the Host Store/Fork policy/Fork audit responsibility map. Do not move
  Store private calls into Fork modules or duplicate a second Store/audit path.
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
./venv/bin/python -m pytest -q -o 'addopts=' tests/fork_features/test_memory_governance_boundary.py
```

Feature docs: `docs/chantxu64/memory-change-governance/README.md`

Maintenance exposure: at upstream SHA
`4f22543509d1b91dc45bcb369447126c5eb14fb7` and Fork baseline
`b6519e453af5ee74bda5619981e9748d690e5d8f`, counting one path touch per
non-merge commit from
`git log --no-merges --since=2026-05-01 --name-status --find-renames` yielded
34 upstream / 1 Fork-only non-merge touches for `tools/memory_tool.py`, 45 / 1
for `agent/background_review.py`, and 112 / 3 for `agent/prompt_builder.py`.
These are exposure counts, not measured conflict or Token savings. The
repository-local `docs/FORK_SYNC_HISTORY.jsonl` was absent; the external
decoupling ledger contained 13 implementation records but zero sync/follow-up
records, so no measured conflict hunks, resolution time, rework, defect, or
Token evidence was available.

Upstream status: policy/audit behavior remains fork-only at the fixed upstream
SHA above.


### 24. Launchd gateway open-file ceiling

Status: production boundary decoupled to runtime config; fork contract only

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
- No fork production branch remains in `hermes_cli/gateway.py` or
  `hermes_cli/resource_limits.py`; deployment config owns the selected value and
  the fork test guards the generated launchd contract.

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

- Preserve the fork regression that configurable emission still works; do not
  reintroduce a hard-coded 8192 block or a second plist writer.
- Ask user when a replacement removes launchd SoftResourceLimits entirely or
  lowers the operator-chosen floor without an equivalent guarantee.
- Runtime activation still requires the normal service rewrite/restart path;
  static source and plist generation checks must not be reported as a live
  process restart.

Verification:

```bash
./venv/bin/python -m pytest tests/fork/test_launchd_open_file_limit.py tests/hermes_cli/test_gateway_service.py -q -o 'addopts=' -k nofile
./venv/bin/python -m py_compile hermes_cli/gateway.py hermes_cli/resource_limits.py tests/fork/test_launchd_open_file_limit.py
git diff --check
```

### 25. OpenAI API Codex-compatible gateway integration

Status: reverted / historical

Date: 2026-08-14; reverted 2026-08-24

Files:

- `agent/auxiliary_client.py`
- `agent/model_metadata.py`
- `agent/chat_completion_helpers.py`
- `run_agent.py`
- `agent/transports/codex.py`
- `agent/turn_context.py`
- `plugins/image_gen/openai-codex/__init__.py`
- `tests/plugins/image_gen/test_openai_codex_provider.py`
- `tests/agent/test_openai_api_semantic_boundary.py`
- `tests/agent/test_codex_request_only_memory_context.py`
- `tests/agent/test_non_stream_stale_timeout.py`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- The fork-only interpretation of ordinary `openai-api` as a Codex-compatible
  API-key gateway was retired after runtime configuration moved to official
  `openai-codex` OAuth.

What was removed:

- All provider-declared auxiliary transport inheritance introduced by the
  retired gateway change. Explicit `api_mode="codex_responses"` and the older
  `api.openai.com` Codex-model heuristic remain; provider-specific inheritance
  is left to upstream.
- CodexManager private `models[]` metadata parsing, credential-scoped endpoint
  metadata/cache files, and the `openai-api` cache-order exception.
- Codex cache-routing headers and Codex stale-timeout classification for
  `openai-api` Responses requests.
- The fork-added direct non-stream Codex timeout-floor parity; direct requests
  again use the upstream 150/240-second context ceilings.
- The `openai-api` request-only developer-memory exception.
- `OpenAIApiImageGenProvider` and the `openai-api` image-provider alias.
- Fork-only positive tests for the retired gateway behavior.

What remains:

- Official `openai-codex` OAuth Responses routing, cache headers, request-only
  developer context, LCM/plugin request context, image provider, and the
  pre-existing main-request Codex watchdog/timeout floor.
- Generic callers may still select a supported Responses transport explicitly;
  ordinary `openai-api` no longer gains official Codex identity or its private
  exceptions automatically.
- Provider-aware LCM/plugin request context and unrelated later fork fixes.

Merge protection:

- Do not reintroduce the retired `openai-api` Codex gateway behavior from this
  historical entry during future upstream merges.
- Preserve the official `openai-codex` behavior and the negative boundary tests.
- Keep auxiliary provider-declared transport inheritance and direct non-stream
  timeout behavior aligned with upstream; do not restore local parity shims.
- A future API-key Codex gateway integration requires a new explicit user
  decision and fresh tests; it must not be inferred from provider naming.

Verification:

- 2026-08-24: focused migration/boundary suite reported `358 passed`; adjacent
  official Codex transport, TTFB/watchdog, direct Cron execution-path, and
  turn-context suites reported `138 passed`.
- Per operator direction, no runtime type check, Priority-specific check, real
  image generation, Gateway restart, or Cron execution was performed as part
  of the semantic rollback.

Upstream status: historical fork-only behavior; intentionally removed. The
auxiliary and direct non-stream timeout paths were validated against
`upstream/main` at `91e867631e9d2eb9fbd69edd4459475d38070979`.

### 26. Successful STT keeps an explicit voice-origin marker

Status: active

Date: 2026-08-15

Files:

- `gateway/run_inbound.py`
- `apps/desktop/src/app/chat/composer/hooks/use-composer-voice.ts` — `submitVoiceTurn` wraps a
  local-STT transcript in the same marker before submit, so the desktop's own STT button reaches
  the model with voice provenance (added 2026-09-21). GPT-Live does NOT go through here: it passes
  the marker out-of-band via `voiceContext`, keeping bubble and history clean.
  Accepted costs on this surface, for parity with the gateway path: the marker IS the submitted
  text, so the chat bubble renders it and the persisted row stores the wrapper rather than the bare
  transcript. The gateway hides it behind a voice-message bubble; this surface has none.
- `apps/desktop/src/app/chat/composer/hooks/use-composer-voice-stt-marker.test.tsx` — pins the wrap.
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
./venv/bin/python -m py_compile gateway/run_inbound.py \
  tests/gateway/test_stt_config.py \
  tests/gateway/test_telegram_audio_vs_voice.py \
  tests/gateway/test_telegram_voice_v0_regressions.py
./venv/bin/ruff check gateway/run_inbound.py \
  tests/gateway/test_stt_config.py \
  tests/gateway/test_telegram_audio_vs_voice.py \
  tests/gateway/test_telegram_voice_v0_regressions.py
git diff --check
```

Feature docs: none — this is a small message-enrichment compatibility rule fully described here and protected by focused regressions.

Upstream status: fork-only.

### 27. Per-invocation delegation provider/model/reasoning routing

Status: active

Date: 2026-08-15; native Anthropic reasoning probe fixed 2026-08-28;
route-policy boundary refactored 2026-08-31; control/completion observability
fixed 2026-09-22

Files:

- `fork_features/delegation_routing.py`
- `tools/delegate_tool.py`
- `tools/delegate_tool_registry.py`
- `tools/delegate_tool_config.py`
- `tools/delegate_tool_dispatch.py`
- `tools/delegate_tool_child_run.py`
- `tools/async_delegation.py`
- `run_agent.py`
- `gateway/run_notifications.py`
- `tests/fork_features/test_delegation_routing.py`
- `tests/tools/test_delegate.py`
- `tests/tools/test_delegate_control_actions.py`
- `tests/tools/test_async_delegation.py`
- `tests/tools/test_delegate_request_overrides.py`
- `tests/tools/test_delegate_output_schema.py`
- Native argument forwarding is covered by `tests/tools/test_delegate.py` and
  `tests/tools/test_delegate_request_overrides.py`; the formerly listed
  `tests/tools/test_delegate_task_native_args.py` does not exist and is not a runnable gate.
- `website/docs/user-guide/features/delegation.md`
- `website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/user-guide/features/delegation.md`
- `docs/chantxu64/delegate-per-call-routing/README.md`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- Each `delegate_task` invocation or batch item can select a provider, model, and reasoning effort, with cross-provider credentials/API-mode resolution, truthful same-model reasoning fallback, durable safe route metadata, and bounded Markdown retry suggestions.

What changed:

- Top-level and per-task `provider`, `model`, and `reasoning_effort` fields are
  exposed by the public dispatcher, normalized by `delegate_tool_config`,
  resolved before child construction, and forwarded through synchronous and
  background child-run paths.
- Fork-specific route policy now lives in `fork_features/delegation_routing.py`: model/provider inference and catalog validation, bounded current-route suggestions, exact reasoning capability checks, target-route fallback, top-level/per-task precedence, repeated-route caching, full-batch prevalidation, safe route errors, and public child route metadata. `tools/delegate_tool.py` keeps model-facing input normalization, the single Fork resolver call, host-owned generic delegation config and credential/runtime resolution, child construction, execution, and aggregation. The Fork module receives `_resolve_delegation_credentials` through a callback; it does not import the host or duplicate provider credentials. `tools/async_delegation.py` continues owning async task persistence, recovery, and completion delivery.
- Model-only calls infer a provider only when the authenticated curated inventory has one unique match; explicit provider/model calls resolve the target-model runtime route and reject known catalog mismatches before spawning.
- Routed children resolve reasoning configuration against the target model. An explicit effort is used only when the production request builder preserves it exactly; otherwise Hermes keeps the selected provider/model and applies that target model's normal override/global/provider reasoning configuration without claiming that the requested value took effect.
- Exact reasoning probes cover Chat Completions, Responses, and native Anthropic Messages routes. Anthropic Messages reuses the production request builder: `low`, `medium`, `high`, `xhigh`, and `max` are exact; `minimal -> low`, `ultra -> max`, and omission for `none` are not reported as exact and therefore keep the same-model automatic/default fallback contract.
- Every batch task is route-validated before any child starts. Safe effective route metadata is present in child results, async status, SQLite task payloads, restart recovery, and completion events; secrets and raw request overrides are excluded.
- Model-facing `list`, `steer`, and `stop` remain synchronous in both the normal
  `AIAgent` dispatcher and registry fallback; automatic background mode applies
  only to spawn calls. `list` reports retained async units separately from live
  child objects and exposes durable delivery state, so `completed` cannot be
  mistaken for either `not finished` or `delivered`.
- Model-route errors keep structured error codes and render bounded Markdown suggestions in the error text. They combine up to 10 recent frequently used routes with up to 10 name-similar routes, deduplicate by `(provider, model)`, and stay within 1,800 characters. Recent usage is read from the active profile's local `state.db` and intersected with the current authenticated curated inventory, so stale historical routes are excluded. No LLM, child launch, forced catalog refresh, reasoning-model scan, or automatic model substitution is involved; the full catalog is never inserted into the persistent tool schema.

Why it matters:

- The parent can deliberately use a different provider/model for one subtask without changing global configuration, while model-only calls remain convenient and ambiguous routes fail closed. Errors contain enough bounded current information to retry without a separate model-directory tool call or permanent prompt bloat.
- At fixed local upstream SHA `4f22543509d1b91dc45bcb369447126c5eb14fb7` and Fork baseline `6eea4460484f499622e718684bc7e986da4f436f`, the maintenance-profile script counted one path touch per commit since 2026-05-01 using `git log --name-status --find-renames`: `tools/delegate_tool.py` had 119 upstream touches and two Fork-only non-merge touches; `tools/async_delegation.py` had 31 and one. The repository-local `docs/FORK_SYNC_HISTORY.jsonl` was absent. The external decoupling ledger supplied to the profile existed with 13 implementation records but zero sync or follow-up records. Sync-outcome coverage is therefore missing: no measured conflict hunks, resolution time, rework, defects, or Token evidence was available. This is path-change exposure, not a measured conflict- or Token-reduction claim.

Merge protection:

- Preserve when upstream does not provide the combined contract of per-invocation/per-task cross-provider routing, target-model reasoning resolution, truthful same-model automatic/default fallback for unsupported explicit effort, all-task prevalidation, durable safe route metadata, and bounded current-availability Markdown suggestions.
- Preserve the narrow host/Fork boundary: the Fork module owns route policy, while the host owns credential/runtime resolution and child lifecycle and the async module owns durable task storage/recovery. Do not move provider credentials, child execution, or async Ledger state into `fork_features` merely to reduce host line count.
- Preserve tests at the model-facing dispatcher and registry fallback, not only
  direct `delegate_task()` calls. Keep the completed-vs-delivered list regression
  so a control-call failure cannot silently become a status guess.
- Drop when upstream provides an equivalent public schema, routing precedence, validation behavior, persistence/recovery metadata, and error-result budget with matching regressions.
- Ask user when upstream offers a similar interface but silently chooses ambiguous providers, claims a clamped/mapped reasoning value was honored, switches models for reasoning, omits durable per-task routes, injects a full catalog into the schema, or uses materially different precedence/fallback semantics.

Verification:

- 2026-08-16 revised-contract validation: core delegate/control/async tests reported `125 passed in 16.72s`; adjacent DeepSeek/OpenCode Go/Codex request-builder tests reported `158 passed in 1.82s`; restoration, API Server, Gateway binding, CLI delivery, TUI lifecycle, batch/output-schema, and FD-leak tests reported `65 passed in 7.77s` with seven pre-existing third-party deprecation warnings. Ruff, `py_compile`, and `git diff --check` passed. A read-only live-profile candidate render excluded stale historical `openai-codex` routes and returned only current authenticated picker-inventory routes.
- 2026-08-28 native Anthropic regression validation: delegate/control/async tests reported `127 passed`; Anthropic adapter/sanitization tests reported `98 passed`; Ruff, `py_compile`, and `git diff --check` passed. After Gateway restart, a live `custom:cloudflare-claude` / `claude-fable-5` child completed one API call with the requested effective `low` effort instead of falling back to the model's `high` default.
- 2026-08-31 route-policy boundary validation: Fork boundary plus delegate/control/async tests reported `129 passed`; adjacent DeepSeek/OpenCode Go/Codex/Anthropic request-builder tests reported `302 passed`; Ruff, `py_compile`, the structural ownership check, and `git diff --check` passed. Functional validation did not launch a live child or call a target route; one model subagent performed a separate read-only code review. No Gateway restart, commit, or Push was performed.
- 2026-09-22 control/completion reliability validation: the combined canonical
  delegation and multi-account suite reported `427 passed` in per-file isolated
  subprocesses. The new regressions first failed on all observed defects, then
  passed after the fix; Ruff, Python compilation, and `git diff --check` passed.
  No Gateway restart, commit, or Push was performed.

```bash
scripts/run_tests.sh \
  tests/fork_features/test_delegation_routing.py \
  tests/tools/test_delegate.py \
  tests/tools/test_delegate_control_actions.py \
  tests/tools/test_async_delegation.py
scripts/run_tests.sh \
  tests/plugins/model_providers/test_deepseek_profile.py \
  tests/plugins/model_providers/test_opencode_go_profile.py \
  tests/agent/transports/test_codex_transport.py \
  tests/agent/test_codex_request_transport_diagnostics.py \
  tests/agent/test_anthropic_adapter.py \
  tests/agent/test_message_sanitization_policy.py
python -m ruff check fork_features/delegation_routing.py \
  tools/delegate_tool.py tools/async_delegation.py \
  run_agent.py tests/tools/test_delegate.py \
  tests/fork_features/test_delegation_routing.py \
  tests/tools/test_delegate_control_actions.py \
  tests/tools/test_async_delegation.py
python -m py_compile fork_features/delegation_routing.py \
  tools/delegate_tool.py \
  tools/async_delegation.py run_agent.py tests/tools/test_delegate.py \
  tests/fork_features/test_delegation_routing.py \
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

- `pyproject.toml`
- `fork_features/__init__.py`
- `fork_features/approval/__init__.py`
- `fork_features/approval/policy.py`
- `fork_features/approval/runtime.py`
- `fork_features/approval/script_evidence.py`
- `fork_features/approval/smart_review.py`
- `fork_features/approval/retry_policy.py`
- `agent/agent_runtime_helpers.py`
- `agent/conversation_compression.py`
- `agent/tool_executor.py`
- `model_tools.py`
- `tools/approval.py`
- `tools/approval_context.py`
- `tools/approval_smart.py`
- `tools/tirith_security.py`
- `tools/terminal_tool.py`
- `tools/code_execution_tool.py`
- `tests/fork_features/approval/test_policy_boundary.py`
- `tests/fork_features/approval/test_runtime.py`
- `tests/fork_features/approval/test_smart_approval_context.py`
- `tests/tools/test_denial_retry_escalation.py`
- `tests/tools/test_denial_circuit_breaker.py`
- `tests/tools/test_smart_approval_injection.py`
- `tests/tools/test_smart_approval_policy.py`
- `tests/tools/test_execute_code_approval_cluster.py`
- `tests/tools/test_tirith_security.py`
- `tests/fork_features/approval/test_script_evidence.py`
- `tests/fork_features/approval/test_smart_review.py`
- `tests/fork_features/approval/test_retry_policy.py`
- `tests/hermes_cli/test_gateway_restart_loop.py`
- `tests/agent/test_run_agent.py`
- `docs/chantxu64/current-turn-smart-approval/README.md`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- Smart Approval now judges actual risk and current authorization separately,
  using the complete latest real user-authored turn without character truncation, all completed Clarify
  question/answer pairs after that turn, the action about to run, and bounded
  best-effort contents of directly executed entry scripts plus explicitly
  named local Python files. A first Smart Review
  denial exposes one text-similar retry route. Existing legal release mechanisms
  and a later Smart `approve` remain effective; only a second current `deny`
  consumes the similar state and falls back to one-shot human approval. A denial
  or timeout on that repeat card is final for similar repeat actions in the turn
  without changing ordinary approval behavior.

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
  them and sent as bounded evidence. A literal `hermes_tools.terminal(...)`
  command can expose its directly invoked Python script path. Ordinary imports,
  module-name-only dynamic imports, and non-literal terminal commands are not
  expanded. A directly named file recorded by its containing Git index is
  marked `skipped_git_tracked` without reading its source. An untracked directly
  named file is read even inside another Git worktree or outside the current
  cwd; oversized source returns a 32,000-byte `truncated` prefix instead of an
  empty unreadable result. All unique direct-entry paths are collected; duplicate
  paths are deduplicated without hiding later entries. Missing, unreadable,
  Git-tracked/skipped, or truncated script evidence never forces denial or manual review by itself. The
  reviewer must infer likely effects from the command, path, filename, flags,
  argument names and values, cwd, visible side effects, and current authorization;
  external dependency trees are not inspected.
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
  unreadable evidence remains visible without becoming a fail-closed condition.
- Approval language selection reuses Hermes' interface-language resolver
  (`HERMES_LANGUAGE` override, then `display.language`, then English). The
  reviewer receives the resolved language code for `reason`; fixed smart-review
  presentation uses Chinese for `zh`/`zh-hant` and otherwise retains its English
  fallback. A Chinese interface therefore remains Chinese even when the latest
  user message is English or language-neutral. Structured decision/risk/
  authorization values remain stable machine enums.
- The reviewer treats the approval packet as risk evidence rather than an
  execution transcript. Missing proof of prior Runbook reading, testing,
  validation, or other workflow steps is not evidence that those steps were
  skipped. Workflow prerequisites remain main-agent obligations even when
  phrased as `must`, `must not`, `only after`, or `do not continue`; they do not
  become approval prohibitions.
- The reviewer returns `decision`, `risk_level`, `risk_evidence`, `prohibition`,
  `authorization`, and a short semantic `reason`. User context may scope or
  authorize an identified risky side effect, or enforce a direct prohibition
  against the visible action or target. Critical risk is denied; an approval
  whose risk and authorization fields conflict is downgraded to escalation;
  high-risk exact authorization remains eligible for one-operation approval.
- Existing integrations that compare the historical one-word smart decision
  remain compatible. Smart approvals still do not create a permanent broad
  allowlist entry.
- `fork_features/approval/policy.py` is the only public Fork Smart Approval
  facade. It owns the request-local context, latest-real-turn/Clarify composition,
  structured reviewer access, bounded direct-script evidence access, and same-turn
  retry state. The evidence, reviewer, and retry modules remain internal Policy
  implementation and do not import approval/Terminal/`execute_code` hosts.
- `agent/tool_executor.py` keeps the thin adapter that injects the conversation
  subsystem's canonical real-user classifiers into the Fork context builder, and
  binds that context around both the sequential and concurrent production tool
  dispatchers. Worker-thread propagation and final cleanup are exercised through
  those production entries; `model_tools.py` retains only official approval
  observability wiring.
- `fork_features/approval/runtime.py` owns bounded local/remote script reads,
  structured auxiliary-client invocation, language selection and policy assembly.
  It reads official auxiliary/config interfaces but never imports the approval gate.
  Terminal and `execute_code` import evidence readers from this defining module.
- `tools/approval.py` binds gate-owned retry identity/locking to that runtime and
  retains deterministic floors, YOLO/mode/allowlists, Tirith warning keys, verdict
  execution, human approval transport, persistence, observability, and fail-closed.
- Official split runtime state is read from `tools/approval_context.py`;
  `tools/approval_smart.py` retains official fallback/observer behavior. Structured
  model-call tests patch `fork_features.approval.runtime.call_approval_llm`, while
  gate tests still exercise the live sequential/concurrent production entries.
- On the first Smart denial, the agent is told it may submit the same or a
  textually similar operation again only when it remains necessary. Candidates
  are scoped to the same session, verified user turn, and tool kind. Exact
  whitespace-normalized text matches; otherwise a bounded first/last 8,192
  characters use `SequenceMatcher(autojunk=False)` with a `0.86` threshold.
- The second actual action follows the current normal approval path. YOLO/
  mode-off, existing valid allowlists/session approvals, and a current Smart
  `approve` release it normally; a current `escalate` keeps ordinary manual
  approval behavior. Only a second current Smart `deny` consumes a text-similar
  first-denial state and routes the current command or complete Python source to
  the fresh one-shot fallback. Similarity does not prove semantic equivalence,
  reuse authorization, or inspect shell tokens, paths, targets, ASTs, here-doc
  structure, indentation, or effects.
- One-shot retry approval disables session/permanent capabilities on bound
  Gateway callbacks. A client response of `session` or `always` cannot persist
  this approval. Without a live notify callback the route fails closed with
  `approval_unavailable`, queues no dead pending request, and retains the retry
  state so a later same-action request can reach a newly registered callback.
- Only a denial or timeout from the repeat one-shot card creates the same-turn
  similar-action latch. Ordinary manual approval, an ordinary first Smart
  `escalate`, and selected approval transports retain their prior denial and
  timeout behavior. Both a real session identifier and a real turn identifier
  are required, and the state is deliberately in-memory only.
- When the current execution context has no one-shot human route, the fixed
  denial text reports that unavailable route directly; it no longer claims that
  no verified user turn exists.
- Gateway restart/stop remains a deterministic hard block in the running
  Gateway. Repeating it does not create an approval card and does not execute
  the lifecycle action. Computer Use, Cron, cross-tool intent tracking, and
  cross-process retry persistence are outside this behavior.
- The feature intentionally omits file hashes/version binding, external
  dependency graphs, ordinary-import following, non-literal dynamic dependency
  analysis, and cross-tool semantic same-result tracking.

Maintenance evidence for the Policy boundary:

- Fixed refs: upstream `4f22543509d1b91dc45bcb369447126c5eb14fb7`,
  Fork baseline `63f75f3b6d8c7d54d411e88990b400743d474fc5`, since
  `2026-05-01`, one touch per non-merge commit per current path, no historical
  alias collapse.
- Reproduce each count with
  `git log --since=2026-05-01 --no-merges --format='%H' <ref-or-range> -- <path> | sort -u | wc -l`.
- Upstream/Fork-only touches were `125/2` for `tools/approval.py`, `90/3` for
  `agent/tool_executor.py`, `191/7` for `agent/agent_runtime_helpers.py`, `51/1`
  for `model_tools.py`, `112/1` for `tools/terminal_tool.py`, `61/1` for
  `tools/code_execution_tool.py`, `12/1` for `tools/tirith_security.py`, and
  `157/3` for `agent/conversation_compression.py`.
- The external decoupling ledger had 13 implementation records and no sync or
  follow-up records. No measured conflict hunks, semantic decisions, resolution
  time, rework, defects, or Token savings were available; touch counts show path
  exposure only.

Why it matters:

- The approval flow should judge actual outcomes and current authorization, not
  require complete static source visibility. At the same time, an automatic
  denial must not trigger wasteful command rewrites or alternate-tool bypass
  attempts: an existing valid release remains effective, while a repeat that is
  still denied can reach the user once; a real user denial ends that fallback
  scope for the turn.

Merge protection:

- Preserve when upstream Smart Approval cannot consume the latest-turn/Clarify
  authorization boundary, structured risk and authorization fields, bounded
  direct-script evidence across terminal and `execute_code`, localized approval
  presentation, first-Smart-denial/legal-release priority/second-denial
  text-similar one-shot fallback, repeat-card-only same-turn denial latching, the
  unchanged Gateway lifecycle hard block, Fork policy separation, or
  protocol-validated Tirith resolution.
- Drop when upstream provides equivalent request isolation, direct-entry-script
  context without fail-closed source gaps, package-managed development-tool
  handling, structured outcomes, language-aware presentation, compatible-scanner
  selection, text-only denial retry/repeat-card-denial semantics, and
  one-operation persistence behavior with matching regressions.
- Ask the user when upstream uses broader conversation history, omits Clarify
  question scope, treats all high risk as denial, or recursively analyzes a
  materially larger dependency surface.

Verification:

- 2026-08-24 post-correction route tests: 11 legal-release-versus-forced-one-shot tests failed before the withdrawal and passed afterward.
- Focused Fork/retry/Terminal/`execute_code`/context regression: `113 passed`.
- Broader approval/Gateway regression: `351 passed`, `2 failed`, `1 deselected`, with `7` third-party deprecation warnings. The two failures are existing order-dependent redaction tests and each passed in a fresh isolated process (`2 passed`); the deselected macOS `/tmp` alias case remains the unchanged platform baseline.
- Adjacent Smart Approval policy, Terminal, code-execution, Tirith, approval-mode, and interface-language regression: `205 passed`, `7 subtests passed`.
- `py_compile`, Ruff, and `git diff --check` passed.
- 2026-08-25 boundary correction: bounded script-evidence/context `59 passed`; adjacent Smart Approval/`execute_code` wiring `52 passed`; Terminal/code-execution/Tirith/i18n `167 passed`, `7 subtests passed`; isolated approval-mode parity `7 passed`.
- The standalone approval file remained `95 passed`, `1 failed` on the unchanged macOS `/tmp` verification-artifact baseline. Ruff, `py_compile`, and `git diff --check` passed for the boundary correction.
- 2026-08-25 issue 1/2/3 and I1/I2 repair validation: bounded script-evidence and Smart Approval context coverage `127 passed`, including current-worktree versus other-Git-project boundaries, recognized/unrecognized task temporary roots, protected-source and outside-root symlinks, interpreter-option handling, literal nested `hermes_tools.terminal` evidence, `execute_code` approval wiring, and Terminal integration. Ruff, `py_compile`, and `git diff --check` passed; this was targeted local validation only.
- The preceding approval-model test paths were mocked. No production configuration/provider/endpoint/fallback change, Gateway restart, live-main copy, commit, or push was performed.
- 2026-08-30 Git-tracking/oversized-prefix/context correction: focused direct-script and reviewer/context tests `93 passed`; Fork Smart Approval/policy/injection/`execute_code`/retry/latch regression `179 passed`; adjacent Terminal/code-execution/Tirith/approval-mode/i18n/Cron-session regression `280 passed`, `1 deselected`, `7 subtests passed`. The deselected macOS `/tmp` verification-artifact baseline still fails independently and is not part of this change.
- Before local Git tracking, a live collector probe against `/Users/robot/.hermes/scripts/nc_report.py` from the Ontology cwd returned a `32,000`-byte `truncated` prefix. After a local scripts repository tracked and committed only `nc_report.py`, the same probe returned `skipped_git_tracked` with zero source bytes. Two live `openai-codex / gpt-5.6-luna` review-only probes with missing source returned `approve/low/sufficient` for a visible read-only `/tmp` diagnostic and `deny/critical/none` for visible `--delete-all /Users/robot/Documents` under an explicit no-delete instruction; neither command was executed.
- `py_compile`, Ruff, and `git diff --check` passed for this correction. No configuration change or Gateway restart was performed, and the Hermes Agent Fork remains uncommitted. The only commit is the local scripts-repository snapshot of `nc_report.py`; nothing was pushed.
- 2026-08-31 Fork Policy boundary validation: focused Policy/Host coverage `60 passed`; full approval/Gateway command `430 passed, 9 failed`; fresh-process isolation gave `7 passed` for approval-mode parity and `2 passed` for redaction, while the unchanged macOS `/tmp` alias remained `1 failed`; adjacent Terminal/code-execution/Tirith/i18n `167 passed, 7 subtests passed`; compression/real-user provenance `197 passed`.
- The reviewer, script-evidence, and retry-policy files were byte-identical to baseline `63f75f3b6d8c7d54d411e88990b400743d474fc5`; six fixed context samples matched the baseline builder. Ruff, `py_compile`, and `git diff --check` passed. No paid model replay, configuration change, Gateway restart, commit, or push was performed.
- The following 2026-08-16 results remain historical evidence for the original
  latest-turn context, Tirith, and language-aware implementation.
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
- 2026-09-16 workflow-prerequisite correction: the two new focused regressions
  first failed against the old reviewer contract and fixed denial text, then
  passed after the minimal correction. Eight sequential review-only calls used
  the explicit official Codex endpoint with `openai-codex / gpt-5.6-luna` and
  executed none of the reviewed commands. The original 3,900-character Cron
  context and resolver command returned `low / sufficient / approve` three
  times with empty `risk_evidence` and `prohibition`; paired cases preserved a
  negated Runbook prerequisite, a direct action prohibition, unapproved versus
  Clarify-authorized valuable deletion, and normal official-API authentication.
  Focused reviewer/Policy/script-evidence/Terminal/`execute_code`/Cron coverage
  completed with `290 passed`; Ruff, `py_compile`, and `git diff --check`
  passed. No configuration, context extraction, operator policy, script-evidence
  scope, Cron/KG logic, Computer Use behavior, Gateway restart, commit, or push
  was included.

Feature docs: `docs/chantxu64/current-turn-smart-approval/README.md`

Upstream status: fork-only.

### 29. Provider-native long-task continuity Request Fork

Status: active fork maintenance; current persistent compression-boundary delivery
is locally verified but not yet loaded or live-verified in the default Gateway.
The earlier request-local delivery was previously live-verified and is now
superseded.

Date: 2026-08-28; persistent delivery redesign 2026-08-30

Files:

- `agent/compression_facade.py` — `_compress_context` forwarder; carries `request_fork` and
  `request_fork_rematerializer` through to `compress_context`.
- `gateway/run_voice.py`
- `tests/fork_features/test_runtime_context_boundaries.py`
- `tests/fork_features/test_pre_llm_context_contract.py`
- `fork_features/request_fork/__init__.py`
- `fork_features/request_fork/compression_lifecycle.py`
- `fork_features/request_fork/prepared_request.py`
- `agent/conversation_loop.py`
- `agent/conversation_compression.py`
- `agent/conversation_compression_manual.py` — upstream manual-compression entry; Fork adds an
  optional `request_fork` passthrough to `_compress_context` so the gateway `/compress` path keeps its
  out-of-turn frozen request. `None` preserves upstream behavior exactly (added 2026-09-20 sync).
- `agent/turn_api_call.py`
- `agent/turn_api_error.py`
- `agent/turn_context_compaction.py`
- `agent/turn_overflow.py`
- `agent/turn_preflight.py`
- `agent/turn_request_assembly.py`
- `agent/turn_finalizer.py`
- `agent/context_compressor.py`
- `agent/agent_runtime_helpers.py`
- `agent/turn_context.py`
- `agent/turn_retry_state.py`
- `run_agent.py`
- `gateway/slash_commands.py`
- `gateway/run.py`
- `gateway/platforms/base.py`
- `hermes_cli/plugins.py`
- `fork_features/hindsight_retain/langfuse_hindsight_export.py`
- `tests/fork_features/test_current_request_fork.py`
- `tests/fork_features/test_compression_lifecycle.py`
- `tests/fork_features/test_prepared_request.py`
- `tests/fork_features/test_long_task_continuity_hooks.py`
- `tests/fork_features/test_long_task_continuity_recovery.py`
- `tests/fork_features/test_plugin_state_cas.py`
- `tests/agent/test_compression_adoption_preserves_live_tail.py`
- `tests/agent/test_compression_concurrent_fork.py`
- `tests/agent/test_reference_handoff_active_turn.py`
- `tests/agent/test_turn_context.py`
- `tests/agent/test_turn_retry_state.py`
- `tests/agent/test_message_sequence_repair.py`
- `tests/agent/test_thinking_only_sanitizer.py`
- `tests/agent/test_413_compression.py`
- `tests/agent/test_compression_budget_rearm.py`
- `tests/agent/test_compression_boundary_hook.py`
- `tests/agent/test_run_agent_codex_responses.py`
- `tests/agent/test_api_content_sidecar.py`
- `tests/agent/test_turn_finalizer_iteration_limit_exit.py`
- `tests/gateway/test_compress_command.py`
- `tests/gateway/test_auto_voice_reply_format.py`
- `tests/gateway/test_history_media_current_turn.py`
- `tests/fork/test_langfuse_hindsight_export.py`
- user plugin `~/.hermes/plugins/long-task-continuity/`
- `docs/LOCAL_MODIFICATIONS.md`

What changed:

- `fork_features/request_fork/compression_lifecycle.py` owns the request snapshot,
  hook payload validation and pending outer-commit notification as one lifecycle.
  The host reports adoption/start/outcome and consumes the pending finish once;
  locks, memory checkpoint ordering, summary dispatch and durable commit remain
  upstream-owned. Preserve both committed and aborted outer-transaction paths.
- `fork_features/request_fork/prepared_request.py` owns the adoption reconstruction
  shared by proactive and failed-wire compression. Its only loop-specific input
  is the explicit tool-call canonicalizer; it never imports the conversation loop.
  Callers in the loop, physical request capture and pre-capture error fallback must
  all retain this binding. Manual out-of-turn reconstruction is unchanged.
- After the official turn-loop split, the production call graph is explicit:
  `turn_preflight` and `turn_context_compaction` initiate ordinary compression;
  `turn_api_call` freezes the physically attempted request;
  `turn_api_error` and `turn_overflow` route provider overflow recovery;
  `conversation_compression` owns the prepare/commit boundary; and
  `turn_finalizer` owns terminal cleanup and forced-summary fallback. Tests drive
  these public turn entries so a preserved Fork module with a missing host call
  fails the maintenance gate.

- `context_compressor.is_non_user_runtime_context_message()` remains the stable
  envelope recognizer used by `conversation_compression._run_summary_phase()`.
  The live transcript is retained for rollback and Request Fork fidelity, while
  a private copy without persisted runtime context is the only input sent to the
  context engine, pre-compress memory checkpoint, and memory extraction commit.
- The obsolete `restart_after_prepared_compression` retry flag is intentionally
  absent after the split: `turn_preflight.run_preflight_compression()` now returns
  a direct `continue` verdict and refunds the unsent call before any provider
  attempt. Adjacent turn-state tests pin the new host contract instead of keeping
  the dead pre-split field.

- A scoped `FrozenCodexRequest` carries a deep-copied, provider-native Codex
  Responses request body and fidelity metadata. The private checkpoint Fork
  clones that body, appends exactly one synthetic `role=user` checkpoint item,
  and sends it with an explicitly owned client created from a frozen provider/
  client-construction spec. It does not retain or reread the parent agent, nor
  rerun chat conversion, request build, transport preflight,
  plugin middleware, tools, transcript persistence, Memory, Retain, or normal
  parent-request hooks.
- Automatic Codex Responses compression defers only the continuity-aware pending
  trigger until the parent request has completed normal request-only context,
  cache decoration, request build, sanitization, transport preflight, and
  one-shot request-header preparation. It captures `prepared_parent` before
  plugin middleware; if compression runs instead of sending that parent, the
  outer loop rebuilds the main request without consuming provider retry budget
  or losing the one-shot user-initiated flag.
- Provider 413/context-overflow recovery freezes the actual request after request
  and execution middleware, Relay mutation, final preflight, and streaming flag
  insertion at the physical provider-call boundary, and labels it `failed_wire`.
  Responses function-call items, function-call outputs, flat tool
  schemas, cache identity, and final headers therefore survive the Fork without
  a second conversion.
- A longer durable parent adopted under the compression lease invalidates the
  earlier prepared request. The Fork-owned pure rematerializer preserves the frozen
  request-only body and inserts only a proven concurrent durable append before
  the complete live tail as `rematerialized_after_adopt`; it does not rerun
  middleware, context selection, vision, or provider calls. Gateway `/compress`
  has no observed parent wire request and truthfully uses
  `reconstructed_out_of_turn`; multimodal history that cannot be reconstructed
  without vision side effects makes the Fork unavailable. Outer transcript
  persistence/session transition still decides committed versus aborted.
- Compression recovery is persisted once at the common local
  `compress_context` commit boundary as one hidden synthetic `role=user` row
  wrapped in the generic `<hermes-runtime-context user-authored="false" ...>`
  provenance envelope. Later model requests see the same stable row through
  ordinary session-history replay; `llm_request` middleware no longer appends a
  fresh compression recovery item on every tool-loop request.
- Before any local context engine or compression-boundary Memory provider sees
  the transcript, Core creates a private copy with the previous runtime-context
  row removed. The unfiltered live list remains available to the already-frozen
  Request Fork and rollback. No-progress comparison uses the same filtered
  baseline, so filtering alone cannot cause a false commit.
- After an engine makes progress, a bounded `on_compression_prepare_commit` hook
  joins the already-running Checkpoint outside the DB commit fence. Core validates
  and wraps at most one returned context row, then the existing in-place or
  rotation transaction persists it atomically with the compressed transcript.
  The next successful compression replaces the old row instead of stacking it.
- The prepare hook receives the host's `max_context_chars`; finish reports
  `persistent_context_source` only for the accepted row after a committed
  transaction (including the deferred outer-commit decision). Preparing a row,
  committing a summary, or accepting another source is not a delivery receipt.
- The continuity plugin leaves under-limit recovery unchanged. On overflow it
  omits only `user_messages[].interpretation` from a private automatic-injection
  projection, retaining all user text, other recovery fields, and the complete
  canonical state/manual tool output. It counts accompanying notices/receipts.
  If that projection still exceeds the limit, inject a small task directory with
  per-section Token estimates (user words and AI interpretations are separate),
  not an empty recovery or an implicitly complete task summary. Estimates reuse
  Hermes' local preflight estimator and are explicitly not provider usage.
  `long_task_state(section='index')` returns the same directory; listed sections
  support zero-based character pagination of serialized JSON via `offset`/`limit`,
  defaulting to 6000 characters with an explicit next offset and revision. Invalid
  partial-read arguments never silently return full state. Existing unqualified
  full reads and named-delta writes remain compatible; the directory instructs
  the model to select only needed parts rather than read everything back.
  Host-rejected delivery remains eligible for one-time request retry,
  acknowledged only by the matching successful API request; a later accepted
  compression clears older pending deliveries without clearing newer ones.
  The plugin's `tests/test_delivery_bounds.py` and `tests/test_index_fallback.py`
  cover these host/plugin contracts.
- The central real-user predicate and both user-message merge paths recognize the
  stable runtime-context envelope, so the synthetic row is not treated as user
  evidence or merged into genuine user text. Gateway voice and media current-turn
  boundaries use the same predicate. The standalone plugin rejects the envelope
  in checkpoint `append_user_messages` and keeps new-session root delivery as a
  separate one-time middleware path.
- The private Request Fork reuses the host-parsed `agent.api_max_retries` ceiling
  for retryable transport failures, creates a fresh independently owned client
  for every attempt, and uses the host backoff policy. Checkpoint JSON correction
  attempts remain separate from transport retries.
- Each checkpoint correction reuses the exact initial instructions, delta template,
  and frozen accepted-state baseline, followed by only the latest failed output
  and errors. Correction replaces the entire failed delta, including its valid
  unsaved changes; it is not a patch applied onto failed drafts. Parent input,
  tool schemas, cache identity, retry limits, and CAS conflict handling stay intact.
- Automatic checkpoint instructions and the manual state-tool description both
  require evidence-based updates to affected older facts when progress changes,
  preserving unresolved acceptance and valid constraints. This is model guidance,
  not a keyword-based contradiction validator or a new update schedule; empty
  lists alone do not imply missing tasks. Plugin `tests/test_retry_context.py`
  covers prompt delivery and the real Fork transport boundary with offline replies.
- Every successful Request Fork call emits one fail-open WARNING usage line keyed by
  `request_id`, with total prompt tokens, uncached input, cache read/write tokens,
  and cache-hit percentage. This keeps checkpoint cache behavior locally auditable
  without routing the private Fork through normal parent-request hooks.
- If all checkpoint attempts fail, diagnostic details remain in durable plugin
  state and are not injected into the model. Recovery uses the last accepted
  root, user messages, and next action when available; otherwise it gives only
  brief guidance to continue from the compression summary and latest user
  message, forbidding broad history search.

Why it matters:

- Long-running exploratory tasks must preserve their root goal, durable facts,
  constraints, and causal plan across real context compression without asking a
  weaker summarizer to invent the authoritative state.
- A checkpoint derived from a lossy chat-shaped reconstruction can silently drop
  tool schemas and function-call history; sharing the parent client or runtime
  can also close or mutate the live request. Provider-native values and explicit
  ownership make those boundaries testable.
- Runtime recovery must guide the model without being retained or later quoted
  as user-authored evidence. Profile-global delivery state must not let one
  session overwrite or acknowledge another session's recovery.

Merge protection:

- Never feed Responses-native `input` or flat `tools` back through the
  chat-to-Responses adapter. Tests must observe the final Fork transport body,
  not only a snapshot passed into compression.
- Preserve the distinction between `failed_wire`, `prepared_parent`,
  `rematerialized_after_adopt`, and `reconstructed_out_of_turn`; only the first
  represents a physically attempted provider request.
- Keep the Fork client explicit and independently closed. Do not reintroduce
  `copy.copy(agent)`, implicit primary-client lookup, parent transcript writes,
  normal middleware, or tool execution in the private Fork.
- Preserve compression/Fork parallelism while the context engine runs. After a
  successful summary, join the Checkpoint before `begin_commit`; never move the
  potentially long model wait inside the DB commit fence or back into ordinary
  `llm_request` middleware.
- Preserve generic `user-authored=false` filtering and the hidden synthetic user
  row. Do not replace it with continuity-specific Retain text matching, a
  developer role, dynamic instructions, or direct provider-payload mutation.
- Keep previous-row filtering, no-progress comparison, Memory handoff, and
  rollback snapshots on their documented separate copies. A merge that filters
  the live list or compares filtered output with an unfiltered baseline can
  silently delete context or publish a false compression boundary.
- Preserve named-delta CAS authority and the local storage retry. Runtime-context
  envelopes must never enter authoritative `root.user_messages`, and a stale
  checkpoint result must not overwrite a newer authority revision.

Validation:

- Current persistent-delivery source validation: `193 passed` across compression,
  rotation, message repair, hidden-message classification, Gateway voice/media,
  and lifecycle tests; the focused PluginManager whitelist/API suite adds
  `57 passed`.
- Standalone continuity plugin: `42 passed`; Ruff, `py_compile`, and
  `git diff --check` passed in both repositories.
- A real isolated PluginManager discovery probe loaded the copied standalone
  plugin, registered `on_compression_prepare_commit`, retained the separate
  `llm_request` middleware, and treated a missing checkpoint as a no-op without
  calling any model or touching the active Profile.
- Consecutive-compression tests verify exactly one persisted row with the newer
  revision; both in-place and rotation tests preserve `display_kind=hidden` and
  unchanged real user text. No-progress, commit-fence order, CAS retry, synthetic
  user rejection, and Gateway current-turn boundaries are covered.
- A broader unrelated Gateway media sweep reported `184 passed` and one existing
  test-double signature failure before reaching this feature's code; it was not
  changed as part of this maintenance unit.
- The earlier request-local design was historically live-verified after a
  Gateway restart. The current persistent-delivery source has not been loaded by
  the running Gateway and has no live Langfuse evidence yet.
- No Gateway restart or push was run after the mid-turn Request Fork fix; the
  final source still has no post-fix live Gateway evidence.

Upstream status: fork-only.

## Current fork delta checklist

Compared with the upstream parent of the latest completed fork sync, active fork
deltas are expected in these areas:

- Hindsight Unicode support / synchronous cache-miss Recall / P5 Recall and
  generic memory rewind lifecycle:
  - `.gitignore`
  - `agent/memory_manager.py`
  - `agent/memory_provider.py`
  - `fork_features/hindsight_recall_cache.py`
  - `plugins/memory/hindsight/__init__.py`
  - `hermes_state.py`
  - `tests/fork_features/test_hindsight_recall_cache.py`
  - `tests/fork_features/test_hindsight_p5_policy.py`
  - `tests/plugins/memory/test_hindsight_provider.py`
  - `tests/fork/test_hindsight_unicode_contract.py`
  - `tests/hermes_state/test_hermes_state.py`
  - `tests/agent/test_memory_session_switch.py`
  - `tests/fork/test_hindsight_provider_regressions.py`
  - `tests/fork/test_hindsight_recall_preprocessor.py`
  - `tests/fork/test_hindsight_rewind.py`
  - `cli.py`
  - `gateway/slash_commands.py`
  - `tests/gateway/test_undo_rewind_session.py`
  - `tui_gateway/server.py`
  - `tests/tui_gateway/test_undo_command.py`
- Custom STT API plugin:
  - `plugins/qwen_stt/plugin.yaml`
  - `plugins/qwen_stt/__init__.py`
  - `tools/transcription_tools.py`
  - `agent/transcription_registry.py`
  - `tests/fork/test_custom_stt.py`
  - `tests/fork/test_qwen_stt_plugin.py`
  - `tests/tools/test_transcription.py`
  - `tests/tools/test_transcription_dotenv_fallback.py`
  - `docs/LOCAL_MODIFICATIONS.md`
- Removed custom Qwen/custom HTTP TTS regression:
  - `tests/fork/test_custom_tts_removed.py`
  - `docs/LOCAL_MODIFICATIONS.md`
- Successful STT voice-origin enrichment:
  - `gateway/run_inbound.py`
  - `tests/gateway/test_stt_config.py`
  - `tests/gateway/test_telegram_audio_vs_voice.py`
  - `tests/gateway/test_telegram_voice_v0_regressions.py`
  - `docs/LOCAL_MODIFICATIONS.md`
- Per-invocation delegation provider/model/reasoning routing:
  - `tools/delegate_tool.py`
  - `tools/delegate_tool_config.py`
  - `tools/delegate_tool_dispatch.py`
  - `tools/delegate_tool_child_run.py`
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
  - `pyproject.toml`
  - `fork_features/__init__.py`
  - `fork_features/approval/__init__.py`
  - `fork_features/approval/policy.py`
  - `fork_features/approval/runtime.py`
  - `fork_features/approval/script_evidence.py`
  - `fork_features/approval/smart_review.py`
  - `fork_features/approval/retry_policy.py`
  - `agent/agent_runtime_helpers.py`
  - `agent/conversation_compression.py`
  - `agent/tool_executor.py`
  - `model_tools.py`
  - `tools/approval_context.py`
  - `tools/approval.py`
  - `tools/approval_smart.py`
  - `tools/tirith_security.py`
  - `tools/terminal_tool.py`
  - `tools/code_execution_tool.py`
  - `tests/fork_features/approval/test_policy_boundary.py`
  - `tests/fork_features/approval/test_runtime.py`
  - `tests/fork_features/approval/test_smart_approval_context.py`
  - `tests/tools/test_denial_retry_escalation.py`
  - `tests/tools/test_denial_circuit_breaker.py`
  - `tests/tools/test_smart_approval_injection.py`
  - `tests/tools/test_smart_approval_policy.py`
  - `tests/tools/test_execute_code_approval_cluster.py`
  - `tests/tools/test_tirith_security.py`
  - `tests/fork_features/approval/test_script_evidence.py`
  - `tests/fork_features/approval/test_smart_review.py`
  - `tests/fork_features/approval/test_retry_policy.py`
  - `tests/hermes_cli/test_gateway_restart_loop.py`
  - `tests/agent/test_run_agent.py`
  - `docs/chantxu64/current-turn-smart-approval/README.md`
  - `docs/LOCAL_MODIFICATIONS.md`
- Safe command rewrite:
  - `tools/safe_cmd_rewrite.py`
  - `tools/terminal_tool.py`
  - `tests/fork/test_safe_cmd_rewrite.py`
  - `pyproject.toml`
- Disable newly bundled skills by default when configured:
  - `fork_features/bundled_skills_policy.py`
  - `tools/skills_sync.py`
  - `hermes_cli/update_cmd.py`
  - `tests/fork/test_bundled_skills_policy.py`
  - `tests/fork/test_skills_auto_disable.py`
  - `tests/tools/test_skills_sync.py`
  - `docs/LOCAL_MODIFICATIONS.md`
- Request-only recall isolation and Codex prompt-cache routing:
  - `fork_features/request_context.py`
  - `fork_features/prompt_cache_routing.py`
  - `agent/chat_completion_helpers.py`
  - `agent/transports/codex.py`
  - `agent/conversation_loop.py`
  - `agent/turn_context.py`
  - `agent/turn_iteration_prep.py`
  - `agent/turn_request_assembly.py`
  - `agent/turn_finalizer.py`
  - `agent/session_persistence.py`
  - `agent/model_metadata.py`
  - `agent/codex_responses_adapter.py`
  - `run_agent.py`
  - `gateway/run.py`
  - `gateway/session_transcript.py`
  - `hermes_state_messages.py`
  - `gateway/slash_commands.py`
  - `hermes_cli/cli_commands_mixin.py`
  - `tests/fork_features/test_request_context_policy.py`
  - `tests/agent/test_api_content_sidecar.py`
  - `tests/agent/test_model_metadata.py`
  - `tests/agent/test_gateway_turn_sidecar.py`
  - `tests/agent/test_turn_finalizer_iteration_limit_exit.py`
  - `tests/agent/transports/test_codex_transport.py`
  - `tests/gateway/test_replay_entry_fields.py`
  - `tests/agent/test_steer.py`
  - `tests/agent/test_run_agent_codex_responses.py`
  - `tests/agent/test_codex_app_server_integration.py`
  - `tests/agent/test_codex_request_only_memory_context.py`
- Delivery-ledger session-reset boundary:
  - `fork_features/delivery_session_boundary.py`
  - `gateway/delivery_ledger.py`
  - `gateway/slash_commands_session.py`
  - `tests/fork_features/test_delivery_session_boundary.py`
  - `tests/gateway/test_delivery_ledger.py`
  - `tests/gateway/test_session_model_reset.py`
  - `tests/fork/test_multi_telegram_accounts.py`
  - `website/docs/user-guide/messaging/index.md`
- Transport disconnect classification:
  - `agent/error_classifier.py`
  - `agent/conversation_loop.py`
  - `tests/agent/test_error_classifier.py`
  - `tests/agent/test_thinking_timeout_guidance.py`
  - `tests/fork/test_transport_disconnect_classification.py`
  - `docs/LOCAL_MODIFICATIONS.md`
- Clarify attachment reply context:
  - `fork_features/clarify_attachment_reply.py`
  - `gateway/run_inbound.py`
  - `tools/clarify_gateway.py`
  - `tools/clarify_tool.py`
  - `tests/fork/test_clarify_attachment_reply.py`
  - `tests/gateway/test_clarify_active_session_bypass.py`
  - `tests/tools/test_clarify_gateway.py`
- Auditable autonomous built-in memory governance:
  - `tools/memory_tool.py`
  - `fork_features/memory_governance.py`
  - `fork_features/memory_audit.py`
  - `agent/background_review.py`
  - `agent/prompt_builder.py`
  - `agent/tool_executor.py`
  - `agent/agent_runtime_helpers.py`
  - `tests/agent/test_prompt_builder.py`
  - `tests/agent/test_memory_write_bridge.py`
  - `tests/fork/test_memory_changelog_governance.py`
  - `tests/fork_features/test_memory_governance_boundary.py`
  - `tests/tools/test_memory_tool.py`
  - `tests/tools/test_memory_tool_schema.py`
  - `tests/tools/test_write_approval.py`
  - `tests/agent/test_run_agent.py`
  - `docs/chantxu64/memory-change-governance/README.md`
  - `docs/LOCAL_MODIFICATIONS.md`
- Provider-native long-task continuity Request Fork:
  - `fork_features/request_fork/__init__.py`
  - `fork_features/request_fork/compression_lifecycle.py`
  - `fork_features/request_fork/prepared_request.py`
  - `agent/conversation_loop.py`
  - `agent/conversation_compression.py`
  - `agent/turn_api_call.py`
  - `agent/turn_api_error.py`
  - `agent/turn_context_compaction.py`
  - `agent/turn_overflow.py`
  - `agent/turn_preflight.py`
  - `agent/turn_request_assembly.py`
  - `agent/turn_finalizer.py`
  - `agent/context_compressor.py`
  - `agent/agent_runtime_helpers.py`
  - `agent/turn_context.py`
  - `agent/turn_retry_state.py`
  - `run_agent.py`
  - `gateway/slash_commands.py`
  - `hermes_cli/plugins.py`
  - `fork_features/hindsight_retain/langfuse_hindsight_export.py`
  - `tests/fork_features/test_current_request_fork.py`
  - `tests/fork_features/test_compression_lifecycle.py`
  - `tests/fork_features/test_prepared_request.py`
  - `tests/fork_features/test_long_task_continuity_hooks.py`
  - `tests/fork_features/test_long_task_continuity_recovery.py`
  - `tests/fork_features/test_plugin_state_cas.py`
  - `tests/agent/test_compression_adoption_preserves_live_tail.py`
  - `tests/agent/test_turn_retry_state.py`
  - `tests/agent/test_413_compression.py`
  - `tests/agent/test_compression_boundary_hook.py`
  - `tests/agent/test_run_agent_codex_responses.py`
  - `tests/gateway/test_compress_command.py`
  - `tests/fork/test_langfuse_hindsight_export.py`
  - `docs/LOCAL_MODIFICATIONS.md`
- Documentation:
  - `docs/LOCAL_MODIFICATIONS.md`
- Multi Telegram bots (account_id session slots):
  - `fork_features/multi_telegram_accounts/__init__.py`
  - `fork_features/multi_telegram_accounts/identity.py`
  - `fork_features/multi_telegram_accounts/runtime.py`
  - `fork_features/multi_telegram_accounts/session_routing.py`
  - `gateway/session.py`
  - `gateway/config.py`
  - `gateway/platforms/base.py`
  - `gateway/authz_mixin.py`
  - `gateway/slash_commands.py`
  - `gateway/run.py`
  - `plugins/platforms/telegram/adapter.py`
  - `tests/fork/test_multi_telegram_accounts.py`
  - `tests/fork_features/test_multi_telegram_accounts_boundary.py`
  - `tests/fork_features/test_multi_telegram_accounts_identity.py`
  - `tests/fork_features/test_multi_telegram_accounts_runtime.py`
  - `tests/fork_features/test_multi_telegram_accounts_session_routing.py`
  - `tests/gateway/test_background_process_notifications.py`
  - `tests/gateway/test_resume_command.py`
  - `tests/gateway/test_restart_notification.py`
  - `tests/gateway/test_runner_fatal_adapter.py`
  - `tests/gateway/test_platform_reconnect.py`
  - `tests/gateway/test_shutdown_cache_cleanup.py`
  - `tests/gateway/test_telegram_auth_check.py`
  - `tests/gateway/test_telegram_callback_auth_fail_closed.py`
  - `docs/chantxu64/multi-telegram-accounts/README.md`
  - `docs/LOCAL_MODIFICATIONS.md`
- First browser navigation opens a fresh tab:
  - `fork_features/browser_first_navigation.py`
  - `tools/browser_tool.py`
  - `tests/fork/test_browser_first_conversation_tab.py`
  - `docs/LOCAL_MODIFICATIONS.md`
- Telegram tool-progress literal-text rendering:
  - `fork_features/telegram_tool_progress.py`
  - `gateway/run.py`
  - `plugins/platforms/telegram/adapter.py`
  - `tests/fork/test_telegram_tool_progress_literal_text.py`
  - `tests/gateway/test_run_progress_topics.py`
  - `tests/gateway/test_telegram_rich_messages.py`
  - `docs/LOCAL_MODIFICATIONS.md`
- Self-contained Clarify decision cards:
  - `fork_features/clarify_decision_card.py`
  - `tools/clarify_tool.py`
  - `gateway/run_turn_runner.py`
  - `hermes_cli/cli_modal_mixin.py`
  - `tui_gateway/agent_callbacks.py`
  - `tui_gateway/server.py`
  - `tests/fork_features/test_clarify_decision_card.py`
  - `tests/tools/test_clarify_tool.py`
  - `docs/LOCAL_MODIFICATIONS.md`
- Launchd Gateway open-file ceiling (superseded by upstream configurable
  `runtime.nofile_soft_limit`; fork keeps emission regression only):
  - `tests/fork/test_launchd_open_file_limit.py`
  - `docs/LOCAL_MODIFICATIONS.md`


## Summary statistics

Documented entries: 29 major entries.

Active / current entries: 21.

Historical reverted / abandoned / superseded areas: 8.

Fork-only non-merge commits represented here: see
`git log --no-merges upstream/main..HEAD`.

<!--
Add future modifications above this summary, under either:
- Active modifications
- Historical / reverted modifications
-->
