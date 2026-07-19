# Hindsight P5 Recall Preprocessor

## Purpose

Improve Hindsight `auto_recall` across conversational turns without asking Hindsight to interpret short session-local phrases such as “继续” or “修吧”. Before the current turn receives memory context, a dedicated configurable auxiliary model can conservatively filter the previous real recall and generate one short replacement query when the active long-term-memory target changed or became more specific.

This is a fork-only integration of the user-selected P5 prompt. P6 was evaluated separately and rejected because its hard coverage gate could suppress a needed recall when old results only partially covered the repair target.

## Runtime Flow

The preprocessor receives exactly three inputs:

1. Current user message.
2. Most recent completed, non-tool assistant response before the current turn,
   captured before any preflight context compression can replace old messages
   with a user-role summary.
3. Previous real Hindsight recall: the actual query and ordered result texts with temporary numeric refs.

It returns exactly:

```json
{"drop_old_refs":[1,2],"new_query":"short positive query or null"}
```

The provider then:

- when `new_query` is non-null, keeps every old result not listed in
  `drop_old_refs`, performs one bounded read-only recall, and appends the new
  results;
- when `new_query` is null, injects no Hindsight context for the current turn,
  clears the previous structured/text recall snapshot, and suppresses the
  legacy post-turn background prefetch for that same turn ID;
- formats non-empty output through the existing Hindsight memory-context
  formatter.

The assistant response is input to the auxiliary preprocessor only. It is never copied directly into the Hindsight query. The durable conversation history and system prompt are not mutated, preserving prompt-cache stability.

## Auxiliary Model Configuration and Prompt Contract

The task is registered in Hermes as
`auxiliary.hindsight_recall_preprocessor`. Configure it through
`hermes model` → **Configure auxiliary models** → **Hindsight recall
preprocessor**, or through the equivalent Models UI. The default configuration
preserves the original evaluated route:

```yaml
auxiliary:
  hindsight_recall_preprocessor:
    provider: openai-codex
    model: gpt-5.6-luna
    timeout: 30
```

The standard auxiliary-task fields are supported, including `provider`,
`model`, `timeout`, `base_url`, `api_key`, `api_mode`, and `extra_body`.
`provider` and `model` must both select an explicit route. `provider: auto`,
`provider: main`, or an empty model is treated as unavailable. A bare
`provider: custom` is also rejected unless this task supplies its own
`base_url`; this prevents the generic client resolver from trying a global
custom endpoint and then another API-key provider. Canonical-equivalent
reserved forms such as `custom:`, `custom:auto`, `custom:main`, and
`custom:custom` are evaluated by their suffix and cannot bypass those checks.
These checks keep the task from silently falling back to the main chat model or
another provider. Route, parse, or model failure uses the fail-open policy below.

The auxiliary `timeout` controls only the P5 LLM decision. The subsequent
read-only Hindsight recall has its own
`recall_sync_timeout_seconds` setting in Hindsight's config.

`MemoryManager` retains upstream's 8-second fail-open guard for ordinary
external memory providers. Hindsight declares a provider-specific outer budget
for its complete bounded pipeline:

```text
3-second background-prefetch handoff
+ configured auxiliary.hindsight_recall_preprocessor.timeout
+ up to 2 × configured recall_sync_timeout_seconds
+ 1-second outer-guard scheduling margin
```

With 30-second P5 and 10-second recall settings this outer budget is 54 seconds.
Two recall windows are required for the bounded branch where a P5-generated
query fails with no old results and Hindsight then retries the current query.
Each recall still has its own 10-second deadline; the larger outer budget only
prevents the generic guard from cutting either attempt off first. The 1-second
margin covers thread startup, stage transitions, and timeout scheduling jitter.
An explicit `MemoryManager(external_prefetch_timeout=...)` test/application
override still takes precedence, and providers without a valid declaration keep
the generic 8-second behavior.

- Default provider: `openai-codex`
- Default model: `gpt-5.6-luna`
- Generic provider/main-model fallback: disabled
- Default P5 call timeout: 30 seconds
- Caller requests a 256-token output limit, but the Codex adapter does not send
  `max_tokens` or `max_output_tokens` on the wire because this endpoint rejects
  those fields. It is therefore not a provider-enforced output cap.
- Requested temperature: `0`; the Codex adapter does not currently send this field on the wire, so the call is not described as deterministically temperature-zero.
- P5 prompt SHA-256: `7dfade51638396003e6332f7dbb8da45698d03d715d1d54b2f51658d2edbfa09`

The parser rejects malformed JSON, missing or extra keys, duplicate keys,
bool/non-integer/duplicate/out-of-range refs, empty or multi-line queries, and
Markdown fences. For `openai-codex`, the stream consumer separately captures
`response.completed.response.model`; an absent value or a value different from
the configured model is rejected. The Codex adapter's response `.model` field
remains the requested model for compatibility and is not used as independent
backend identity proof. For ordinary non-Codex Chat Completions responses,
`response.model` is provider-reported and is checked; an optional separate
`provider_reported_model` is checked as well. Any reported mismatch is rejected.

## Structured Prefetch State

Background and synchronous recall retain a private snapshot containing:

- the actual query sent after input-length clipping;
- ordered result texts;
- the historical formatted `_prefetch_result` string for compatibility.

The existing generation guard protects both the text cache and structured snapshot. Starting a newer generation clears both; a late older generation cannot overwrite either. Session switching also clears both. A queued prefetch carries its originating session ID and is checked both before starting its worker and immediately before recall, so a delayed old-session job cannot recall or populate the new session.

## Failure Policy

The feature is deliberately fail-open toward memory availability:

- P5 route unavailable, timeout, model mismatch, exception, or invalid JSON/schema: return the complete old recall cache.
- P5 requests a new query but that Hindsight recall raises: restore the complete old recall cache.
- No old cache and P5 fails: use the existing bounded current-query synchronous recall.
- A valid `new_query=null`: inject no recall context, clear both cached recall
  representations, and suppress same-turn post-turn prefetch, including a
  delayed old-session queue invocation that arrives after a session switch.
- A valid new recall that succeeds with zero results is a real empty result, not an exception.

Occasional redundant recall and conservative retention of a side-topic result are accepted limitations. Do not replace this policy with P6's “old results appear covered, therefore force null” gate.

The preprocessor is skipped when Hindsight is in tools-only mode, `auto_recall` is disabled, or the provider is shutting down.

## Files

- `agent/memory_provider.py` — optional previous-assistant and complete-prefetch-budget contracts.
- `agent/memory_manager.py` — signature-aware forwarding plus provider-specific
  outer budgets while preserving the generic 8-second fail-open default.
- `agent/turn_context.py` — extracts the latest completed non-tool assistant text.
- `agent/codex_runtime.py` — captures the provider-reported terminal response model separately from the requested model.
- `agent/auxiliary_client.py` — propagates that terminal model through the Codex chat-compatible adapter.
- `hermes_cli/plugins.py` and `plugins/memory/__init__.py` — bridge auxiliary
  tasks declared by the active memory provider into the standard Hermes model
  configuration surfaces.
- `plugins/memory/hindsight/recall_preprocessor.py` — frozen P5 prompt, strict parser, configured direct call.
- `plugins/memory/hindsight/__init__.py` — structured snapshot, filtering/merge, recall, failure and lifecycle behavior.
- `tests/fork/test_hindsight_recall_preprocessor.py` — prompt/schema,
  explicit-route/model-provenance, provider/failure, null lifecycle, and stale
  session-queue tests.
- `tests/hermes_cli/test_plugin_auxiliary_tasks.py` — active memory-plugin auxiliary task discovery and defaults.
- `tests/fork/test_hindsight_provider_regressions.py` — structured generation-race regressions.
- `tests/agent/test_turn_context.py` — duck-typed manager compatibility and request-context invariants.
- `tests/run_agent/test_run_agent_codex_responses.py` and `tests/agent/test_auxiliary_client.py` — terminal model provenance.

## Merge Guidance

Preserve when this fork still uses the P5 conversational recall behavior.

Drop only when upstream provides equivalent behavior and the user confirms the replacement preserves all of these properties:

- assistant-derived retrieval targets for accepted short continuations;
- conservative old-result filtering;
- explicit configurable/no-fallback auxiliary route, retaining the evaluated
  Luna defaults unless the user selects another route;
- rejection of `main`, `auto`, and bare generic `custom` routes, plus reported
  model substitutions; preserve suffix-normalization coverage for equivalent
  `custom:*` reserved forms;
- structured query/result snapshot guarded by generation and session lifecycle;
- fail-open restoration of old memory;
- no direct forwarding of full assistant text to Hindsight;
- provider-specific total prefetch budgeting that lets configured P5 and recall
  deadlines finish without changing the 8-second default for other providers;
- no P6-style hard coverage gate.

When `MemoryProvider.prefetch`, turn-context assembly, Hindsight prefetch, or auxiliary-client routing conflicts during an upstream merge, compare actual behavior and run the focused tests rather than preserving individual lines mechanically.

## Verification

Focused regression command:

```bash
python -m pytest tests/fork/test_hindsight_recall_preprocessor.py tests/plugins/memory/test_hindsight_provider.py tests/fork/test_hindsight_provider_regressions.py tests/agent/test_memory_session_switch.py tests/agent/test_memory_provider.py tests/agent/test_turn_context.py tests/run_agent/test_run_agent.py::TestMemoryProviderTurnStart tests/run_agent/test_run_agent_codex_responses.py tests/agent/test_auxiliary_client.py::TestCodexAdapterReasoningTranslation tests/hermes_cli/test_plugin_auxiliary_tasks.py -q -o 'addopts='
```

Implementation verification on 2026-07-17:

```text
433 passed
Ruff: All checks passed
compileall: passed
git diff --check: passed
```

Latest configurable-route verification:

```text
fork-specific preprocessor gate: 30 passed
affected Hindsight/Agent/plugin regressions: 378 passed
auxiliary-client/Codex regressions: 417 passed
core plugin-manager regressions: 134 passed
memory boundary/session/write/async regressions: 159 passed
Ruff: All checks passed
compileall: passed
git diff --check: passed
P5 prompt SHA-256: 7dfade51638396003e6332f7dbb8da45698d03d715d1d54b2f51658d2edbfa09
standard task discovery: hindsight_recall_preprocessor present
effective defaults: openai-codex / gpt-5.6-luna / 30 seconds
```

External-prefetch timeout compatibility verification on 2026-07-19:

```text
TDD initial red gate: 2 expected failures (generic manager ignored provider budget; Hindsight had no budget contract)
TDD initial green gate: 2 passed
independent-review red gate: 3 expected failures (second recall omitted; end-to-end outer truncation; overflow conversion skipped provider)
independent-review green gate: 3 passed
adjacent MemoryManager/P5/Hindsight provider suites: 255 passed
focused memory/P5/Codex integration suite: 482 passed
complete fork suite: 478 passed, 8 third-party deprecation warnings
Ruff: All checks passed
py_compile: passed
unstaged git diff --check: passed
```

Real read-only smoke used the production provider path against Bank `Hermes`:

```text
old recall results: 6
P5 drop refs: 1,2,3,4,5,6
new query: 用户对 Hindsight recall Prompt 设计与评估的长期偏好、评价标准、约束和验收要求
new recall results: 6
final formatted merge: exact match
provider-reported terminal model: gpt-5.6-luna
retain writer started: false
retain queue empty: true
```

No retain/write call, config change, service restart, commit, or push was part of this verification.

## LOCAL_MODIFICATIONS Entry

Corresponding entry: `docs/LOCAL_MODIFICATIONS.md` → `### 9. Hindsight P5 recall preprocessor`.
