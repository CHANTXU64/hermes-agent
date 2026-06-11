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

### 2. MoA custom provider support

Date: 2026-04-20

Commits:

- `5c5ffe04` — provider-agnostic MoA adaptation
- `e60e548b` — tests for the provider-agnostic architecture
- `a0fc0fa0` — custom endpoint 401 authentication fix

Files:

- `tools/mixture_of_agents_tool.py`
- `tests/tools/test_mixture_of_agents_tool.py`
- `hermes_cli/config.py`
- `hermes_cli/runtime_provider.py`

What changed:

- MoA can resolve custom providers instead of assuming OpenRouter/OpenAI-style
  defaults.
- Runtime provider resolution supports configured `model.base_url` and custom
  provider credentials.
- Tests were updated for the provider-agnostic routing behavior.
- The 401 failure path for custom endpoints was fixed.

Why it matters:

- The user's runtime uses custom providers; MoA must not fall back to OpenRouter
  without the right credentials.
- A merge that drops this support can make MoA fail with 401 errors.

Merge protection:

- Preserve custom provider resolution and credential fallback behavior.
- Preserve MoA config defaults in `hermes_cli/config.py` unless upstream has a
  verified equivalent.
- Test or inspect `tools/mixture_of_agents_tool.py` and
  `hermes_cli/runtime_provider.py` after conflicts.

Upstream status: fork adaptation plus fork-only auth fixes.

### 3. MLX Whisper local STT provider

Date: 2026-04-20

Commit: `ae8c0acd`

Files:

- `tools/transcription_tools.py`
- `agent/transcription_registry.py`

What changed:

- Added `mlx_whisper` as a first-class local STT provider on macOS / Apple
  Silicon.
- Added model aliases such as `tiny`, `base`, `small`, `medium`, `large-v3`, and
  `turbo`.
- Auto-detection can choose MLX Whisper on Darwin when `faster-whisper` is not
  available.

Why it matters:

- The user uses MLX Whisper locally for Chinese speech transcription.
- Future merges must not remove the `mlx_whisper` provider path or aliases.

Merge protection:

- Preserve `_HAS_MLX_WHISPER`, `_normalize_mlx_model`, and
  `_transcribe_mlx_whisper` behavior unless upstream provides an equivalent.
- Preserve `mlx_whisper` as a native/built-in STT provider name in
  `agent/transcription_registry.py` if upstream adds plugin dispatch around STT.
- Confirm provider selection still supports explicit `stt.provider: mlx_whisper`.

Upstream status: fork adaptation of upstream STT work.

### 4. Safe command rewrite for terminal tool

Date: 2026-04-21

Commit: `5513a9b9`

Files:

- `tools/safe_cmd_rewrite.py`
- `tools/terminal_tool.py`
- `tests/tools/test_safe_cmd_rewrite.py`
- `pyproject.toml`

What changed:

- Terminal execution rewrites destructive local shell commands into safer
  alternatives:
  - `rm ...` becomes `trash ...`
  - `mv ...` becomes `gmv -b ...`
  - `cp ...` becomes `gcp -b ...`
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
- Run `tests/tools/test_safe_cmd_rewrite.py` after resolving conflicts touching
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

## Historical / reverted modifications


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
- `tests/plugins/memory/test_hindsight_provider.py`
- `tests/agent/test_memory_session_switch.py`
- `hermes_cli/commands.py`
- `tests/hermes_cli/test_commands.py`
- `cli.py`
- `gateway/run.py`
- `gateway/slash_commands.py`
- `tests/gateway/test_retain_command.py`
- `tests/gateway/test_undo_rewind_session.py`
- `tui_gateway/server.py`
- `tests/tui_gateway/test_undo_command.py`
- `docs/chantxu64/hindsight-manual-retain.md`

Summary:

- Adds a user-triggered `/retain` command that flushes Hindsight's existing buffered conversation turns through the normal automatic retain storage path.

What changed:

- Added `hindsight_retain_session` / `/retain` for user-triggered Hindsight session retain.
- Hindsight `sync_turn()` now persists the exact same turn JSON used by automatic retain into a separate SQLite file: `$HERMES_HOME/hindsight/retain_turns.sqlite3`.
- Persisted retain rows include `retain_document_id`, a stable logical document id inherited across compression-created child sessions.
- Gateway/CLI `/retain` no longer reconstructs from raw Hermes SessionDB transcript; it asks the provider to read persisted retain turns for the current session lineage.
- Gateway `/retain` resolves the current `session_id` via `SessionStore.get_or_create_session(source)`, matching the normal message path, so `/resume` and gateway restart still point retain at the selected session even before a cached agent exists.
- Manual retain first groups persisted turns by `retain_document_id`; this preserves a single logical Hindsight document even when compression/session bookkeeping records continuation sessions as siblings rather than a clean parent chain.
- Parent-chain lookup remains a fallback for older local rows without `retain_document_id`, and ignores empty stored parents when looking for a prior non-empty parent.
- Persisted turn lookup does not filter by the historical local `bank_id`; `/retain` submits matching lineage turns to the bank configured at retain time.
- Manual `/retain` submits a clean item with `content` and configured `context`, avoiding extra metadata/tags.
- Legacy provider buffer flush still tracks one pending append job, a session generation guard, and queued/flushed turn counts so automatic retain and direct provider tests do not regress.
- After upstream `09d66037f` added `_last_retained_turn_count` for append retain deltas, this fork intentionally keeps automatic retain routed through `flush_retained_turns()` instead, so automatic retain and manual/direct flush share the same queued/flushed/pending/generation state machine while still sending only new turns on append-capable APIs.
- When `auto_retain=false`, completed turns are written only to the local retain-turn SQLite file until `/retain` submits them.
- `/undo` now calls a dedicated memory rewind hook in CLI, Gateway, and TUI paths; Hindsight mirrors that rewind by soft-excluding the last N active rows in `hindsight_retain_turns` (`active=0`, `rewound_at`) so future manual `/retain` skips undone turns without hard-deleting audit rows.
- Hindsight rewind handling truncates the in-memory retain buffer and invalidates flush state without running the normal session-switch flush, so `/undo` does not itself push stale buffered turns to Hindsight.
- Only `/retain` is user-facing; no long command aliases are registered.
- Slack native slash generation keeps Telegram-visible canonical commands ahead of low-priority aliases so the extra fork-only `/retain` command does not push upstream `/debug` out of Slack registration under Slack's 50-command cap.
- `hindsight_retain_session` is not registered in model-visible tool schemas; CLI/Gateway call the provider directly via `memory_manager.get_provider("hindsight")`, so manual retain works even when `memory_mode="context"` hides Hindsight tools from the model.

Why it matters:

- The user needs an explicit, user-triggered way to preserve the normal Hindsight session document without changing the automatic retain storage model.

Merge protection:

- Do not reconstruct manual session retain from raw Hermes SessionDB transcript; the source of truth is provider-owned `$HERMES_HOME/hindsight/retain_turns.sqlite3` rows written by `sync_turn()`.
- Do not create a separate `manual-session:*` document; use `_resolve_retain_target_for_session()` with the resolved `retain_document_id` so Hindsight append semantics stay aligned with automatic retain.
- Do not expose `hindsight_retain_session` as a model-visible tool by default; this is a user slash command/provider method.
- Manual `/retain` must include all sessions that share the same `retain_document_id`, ordered by persisted row id; do not rely solely on `parent_session_id`, because compression continuations can appear as siblings in SessionDB.
- Gateway `/retain` must resolve the active session from `SessionStore.get_or_create_session(source)` before consulting cached agents, mirroring the normal message path after `/resume` or gateway restart.
- Manual `/retain` must not filter persisted turns by historical local `bank_id`; the current provider config determines the API target bank.
- Manual `/retain` payload items should stay clean (`content`, configured `context`, and `update_mode` only when needed), without extra metadata/tags.
- `/undo` must notify memory providers through the dedicated rewind hook, not only evict cached agents or call the normal session-switch hook; otherwise provider-owned persisted turns can drift from the active transcript.
- Hindsight `/undo` handling must mark local persisted retain rows inactive and must not run flush-on-switch for rewound buffered turns.
- Preserve tests proving manual retain persists Hindsight turn payloads to the separate SQLite file, groups compression siblings by root `retain_document_id`, falls back through prior non-empty parent rows for older data, resolves resumed/restarted gateway sessions without cached agents, ignores historical local bank casing/config changes, handles no-persisted-turn sessions, excludes rewound persisted turns, keeps legacy buffer flush behavior (pending rejection, failure rollback, generation guard, `memory_mode="context"`), and keeps Slack/Telegram slash registration parity despite the extra `/retain` command.
- Do not reintroduce upstream's standalone `_last_retained_turn_count` watermark unless the entire fork flush state machine is deliberately replaced and all manual `/retain`, append-delta, pending-failure rollback, and session-switch generation tests still pass. The expected fork behavior is `sync_turn()` persists the turn first, then automatic retain calls `flush_retained_turns()`.

Verification:

- `python -m pytest tests/plugins/memory/test_hindsight_provider.py tests/hermes_cli/test_commands.py tests/gateway/test_retain_command.py -q -o 'addopts='` → 260 passed.
- `python -m py_compile plugins/memory/hindsight/__init__.py cli.py gateway/run.py hermes_cli/commands.py tests/gateway/test_retain_command.py` → passed.
- `git diff --check` → passed.
- `python -m pytest tests/plugins/memory/test_hindsight_provider.py -q` → 127 passed after adding `retain_document_id` grouping.
- 2026-06-09 conflict resolution against upstream `09d66037f`: `python -m pytest tests/plugins/memory/test_hindsight_provider.py tests/agent/test_memory_session_switch.py tests/run_agent/test_memory_sync_interrupted.py -q -o 'addopts='` → 158 passed, 1 unrelated `audioop` deprecation warning.
- Rewind filtering update: `python -m pytest tests/plugins/memory/test_hindsight_provider.py tests/agent/test_memory_session_switch.py tests/agent/test_memory_async_sync.py tests/run_agent/test_memory_sync_interrupted.py tests/gateway/test_undo_rewind_session.py tests/tui_gateway/test_undo_command.py -q -o 'addopts='` → 186 passed.
- Rewind filtering update: `python -m pytest tests/hermes_cli/test_commands.py tests/gateway/test_retain_command.py -q -o 'addopts='` → 148 passed.

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

- The user's gateway can switch from local MLX Whisper to Alibaba Qwen STT through config only, while preserving MLX Whisper as a fallback/local option.
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

- `tools/tts_tool.py`
- `tests/tools/test_tts_custom_api.py`
- `docs/LOCAL_MODIFICATIONS.md`

Summary:

- Adds a configurable `tts.provider: custom_api` path so the fork can synthesize speech through Qwen/DashScope using the same `QWEN_API_KEY` convention as custom STT.

What changed:

- Added `custom_api` as a built-in TTS provider in `tools/tts_tool.py`.
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
- Preserve `api_key_env` lookup through `get_env_value()` so keys in `~/.hermes/.env` work.
- Preserve DashScope multimodal handling of `output.audio.url`; Qwen TTS does not use the OpenAI-compatible `/audio/speech` endpoint shape by default.
- Preserve the generic `audio_speech` mode unless upstream provides a verified equivalent configurable HTTP TTS provider.
- Preserve Telegram Opus conversion behavior for `custom_api` when voice-compatible delivery is needed.

Verification:

```bash
python -m pytest tests/tools/test_tts_custom_api.py -q -o 'addopts='
python -m pytest tests/tools/test_tts_custom_api.py tests/tools/test_tts_opus_routing.py tests/tools/test_tts_max_text_length.py -q -o 'addopts='
python - <<'PY'
import json, os
from tools.tts_tool import text_to_speech_tool
out='/tmp/hermes_qwen_tts_test.mp3'
try:
    os.remove(out)
except FileNotFoundError:
    pass
res=json.loads(text_to_speech_tool('你好，这是语音合成测试。', out))
print(res)
PY
ffprobe -v error -show_entries format=format_name,duration -of json /tmp/hermes_qwen_tts_test.ogg
```

Feature docs: none — TTS provider extension documented in this index.

Upstream status: fork-only.


## Current fork delta checklist

Compared with the upstream parent of the latest completed fork sync, active fork
deltas are expected in these areas:

- Hindsight Unicode support / manual session retain / synchronous cache-miss recall:
  - `.gitignore`
  - `agent/memory_manager.py`
  - `agent/memory_provider.py`
  - `plugins/memory/hindsight/__init__.py`
  - `tests/plugins/memory/test_hindsight_provider.py`
  - `tests/agent/test_memory_session_switch.py`
  - `hermes_cli/commands.py`
  - `tests/hermes_cli/test_commands.py`
  - `cli.py`
  - `gateway/run.py`
  - `gateway/slash_commands.py`
  - `tests/gateway/test_retain_command.py`
  - `tests/gateway/test_undo_rewind_session.py`
  - `tui_gateway/server.py`
  - `tests/tui_gateway/test_undo_command.py`
  - `docs/chantxu64/hindsight-manual-retain.md`
- MoA custom provider support:
  - `tools/mixture_of_agents_tool.py`
  - `tests/tools/test_mixture_of_agents_tool.py`
  - `hermes_cli/config.py`
  - `hermes_cli/runtime_provider.py`
- MLX Whisper STT / custom STT API / custom Qwen TTS API:
  - `tools/transcription_tools.py`
  - `tools/tts_tool.py`
  - `agent/transcription_registry.py`
  - `tests/tools/test_transcription.py`
  - `tests/tools/test_transcription_dotenv_fallback.py`
  - `tests/tools/test_tts_custom_api.py`
  - `hermes_cli/config.py`
- Safe command rewrite:
  - `tools/safe_cmd_rewrite.py`
  - `tools/terminal_tool.py`
  - `tests/tools/test_safe_cmd_rewrite.py`
  - `pyproject.toml`
- Disable newly bundled skills by default when configured:
  - `tools/skills_sync.py`
  - `hermes_cli/config.py`
  - `hermes_cli/main.py`
  - `tests/tools/test_skills_sync.py`
- Documentation:
  - `docs/LOCAL_MODIFICATIONS.md`


## Summary statistics

Documented entries: 11 major entries.

Active functional areas: 9.

Historical reverted areas: 1.

Fork-only non-merge commits represented here: see
`git log --no-merges upstream/main..HEAD`.

<!--
Add future modifications above this summary, under either:
- Active modifications
- Historical / reverted modifications
-->
