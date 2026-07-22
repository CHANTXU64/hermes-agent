# Hindsight Sync Cache-Miss Recall

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

## Files

- `plugins/memory/hindsight/__init__.py` — Hindsight provider implementation, sync fallback, config parsing, generation guard.
- `tests/plugins/memory/test_hindsight_provider.py` — provider behavior and race regression tests.
- `tests/agent/test_memory_session_switch.py` — bare-provider session switch state tests updated for generation fields.
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

Conflict handling notes:

- Do not preserve `_prefetch_result` clearing alone as sufficient stale-context protection; the structured snapshot must follow the same generation, session-switch, and rewind lifecycle.
- Do not reuse the general Hindsight API timeout as the sync fallback timeout.
- Do not reintroduce a post-turn raw-user-query recall.

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
