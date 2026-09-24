# Hindsight Sync Cache-Miss Recall

> **Moved 2026-09-24.** Upstream `4cbf862abe` removed the bundled Hindsight plugin. The provider-side
> code and its regressions now live in the plugin Fork `https://github.com/CHANTXU64/hindsight`
> (branch `chantxu64/hermes-fork`, directory `hindsight-integrations/hermes`): `plugins/memory/hindsight/X`
> below is `X` there, `fork_features/hindsight_recall_cache.py` is `recall_cache.py`, and the Fork tests are
> under `tests_hermes/` (run with `HERMES_AGENT_ROOT=<hermes checkout>`). See entry 1 of
> `docs/LOCAL_MODIFICATIONS.md` for the pinned commit. Paths below are the pre-move Hermes paths.

## Purpose

Ensure Hindsight `auto_recall` can provide relevant memory context on the
current turn when no prior carried recall exists. This matters for the first
user message in a fresh session and the first turn after context
compression/session rotation.

When a previous assistant response is available, P5 may derive a more specific
query before this fallback is needed. The raw current-query fallback is the
bounded fail-open path for a fresh turn without that conversational input, a P5
route failure with no old results, or other no-result cache misses.

## Difference From Upstream

Upstream-style behavior only returns an already-cached prefetch result. When
`_prefetch_result` is empty, the current turn receives no injected memory
context.

This fork adds a bounded synchronous fallback inside the Hindsight provider:

- `prefetch()` uses the prior turn's carried real recall when available.
- On cache miss without a usable P5-derived target, `prefetch()` can
  synchronously recall using the current query.
- The synchronous fallback uses a short, separate timeout instead of the general Hindsight API timeout.
- The actual current-turn recall is carried into the next P5 decision.
- Hindsight's post-turn `queue_prefetch()` is a no-op; it never recalls the
  completed turn's raw user text.

The provider-neutral `MemoryManager` only supplies a bounded worker thread and
the `on_prefetch_timeout` callback. Fork-owned cache lifecycle rules live in
`fork_features/hindsight_recall_cache.py`. The Hindsight Provider owns API calls,
query clipping, formatting, cache-miss trigger/callback and snapshot carry. P5
model decisions and result orchestration live in the Fork-only
`plugins/memory/hindsight/recall_preprocessor.py` module.

## Files

- `fork_features/hindsight_recall_cache.py` — Session-scoped carried cache,
  generation/timeout invalidation and sync-miss gate.
- `plugins/memory/hindsight/__init__.py` — Hindsight API calls, sync fallback
  trigger/callback, config, formatting and snapshot carry.
- `plugins/memory/hindsight/recall_preprocessor.py` — P5 model decision,
  old-result filtering, optional Recall callback, failure restoration and
  outcome construction.
- `tests/fork_features/test_hindsight_recall_cache.py` — direct lifecycle
  contracts for consume/carry/timeout/Session/Undo/gating.
- `tests/fork/test_hindsight_provider_regressions.py` — public Provider fallback
  and carry behavior.
- `tests/fork/test_hindsight_recall_preprocessor.py` — P5 fixture/read migration
  and unchanged query/selection/fallback contracts.
- `tests/fork/test_hindsight_manual_retain_removed.py` — rewind invalidation
  without reviving the retired manual-retain ledger.
- `tests/plugins/memory/test_hindsight_provider.py` — provider behavior and race
  regression tests.
- `tests/agent/test_memory_session_switch.py` — bare-provider Session switch
  state tests.
- `docs/LOCAL_MODIFICATIONS.md` — fork modification index entry.
- `docs/chantxu64/hindsight-sync-cache-miss-recall/README.md` — this feature document.

## Configuration / Usage

Configuration keys in the Hindsight provider config:

- `recall_sync_on_cache_miss`: enables synchronous recall when `prefetch()` has no carried result. Default: `true`.
- `recall_sync_timeout_seconds`: timeout for the synchronous fallback recall. Default: `5`.

Existing `auto_recall`, `memory_mode`, `recall_prefetch_method`, tags, types, budget, and max-token settings still apply.

The fallback is skipped when:

- Hindsight is in `tools` mode.
- `auto_recall` is disabled.
- The provider is shutting down.
- The current query is empty.
- The sync fallback setting is disabled.

## Merge Guidance

- Preserve when: this fork still expects no-snapshot turns to receive bounded
  current-turn recall—using a P5-derived target when conversational input is
  available, otherwise the current query—without any previous-turn raw-query
  background recall.
- Drop when: upstream implements equivalent current-turn cache-miss recall with stale prefetch protection and a bounded sync timeout.
- Ask user when: upstream changes the memory prefetch lifecycle, Hindsight provider state model, or session-switch semantics in a way that is similar but not identical.

Fixed upstream `66666f6e2eca0ae883195a34c66131985ea7dd06` has an opt-in
`recall_sync` mode that recalls on every eligible turn and defaults off. It is
not equivalent to the Fork's default-on, cache-miss-only fallback.

The P5 orchestration commit `541b8f1083` statically depends on the Recall cache
lifecycle introduced by `b7cf9981c7`. If reverting these units, revert P5 first
and the cache lifecycle second; retaining P5 while removing the cache unit is
unsupported.

Conflict handling notes:

- Do not treat clearing only the formatted cache text as sufficient
  stale-context protection; the structured snapshot must follow the same
  generation, session-switch and rewind lifecycle.
- Do not reuse the general Hindsight API timeout as the sync fallback timeout.
- Do not reintroduce a post-turn raw-user-query recall.

## Verification

Current canonical focused verification (`84 passed` on the 2026-08-31
documentation follow-up):

```bash
scripts/run_tests.sh tests/fork_features/test_hindsight_recall_cache.py tests/fork/test_hindsight_provider_regressions.py tests/plugins/memory/test_hindsight_provider.py tests/agent/test_memory_session_switch.py tests/run_agent/test_memory_sync_interrupted.py -q
```

The earlier 2026-08-30 expanded direct-pytest gate additionally covered all
Hindsight Fork/plugin suites, MemoryManager, Request-only injection,
pre-compression and Gateway memory paths. It reported `384 passed` with `7`
third-party deprecation warnings; this is historical implementation evidence,
not the canonical command above.

Manual smoke check used during implementation:

```python
from plugins.memory.hindsight import HindsightMemoryProvider
p = HindsightMemoryProvider()
p.initialize(session_id='verification-session', platform='cli', hermes_home='/Users/robot/.hermes')
text = p.prefetch('Hindsight 自动召回 新会话 第一句')
assert text
p.shutdown()
```

## LOCAL_MODIFICATIONS Entry

Corresponding entry in `docs/LOCAL_MODIFICATIONS.md`: `### 8. Hindsight synchronous cache-miss recall`.
