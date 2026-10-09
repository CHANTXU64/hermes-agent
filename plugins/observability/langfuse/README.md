# Langfuse Observability Plugin

This plugin ships bundled with Hermes but is **opt-in** — it only loads when
you explicitly enable it.

## Enable

```bash
# Interactive: credentials + PM preparation of the langfuse extra + enable
hermes tools  # → Langfuse Observability
```

Restart Hermes after setup. If dependency preparation fails, retry through
`hermes tools`; do not inject the SDK into the selected environment with pip.
For manual source-checkout setup, see the
[plugin guide](../../../website/docs/user-guide/features/built-in-plugins.md#observabilitylangfuse).

## Required credentials

Set these in `~/.hermes/.env` (or via `hermes tools`):

```bash
HERMES_LANGFUSE_PUBLIC_KEY=pk-lf-...
HERMES_LANGFUSE_SECRET_KEY=sk-lf-...
HERMES_LANGFUSE_BASE_URL=https://cloud.langfuse.com   # or your self-hosted URL
```

Without the SDK or credentials the hooks no-op silently — the plugin fails
open.

## Verify

```bash
hermes plugins list                 # observability/langfuse should show "enabled"
hermes chat -q "hello"              # then check Langfuse for a "Hermes turn" trace
```

Generation observations include the Hermes system prompt when the provider
uses a separate `system` param (Anthropic Messages API). Open an **LLM call**
child span to inspect `role: system` (truncated via `HERMES_LANGFUSE_MAX_CHARS`).

## Fast mode and native performance metrics

Open an **LLM call** generation:

- **Model parameters** includes explicit `service_tier` (for example OpenAI
  `priority`) and/or `speed` (Anthropic `fast`). SDK `extra_body` overrides
  top-level values. These are per-request values, not the agent's static mode.
- **Metadata** separates `requested_service_tier` / `requested_speed` /
  `fast_requested` from `response_service_tier` / `response_speed` /
  `fast_confirmed`. Null means unknown. Missing response evidence never inherits
  the request. `auto` is not an explicit Fast request. An unknown service-level
  value is retained, without guessing its Fast meaning.
  Anthropic priority capacity alone does not confirm Fast speed. When the existing
  physical-request callback runs, success/error observations use its final values,
  including execution-middleware rewrites; otherwise the prepared request is kept.
- Fast evidence is structural: it survives all capture modes and truncation of
  large request/response content. Error generations keep the requested values;
  the next API attempt gets its own values.
- **Time to first token (TTFT)** uses the first nonempty generated text,
  reasoning or tool data from Responses, Chat Completions and Anthropic streams,
  not a lifecycle/role-only/keepalive event. Non-streaming calls or streams with
  no measured content leave `completionStartTime` absent. The separate Hermes
  `first_chunk_at` hook field remains TTFB and is not repurposed.
- **Tokens per second** is Langfuse's native output-token/request-latency metric
  (including initial wait), not a custom decode-only rate. Enable its column in
  the observations table if hidden. Token accounting is unchanged; cache usage
  remains in the existing usage details. No input-rate metric is added.

This extends the plugin's existing per-API-call generations, not its coverage
to new auxiliary-call sources. The generation start is the SDK's instrumentation
time; its end uses the API completion timestamp so post-response processing does
not inflate latency. Transport-internal reconnects remain within the existing
generation; TTFT takes content from the successful attempt, not a failed one.
Existing traces are not backfilled. Running processes must load the new code
before new traces contain these fields; changing files alone does not do that.

## Optional tuning

```bash
HERMES_LANGFUSE_ENV=production       # environment tag
HERMES_LANGFUSE_RELEASE=v1.0.0       # release tag
HERMES_LANGFUSE_SAMPLE_RATE=0.5      # sample 50% of traces
HERMES_LANGFUSE_MAX_CHARS=12000      # max chars per field (default: 12000)
HERMES_LANGFUSE_MAX_DEPTH=4          # max payload depth (default: 4)
HERMES_LANGFUSE_CAPTURE=sanitized    # content capture mode (see below)
HERMES_LANGFUSE_DEBUG=true           # verbose plugin logging
```

`HERMES_LANGFUSE_MAX_DEPTH` controls nested payload capture in both `sanitized`
and `full` modes, including tool arguments and JSON tool results. The root is
depth 0; each dictionary value or array element adds one level. Values beyond
the limit become `<max-depth>`, including scalars. For deeper MCP responses,
set it to a higher non-negative integer (for example, `10`) in the Hermes
process environment. Unset or blank values default to `4`; invalid or negative
values log a warning and fall back to `4`. `0` keeps only the root level.
Increasing the depth exports more content and may produce larger traces;
secret redaction, string-length limits, and the 50-item collection limit remain
unchanged. `metadata` mode still omits content.

## Capture modes

`HERMES_LANGFUSE_CAPTURE` controls how much *content* (prompts, responses,
tool arguments/results) is exported. Structural metadata — IDs, roles, tool
names, token usage, cost, timing — is always captured in every mode.

| mode | behavior |
|------|----------|
| `metadata` | No content. Each content field is replaced by a shape/size stub (`{"omitted": true, "type": "text", "chars": N}`). |
| `sanitized` | **(default)** Content is exported after secret-pattern redaction (API keys, tokens, JWTs, private keys, `password=`-style assignments) and truncation. Redaction runs *before* truncation. |
| `full` | Raw content, truncated only. Explicit opt-in — traces will contain whatever passed through the conversation, including injected memory and file contents. |

The active mode is recorded on every trace as `metadata.capture_mode`.

Note: `sanitized` is pattern-based defense in depth, not a DLP guarantee.
For personal sessions or shared Langfuse projects, prefer `metadata`.

## Error + shutdown coverage

- Failed model requests (`api_request_error` hook) close their generation
  with `level=ERROR`, status code, retry counters, and a capture-mode-scrubbed
  error message. Non-retryable failures also finish the turn trace.
- Session end/finalize closes any still-open traces for that session and
  flushes queued events, so interrupted or tool-only turns don't dangle.

## Disable

```bash
hermes plugins disable observability/langfuse
```
