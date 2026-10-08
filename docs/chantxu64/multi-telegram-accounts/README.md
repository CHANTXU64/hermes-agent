# Same-profile multi-Telegram bots — retired

Status: obsolete, removed by explicit user decision on 2026-10-08.
Stable maintenance ID: `F-telegram-multi-account`.

## Decision and supported replacement

Use one Telegram Bot per independent Hermes Profile. The old Fork shared one
Profile's configuration, credentials, Skills and memory among extra named bots.
The user no longer uses that arrangement and chose to remove its core lifecycle
and routing patches to reduce upstream-merge maintenance.

This deliberately removes `TELEGRAM_BOT_TOKEN_<ACCOUNT>` discovery, extra named
adapters, account-specific session slots and reconnect queues, and cross-Bot
`/resume` transfers. Normal Telegram, official Profiles and ordinary `/resume`
within the active Profile remain supported. No new configuration or compatibility
switch replaces the deleted feature.

## Historical data

Existing Profile configuration, historical messages and retained memories remain
intact. The primary Bot's session keys are unchanged. Old history stays in its
original database; it is not imported into another Profile automatically.
Ordinary explicit history access is not a transfer of the old Bot route.

The user explicitly rejected retaining account-aware compatibility checks. They
are removed from session recovery, notifications, delivery and Kanban paths.
Instead, an authorized one-time cleanup closes leftover old activity and removes
its active routing entries and legacy mirror entries. It neither deletes chat
content nor retries Retain. Old account-qualified routes are no longer supported
runtime inputs; historical data alone does not justify restoring their handlers.

Existing text and local-file reconnect handoff is retained for the supported
single-Bot path; only its named-account resolver is removed. These methods do not
depend on the retired package. Independent Fork features such as
literal-text progress, Quick Commands, and session-reset delivery retirement are
unchanged (their isolation tests use current Profile/topic routes).

The extra per-session delivery-ledger predicate is removed too. Startup claims
still use platform/Profile ownership, runtime claims still use process/Profile
ownership, and the normal deadline scheduler can arm a retry while its adapter is
offline. Actual delivery still requires that Profile's adapter; an unsent claim
is released without spending its retry budget. Flood waits and the independent
session-reset/superseded-obligation protection remain unchanged.

## Verification and maintenance

The authoritative retirement contract and touchpoints are in
`docs/LOCAL_MODIFICATIONS.md`, entry `F-telegram-multi-account`. Removal tests:
`tests/fork/test_telegram_account_retirement.py`. Standard Telegram, Profile,
recovery, lifecycle and notifier suites verify the remaining supported behavior.

Old implementation, behavior contracts and measured refactor results remain
recoverable in Git before removal (Fork baseline `252be9b385`). Historical and
new change/verification evidence is append-only in `changes.jsonl` beside this
file. Do not restore old preservation rules during an upstream merge.

The user separately authorized one-time runtime-state cleanup and a restart of
only the default Profile Gateway. Other Profile services and their configuration
are outside that operational scope. No commit or push is authorized. Source
verification and production activation remain separately evidenced in the ledger.

Rollback is a targeted reversal of the removal diff against the baseline. It must
be authorized; do not reset the whole working tree or touch unrelated Fork work.
