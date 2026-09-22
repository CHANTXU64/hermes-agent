# Multi Telegram bots in one profile

## Purpose

Run multiple Telegram bot tokens under **one** Hermes profile. The bots share
configuration, models, Skills, plugins, and long-term memory while keeping
independent current conversations, exact return-bot routing, cross-bot resume,
and independent reconnect ownership.

Status: active; original feature 2026-07-13; Fork boundary refactored 2026-08-31.

Stable maintenance ID: `F-telegram-multi-account`.

## Difference From Upstream

At fixed review point
`upstream/main@a9c783f21995723c812dcb2f8ae58bc6a4323e2f`, upstream still expects one
`TELEGRAM_BOT_TOKEN` per profile/gateway context. Official multi-profile
gateways isolate profile configuration and memory, so they are not equivalent
to this feature's same-profile shared-brain behavior.

This fork retains the feature and isolates its policy. It does **not** rename the
account identity generically or introduce a reusable Adapter registry without a
second real consumer.

## Fork Boundary

The Fork-owned package has three explicit responsibilities:

- `fork_features/multi_telegram_accounts/identity.py`
  - account validation and normalization
  - `TELEGRAM_BOT_TOKEN_<ACCOUNT>` discovery in the active secret scope
  - `:account:<id>` session-key append/split rules
- `fork_features/multi_telegram_accounts/runtime.py`
  - live and failed named-adapter ownership
  - exact named-adapter lookup and fail-closed routing
  - named Bot startup, fatal handoff, independent reconnect, and shutdown
- `fork_features/multi_telegram_accounts/session_routing.py`
  - cross-bot `/resume` route discovery
  - running-target rejection
  - route transfer and detached-route cleanup

Upstream-owned host files keep only the seams the feature genuinely needs:

- `gateway/config.py` supplies the active secret scope and stores discovered
  named-account configs.
- `gateway/session.py` keeps account provenance runtime-only and re-exports the
  stable session-key helpers.
- `gateway/platforms/base.py` carries account provenance through source creation.
- `plugins/platforms/telegram/adapter.py` stamps account provenance before auth,
  batching, observation persistence, and session-key computation.
- `gateway/authz_mixin.py` asks the Fork runtime for the exact named adapter and
  restores post-restart account provenance from the trusted durable session key.
- `gateway/slash_commands.py` delegates cross-bot resume policy and preserves the
  account route for restart notices.
- `gateway/run.py` creates the runtime and keeps thin startup, fatal, reconnect,
  shutdown, and status handoffs.
- `gateway/run_notifications.py` reconstructs async-completion sources with the
  account suffix and resolves the exact named adapter rather than the primary.

## Configuration / Usage

```bash
# Primary (legacy session key; required when named bots are used)
TELEGRAM_BOT_TOKEN=111:AAA

# Extra bots in the same profile
TELEGRAM_BOT_TOKEN_WORK=222:BBB
TELEGRAM_BOT_TOKEN_ALERTS=333:CCC
```

Account IDs use `[A-Za-z0-9_-]`, start alphanumeric, are at most 32 characters,
and are stored lowercase. Named tokens are ignored when the primary token is
absent, so adding or renaming a named Bot cannot silently take ownership of
legacy bare session keys.

Session keys remain:

```text
primary: agent:main:telegram:dm:<chat_id>
work:    agent:main:telegram:dm:<chat_id>:account:work
```

## Behavior Contract

- The primary Bot remains the default Telegram adapter and keeps its old key.
- Each named Bot has its own current conversation; `/new` affects only that Bot's
  slot.
- Real Telegram `user_id` and `chat_id` remain unchanged, so session ownership
  and `/resume` continue to identify the real human.
- Resuming another Bot's idle session transfers the transcript to the current
  Bot route and unbinds the old route. A running target is rejected instead of
  becoming live on two routes.
- Normal replies, streaming, typing, busy responses, voice/media, follow-ups,
  background process/watch completion, authorization, and restart notices use
  the originating Bot.
- A configured named Bot that is temporarily unavailable fails closed; traffic
  never falls back to the primary Bot.
- Database peer recovery requires the same account suffix and cannot reuse
  another Bot's active session ID.
- Each named Bot keeps its own fatal/reconnect state and its own token/config.
  Failure of one named Bot does not replace, disconnect, or populate the primary
  Telegram retry slot.
- The Gateway stays alive when the primary Bot is down but a named Bot is still
  live or queued for reconnect.
- Skills, profile config, Hindsight bank, plugins, and model credentials remain
  shared because all Bots run in the same profile.

## Deliberate Non-goals

- Dashboard multi-bot UI
- Per-bot Home/default-model configuration
- Account-qualified proactive `send_message` or Cron destination syntax; bare
  proactive Telegram delivery continues to use primary
- Named-bot Telegram DM Topics; Topic mode remains a primary-bot feature
- Account-specific `/update` lifecycle routing and publish-oriented edge paths
- A generic `instance_id` migration or public Adapter instance registry

## Regression Protection

Behavior and boundary tests:

- `tests/fork/test_multi_telegram_accounts.py`
- `tests/fork_features/test_multi_telegram_accounts_identity.py`
- `tests/fork_features/test_multi_telegram_accounts_runtime.py`
- `tests/fork_features/test_multi_telegram_accounts_session_routing.py`
- `tests/fork_features/test_multi_telegram_accounts_boundary.py`
- `tests/gateway/test_background_process_notifications.py`
- `tests/gateway/test_resume_command.py`
- `tests/gateway/test_restart_notification.py`
- `tests/gateway/test_runner_fatal_adapter.py`
- `tests/gateway/test_platform_reconnect.py`
- `tests/gateway/test_shutdown_cache_cleanup.py`
- `tests/gateway/test_telegram_auth_check.py`
- `tests/gateway/test_telegram_callback_auth_fail_closed.py`

The input counterexamples cover blank, case-varied, invalid, duplicate-primary,
and duplicate-named token forms plus valid and invalid existing key suffixes.
The boundary test protects policy ownership without snapshotting implementation
text.

Canonical focused verification:

```bash
scripts/run_tests.sh tests/fork_features/test_multi_telegram_accounts_boundary.py tests/fork_features/test_multi_telegram_accounts_identity.py tests/fork_features/test_multi_telegram_accounts_runtime.py tests/fork_features/test_multi_telegram_accounts_session_routing.py tests/fork/test_multi_telegram_accounts.py tests/gateway/test_background_process_notifications.py tests/gateway/test_resume_command.py tests/gateway/test_restart_notification.py tests/gateway/test_runner_fatal_adapter.py tests/gateway/test_platform_reconnect.py tests/gateway/test_shutdown_cache_cleanup.py tests/gateway/test_telegram_auth_check.py tests/gateway/test_telegram_callback_auth_fail_closed.py -q -o 'addopts='
```

Refactor evidence before applying to the primary working tree:

- pre-refactor focused baseline: `125 passed`
- disposable sample focused suite: `148 passed`
- final host-integrated canonical suite, including shutdown: `151 passed`
- Python compilation, Ruff, diff-format check, and direct type check of the new
  Fork package passed
- broad type-check comparison: `399` baseline diagnostics, `398` candidate,
  zero new unique diagnostics
- same fixed upstream SHA: zero added conflict paths, conflict hunks, or
  Telegram-policy conflict hunks
- 2026-09-22 named-account completion regression: the combined canonical
  delegation and multi-account suite reported `427 passed`; Ruff, Python
  compilation, and `git diff --check` also passed

The existing whole-file conflicts in `gateway/run.py` and
`gateway/slash_commands.py` are unrelated Fork overlap and remain visible; this
maintenance unit does not claim to remove them.

## Merge Guidance

Preserve these semantic contracts during every upstream sync:

1. primary legacy session key and default adapter slot
2. runtime-only `SessionSource.account_id`, with post-restart restoration from
   the trusted `:account:<id>` session-key suffix
3. account provenance before auth, batching, observation, and session selection
4. exact named-adapter lookup with disconnected-account fail-closed behavior
5. original-Bot routing for every reply and notification path, including async
   delegation completion after persisted-source reconstruction
6. cross-bot `/resume` ownership and running-target rejection
7. per-named-Bot fatal/reconnect ownership and Gateway survival rules

If upstream changes a host seam, adapt only the thin handoff. Keep Telegram policy
inside `fork_features/multi_telegram_accounts/`; do not copy it back into the
Gateway host. If upstream introduces a different account or session model, stop
and compare actual user behavior before choosing a migration.

## Rollback / Deletion

Roll back the Fork package, five host-policy edits, and related tests as one
maintenance unit. Do not delete only the runtime handoff while leaving account
provenance or session suffixes behind.

Delete this feature only after upstream provides an equivalent same-profile,
multi-Bot, shared-brain design with independent current sessions and independent
reconnect, and the full behavior contract above passes against it.

## Manual Verification (Optional)

Requires two real Bot tokens and a user-controlled Gateway restart:

1. Set primary plus `TELEGRAM_BOT_TOKEN_WORK`.
2. Restart the Gateway when ready.
3. Message both Bots and confirm independent current context.
4. Title a session on one Bot, then `/resume` it from the other; confirm transfer
   and original-route cleanup.
5. Disconnect one named Bot and confirm the primary and other named Bots continue
   while only that account enters reconnect.

No Gateway restart or real-token test was performed by this 2026-08-31 source
refactor.

## LOCAL_MODIFICATIONS Entry

Corresponding authoritative entry:
`docs/LOCAL_MODIFICATIONS.md` →
`### 13. Multi Telegram bots in one profile (account_id session slots)`.
