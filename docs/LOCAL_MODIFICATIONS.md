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

### 5. Review prompts and `skill_manage` description configurable from config

Date: 2026-04-22

Commits:

- `907e6bd6` — initial `skills.skill_review_prompt` configurability
- `afc0f3d1` — documented entry 10
- `13446fdd` — memory/combined prompts and `skill_manage_description`
- `78607d74` — fallback fix for bare-object tests

Files:

- `run_agent.py`
- `tools/skill_manager_tool.py`
- `docs/LOCAL_MODIFICATIONS.md`

What changed:

- Background review prompts can be loaded from `config.yaml`:
  - `skills.skill_review_prompt`
  - `skills.memory_review_prompt`
  - `skills.combined_review_prompt`
- `skill_manage` tool description can be loaded from:
  - `skills.skill_manage_description`
- `AIAgent._spawn_background_review()` falls back to class constants when tests
  construct a bare `AIAgent` with `object.__new__(AIAgent)`.

Why it matters:

- The user has strict rules against low-quality or duplicate skill creation.
- Prompt and tool-description behavior must stay aligned with user policy.
- The fallback is required to avoid tests failing when `__init__` did not run.

Merge protection:

- Preserve instance prompt loading in `AIAgent.__init__`.
- Preserve `getattr(instance_attr, class_constant)` fallback in
  `_spawn_background_review()`.
- Preserve `_load_skill_manage_description()` or an equivalent config-backed
  mechanism.
- Do not reintroduce a hardcoded tool description that contradicts configured
  skill policy.

Upstream status: fork-only.

### 6. Local modifications document

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

## Historical / reverted modifications

### 7. Qwen TTS provider via DashScope

Date: 2026-04-21

Commits:

- `0e7ab099` — added Qwen TTS provider
- `bd7fd984` — moved Qwen from native Opus to ffmpeg conversion path
- `78607d74` — reverted Qwen TTS and restored KittenTTS behavior

Files:

- `tools/tts_tool.py`

What changed historically:

- A Qwen / DashScope TTS provider was added and later adjusted for Opus output.

Current status:

- Reverted. The fork currently should not contain the Qwen / DashScope TTS
  provider implementation.
- Official KittenTTS behavior was restored.

Why this matters:

- This is an audit record, not an active merge-preservation rule.
- Future conflict resolution must not resurrect Qwen TTS just because old entries
  mention it.

Merge protection:

- If `tools/tts_tool.py` conflicts, preserve current upstream-compatible
  KittenTTS behavior unless the user explicitly requests Qwen TTS again.
- Treat Qwen TTS code as removed historical code.

Upstream status: reverted fork-only experiment.

## Current fork delta checklist

As of HEAD `87adafd53`, compared with the upstream parent of the latest merge,
active fork deltas are expected in these areas:

- Hindsight Unicode support:
  - `.gitignore`
  - `plugins/memory/hindsight/__init__.py`
- MoA custom provider support:
  - `tools/mixture_of_agents_tool.py`
  - `tests/tools/test_mixture_of_agents_tool.py`
  - `hermes_cli/config.py`
  - `hermes_cli/runtime_provider.py`
- MLX Whisper STT:
  - `tools/transcription_tools.py`
- Safe command rewrite:
  - `tools/safe_cmd_rewrite.py`
  - `tools/terminal_tool.py`
  - `tests/tools/test_safe_cmd_rewrite.py`
  - `pyproject.toml`
- Review prompt / skill policy configurability:
  - `run_agent.py`
  - `tools/skill_manager_tool.py`
- Documentation:
  - `docs/LOCAL_MODIFICATIONS.md`

Note: `tools/tts_tool.py` may still show tiny formatting or typo diffs from the
historical Qwen revert path. Do not treat those as a reason to preserve Qwen
TTS. Inspect the current code before making merge decisions.

## Summary statistics

Documented entries: 7 major entries.

Active functional areas: 5.

Historical reverted areas: 1.

Fork-only non-merge commits represented here: 14.

<!--
Add future modifications above this summary, under either:
- Active modifications
- Historical / reverted modifications
-->
