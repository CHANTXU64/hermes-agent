# Hindsight Sync Cache-Miss Recall

## Purpose

Ensure Hindsight `auto_recall` can provide relevant memory context on the current turn when the background prefetch cache is empty. This matters for the first user message in a fresh session and the first turn after context compression/session rotation.

## Difference From Upstream

Upstream-style behavior only returns already-cached background prefetch results. When `_prefetch_result` is empty, the current turn receives no injected memory context and only starts a background prefetch for a later turn.

This fork adds a bounded synchronous fallback inside the Hindsight provider:

- `prefetch()` returns cached background recall when available.
- On cache miss, `prefetch()` can synchronously recall using the current query.
- The synchronous fallback uses a short, separate timeout instead of the general Hindsight API timeout.
- Background prefetch writes are guarded by a generation counter so late or superseded recalls cannot overwrite newer context.
- A newly queued prefetch generation clears any older cached result before starting, so an empty newer recall cannot leave stale context behind.

## Files

- `plugins/memory/hindsight/__init__.py` — Hindsight provider implementation, sync fallback, config parsing, generation guard.
- `tests/plugins/memory/test_hindsight_provider.py` — provider behavior and race regression tests.
- `tests/agent/test_memory_session_switch.py` — bare-provider session switch state tests updated for generation fields.
- `docs/LOCAL_MODIFICATIONS.md` — fork modification index entry.
- `docs/chantxu64/hindsight-sync-cache-miss-recall/README.md` — this feature document.

## Configuration / Usage

Configuration keys in the Hindsight provider config:

- `recall_sync_on_cache_miss`: enables synchronous recall when `prefetch()` has no cached background result. Default: `true`.
- `recall_sync_timeout_seconds`: timeout for the synchronous fallback recall. Default: `5`.

Existing `auto_recall`, `memory_mode`, `recall_prefetch_method`, tags, types, budget, and max-token settings still apply.

The fallback is skipped when:

- Hindsight is in `tools` mode.
- `auto_recall` is disabled.
- The provider is shutting down.
- The current query is empty.
- The sync fallback setting is disabled.

## Merge Guidance

- Preserve when: this fork still expects first-turn/new-session/compression-session Hindsight recall to inject current-query memory context without waiting for the previous-turn background prefetch.
- Drop when: upstream implements equivalent current-turn cache-miss recall with stale prefetch protection and a bounded sync timeout.
- Ask user when: upstream changes the memory prefetch lifecycle, Hindsight provider state model, or session-switch semantics in a way that is similar but not identical.

Conflict handling notes:

- Do not preserve `_prefetch_result` clearing alone as sufficient stale-context protection; generation checks are required because background threads can complete after session switches or newer queued prefetches.
- Do not reuse the general Hindsight API timeout as the sync fallback timeout.
- Do not let a newer empty background recall leave an older cached result available for consumption.

## Verification

```bash
python -m pytest tests/plugins/memory/test_hindsight_provider.py tests/agent/test_memory_session_switch.py tests/run_agent/test_memory_sync_interrupted.py -q -o 'addopts='
```

Expected result from the implementation change:

```text
149 passed
```

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
