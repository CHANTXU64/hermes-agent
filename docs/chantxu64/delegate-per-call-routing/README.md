# Per-invocation delegation routing

## Purpose

This fork lets one `delegate_task` invocation choose the child provider, model, and reasoning strength without changing global `delegation.*` configuration or the parent agent.

## Public contract

Single-task calls accept optional `provider`, `model`, and `reasoning_effort`. Batch calls accept the same top-level defaults and the same fields on each `tasks[]` item; item values win over top-level defaults.

Precedence:

1. Per-task field.
2. Top-level invocation field.
3. Existing `delegation.*` configuration.
4. Existing parent inheritance.

Behavior:

- No invocation fields preserves the pre-existing route and reasoning behavior.
- Reasoning-only keeps the inherited child route.
- Model-only uses the authenticated picker inventory and requires one unique provider match.
- Explicit provider+model uses runtime-provider resolution with the target model, so provider credentials and model-dependent API mode are recalculated.
- A routed model without explicit reasoning resolves `agent.reasoning_overrides` and then global reasoning configuration against the target model.
- Explicit reasoning is used only when the selected request builder preserves the exact value. If it would be clamped, mapped, or omitted, the selected provider/model is kept and the per-call value is treated as unspecified; the target model's normal Hermes reasoning configuration applies.
- Control actions (`list`, `steer`, `stop`) reject spawn-only route fields instead of ignoring them.

This feature does not introduce or redesign fallback behavior.

## Provider/model validation

Model-only inference uses `hermes_cli.inventory.build_models_payload(...)` with configured/authenticated providers only. Zero matches and multiple matches fail closed.

For explicit provider+model, a non-empty authenticated picker catalog is treated as authoritative for that known provider. A missing model is rejected. If the provider has no picker row or no catalog (common for some custom/direct endpoints), Hermes does not infer unavailability and lets the existing runtime-provider resolver decide.

Errors keep `error_code` metadata but put retry suggestions in compact Markdown tables inside the error text instead of putting the full model catalog in the persistent tool schema:

- `ambiguous_model` lists every current provider route matching the requested model.
- Missing and provider-mismatched models can show up to 10 frequent and 10 similar currently available routes, deduplicated by `(provider, model)` and bounded to 1,800 Markdown characters.
- Frequent routes come from the active profile's local `state.db` over the last 30 days, ranked by API calls, sessions, token use, and recency, then intersected with the current authenticated curated inventory. Stale historical routes are excluded.
- Similar routes come only from the current inventory, use a 0.55 minimum name-similarity score, and exclude routes already shown as frequent.
- An explicitly requested provider constrains both sections to that provider.
- The lookup is local and read-only; it does not invoke a model, launch a child, force a catalog refresh, or silently substitute a model.

The full authenticated catalog is deliberately not injected into `_build_dynamic_schema_overrides()`: the current installation exposes 57 provider/model pairs, so catalog injection would add roughly 1,451 characters to every request and change the tool definition whenever the catalog changes.

Fallback discovery guidance points only to locally verified entry points:

```bash
hermes model --refresh
hermes auth list
hermes auth list <provider>
```

## Reasoning validation

The generic accepted vocabulary is:

```text
none, minimal, low, medium, high, xhigh, max, ultra
```

The exactly usable subset is route-specific. Validation probes the existing production request builders/provider profiles and compares the requested value with the emitted wire value. Binary thinking toggles, silent omission, and level remapping do not count as the requested value taking effect. Instead of aborting or changing models, Hermes falls back to the target model's `agent.reasoning_overrides`, global reasoning configuration, or provider default. Invalid vocabulary still fails early.

Native `anthropic_messages` routes use the same production Anthropic request builder for this probe. Its exact per-invocation subset is `low`, `medium`, `high`, `xhigh`, and `max`; `minimal` maps to `low`, `ultra` maps to `max`, and `none` currently omits reasoning fields rather than emitting an explicit disable marker, so those three values retain the same-model automatic/default fallback behavior instead of being reported as exact.

## Background metadata and observability

Each child result exposes safe route metadata when available:

- `provider`
- `model`
- `reasoning_effort` (the selected child reasoning configuration)

Background batch records persist a `routes` array aligned with `goals`. The array is included in in-memory status, SQLite `task_json`, restart-recovered events, and completion events. A mixed-provider batch no longer advertises one misleading batch-level model; the legacy batch `model` field is populated only when every task uses the same model.

API keys, credential IDs, request overrides, and base URLs are not included in route metadata.

## Main implementation seams

- `fork_features/delegation_routing.py`
  - provider inference and explicit provider/model catalog validation
  - current-route suggestion lookup, ranking, and bounded Markdown rendering
  - target-route reasoning exactness and normal/default reasoning resolution
  - top-level/per-task route precedence, repeated-route caching, and full-batch
    prevalidation before child construction
  - safe route errors and public child route metadata
- `tools/delegate_tool.py`
  - model-facing schema and dispatch handler
  - host-owned generic delegation config and credential/runtime resolution
  - one call into the Fork route policy, followed by child construction and
    result aggregation
- `run_agent.py`
  - live model-tool dispatch forwarding
- `tools/async_delegation.py`
  - per-task route persistence and completion-event metadata
- `tests/fork_features/test_delegation_routing.py`
  - Fork ownership seam and pre-spawn route-failure contract
- `tests/tools/test_delegate.py`
- `tests/tools/test_delegate_control_actions.py`
- `tests/tools/test_async_delegation.py`

The Fork policy receives `_resolve_delegation_credentials` as a callback. It
does not import `tools.delegate_tool`, copy runtime-provider credential logic,
or own child lifecycle and async task storage.

## Maintenance exposure

At fixed local upstream SHA
`4f22543509d1b91dc45bcb369447126c5eb14fb7` and Fork baseline
`6eea4460484f499622e718684bc7e986da4f436f`, the maintenance-profile script
counted one path touch per commit since 2026-05-01 using `git log
--name-status --find-renames`: `tools/delegate_tool.py` had 119 upstream touches
and two Fork-only non-merge touches; `tools/async_delegation.py` had 31 and one.
The repository-local `docs/FORK_SYNC_HISTORY.jsonl` was absent. The external
decoupling ledger supplied to the maintenance profile existed with 13
implementation records but zero sync or follow-up records. Sync-outcome
coverage is therefore missing: no measured conflict hunks, resolution time,
rework, defects, or Token evidence was available. These figures show
path-change exposure only; they do not prove conflict or Token reduction.

## Merge protection

Preserve the behavior unless upstream provides an equivalent contract covering all of these together:

- model-only unique-provider inference;
- explicit cross-provider runtime resolution with target-model API mode;
- target-model reasoning override resolution;
- truthful explicit reasoning validation with same-model automatic/default fallback;
- per-task batch overrides;
- durable per-task route metadata;
- actionable fail-closed model-route errors;
- bounded Markdown route suggestions from current availability and local usage without persistent schema bloat.

If upstream adds a similar but behaviorally different interface, stop and compare contracts rather than silently combining both.

## Verification

Run at minimum:

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

python -m ruff check \
  fork_features/delegation_routing.py \
  tools/delegate_tool.py \
  tools/async_delegation.py \
  run_agent.py \
  tests/fork_features/test_delegation_routing.py \
  tests/tools/test_delegate.py \
  tests/tools/test_delegate_control_actions.py \
  tests/tools/test_async_delegation.py

python -m py_compile \
  fork_features/delegation_routing.py \
  tools/delegate_tool.py \
  tools/async_delegation.py \
  run_agent.py \
  tests/fork_features/test_delegation_routing.py \
  tests/tools/test_delegate.py \
  tests/tools/test_delegate_control_actions.py \
  tests/tools/test_async_delegation.py

git diff --check
```

2026-08-16 revised-contract validation results:

- core delegation/control/async suite: `125 passed in 16.72s`;
- adjacent DeepSeek/OpenCode Go/Codex request-builder suite: `158 passed in 1.82s`;
- restoration, API Server, Gateway binding, CLI delivery, TUI lifecycle, batch/output-schema, and FD-leak suite: `65 passed in 7.77s` with seven pre-existing third-party deprecation warnings;
- Ruff, `py_compile`, and `git diff --check`: passed;
- a read-only live-profile candidate render excluded stale historical `openai-codex` routes and returned only routes present in the current authenticated curated inventory.

2026-08-31 route-policy extraction validation results:

- Fork boundary plus core delegation/control/async suite: `129 passed`;
- adjacent DeepSeek/OpenCode Go/Codex/Anthropic request-builder suite:
  `302 passed`;
- Ruff, `py_compile`, and `git diff --check`: passed.

Functional validation did not launch a live child or call a target route. One
model subagent performed a separate read-only code review. No push or Gateway
restart was performed, and the extraction was not committed as part of this
validation.
