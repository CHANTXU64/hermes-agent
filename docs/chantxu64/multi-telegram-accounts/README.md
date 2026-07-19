# Multi Telegram bots in one profile

## Purpose

Run multiple Telegram bot tokens under **one** Hermes profile (shared config,
skills, plugins, long-term memory) while keeping **independent short-term
session slots** per bot — similar to opening multiple Hermes CLI terminals.

## Difference From Upstream

Upstream expects one `TELEGRAM_BOT_TOKEN` per profile/gateway context.
Related community work (e.g. multi-account routing PR) is not merged as an
equivalent “shared brain + multi CLI sessions” design in this fork’s baseline.

This fork:

1. Discovers `TELEGRAM_BOT_TOKEN_<ACCOUNT>` extras inside the active profile's
   secret scope (no cross-profile token import under multiplexing).
2. Starts sibling Telegram adapters in the same gateway process.
3. Isolates session keys with `:account:<id>` without rewriting real user/chat ids.
4. Keeps primary bot on `adapters[Platform.TELEGRAM]` for compatibility.

## Files

- `gateway/session.py` — `account_id`, session key helpers, `build_session_key`
- `gateway/config.py` — env discovery into `platforms.telegram.extra.accounts`
- `gateway/platforms/base.py` — source constructor accepts routing `account_id`
- `gateway/authz_mixin.py` — account-aware, fail-closed adapter resolution
- `gateway/slash_commands.py` — persist account route for restart lifecycle
- `gateway/run.py` — start/stop, fatal/reconnect lifecycle, key parsing and outbound routing
- `plugins/platforms/telegram/adapter.py` — stamp `account_id` before dispatch
- `tests/fork/test_multi_telegram_accounts.py` — session/config/runtime contracts
- `tests/gateway/test_background_process_notifications.py` — originating-bot background routing
- `tests/gateway/test_resume_command.py` — cross-bot transfer and active-session rejection

## Configuration / Usage

```bash
# Primary (legacy; session key unchanged; required when named bots are used)
TELEGRAM_BOT_TOKEN=111:AAA

# Extra bots (same profile)
TELEGRAM_BOT_TOKEN_WORK=222:BBB
TELEGRAM_BOT_TOKEN_ALERTS=333:CCC
```

Account id rules: `[A-Za-z0-9_-]`, starts alphanumeric, max 32, stored lowercase.
Named tokens are ignored when `TELEGRAM_BOT_TOKEN` is absent, so adding or
renaming an extra bot cannot silently change which token owns legacy bare keys.

Session keys:

```text
primary: agent:main:telegram:dm:<chat_id>
work:    agent:main:telegram:dm:<chat_id>:account:work
```

Semantics:

- Each bot has its own current conversation (`/new` is per-bot slot).
- Real Telegram `user_id` / `chat_id` stay real → `/resume` ownership still
  scopes to the same human on platform `telegram`.
- Resuming another bot’s idle session transfers that transcript to the current
  bot’s routing slot; the old bot key is unbound. A still-running target is
  rejected instead of creating two live bindings to one transcript.
- Runner-side streaming, typing, busy, voice/media, follow-up and background
  process/watch notifications resolve `source.account_id`; an unavailable named
  account fails closed instead of sending from the primary bot.
- Database peer recovery cannot reuse another bot account’s active session id.
- Pre-dispatch auth, batching, passive group observation and session-key
  computation already carry the named account route.
- Each named bot keeps its own fatal/reconnect state and reconnects with its
  own token/config. A named bot failure does not replace or stop primary.
- `/restart` shutdown and comeback notices retain the originating bot account.
- Skills / config / Hindsight bank / plugins remain shared (same profile).

Does **not** (v1):

- Dashboard multi-bot UI
- Per-bot allowlist / home / default model
- Account-qualified proactive `send_message` / cron destination syntax; bare
  Telegram proactive delivery continues to use primary
- Named-bot Telegram DM Topics; Topic mode remains a legacy primary-bot feature
- Account-specific `/update` lifecycle routing and other publish-oriented edge paths

## Merge Guidance

- Preserve when: multi-token env + account session suffix + primary adapter slot.
- Drop when: upstream multi-bot is proven equivalent with tests for:
  - primary key compatibility
  - same-user multi-bot isolation
  - `/resume` by real user across bots
- Ask user when: upstream uses a different key shape or splits brains by profile.

## Verification

```bash
python -m py_compile gateway/session.py gateway/config.py gateway/platforms/base.py gateway/authz_mixin.py gateway/slash_commands.py gateway/run.py plugins/platforms/telegram/adapter.py
scripts/run_tests.sh tests/fork/test_multi_telegram_accounts.py tests/gateway/test_background_process_notifications.py tests/gateway/test_resume_command.py tests/gateway/test_restart_notification.py tests/gateway/test_runner_fatal_adapter.py tests/gateway/test_platform_reconnect.py -q
scripts/run_tests.sh tests/gateway/test_telegram_auth_check.py tests/gateway/test_telegram_callback_auth_fail_closed.py -q
scripts/run_tests.sh tests/fork -q
```

The repository CI uses `scripts/run_tests.sh` with the default `tests/` discovery
root, which recursively includes every `test_*.py` file under `tests/fork/`.
Pre-commit baseline on 2026-07-13: focused runtime `172 passed`, cache/auth
`28 passed`, and the complete fork suite `424 passed`.

Manual (optional, needs two tokens):

1. Set primary + `TELEGRAM_BOT_TOKEN_WORK`.
2. Restart gateway when ready.
3. Message both bots; confirm independent context.
4. `/resume` a titled session from the other bot; confirm list includes it.

## LOCAL_MODIFICATIONS Entry

Corresponding entry in `docs/LOCAL_MODIFICATIONS.md`: `### 13. Multi Telegram bots in one profile (account_id session slots)`.
