"""Pure identity and environment rules for same-profile Telegram accounts.

Host modules own generic Gateway configuration and ``SessionSource`` storage.
This module owns only the Fork contract for named Telegram account IDs, their
environment discovery, and the suffix added to an otherwise-normal session key.
It imports no Gateway modules so both config and session code can depend on it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any, Optional


logger = logging.getLogger(__name__)
_ACCOUNT_SESSION_MARKER = ":account:"
_ACCOUNT_ENV_RE = re.compile(r"TELEGRAM_BOT_TOKEN_([A-Za-z0-9_-]+)")


def normalize_account_id(value: Optional[str]) -> Optional[str]:
    """Normalize a named account ID or reject values unsafe for session keys."""
    if value is None:
        return None
    raw = str(value).strip().lower()
    if not raw or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", raw):
        return None
    return raw


def append_account_session_key(session_key: str, account_id: Optional[str]) -> str:
    """Append or replace a named-account suffix; the primary key stays bare."""
    account = normalize_account_id(account_id)
    if not account:
        return session_key
    base, current = split_account_session_key(session_key)
    if current == account:
        return session_key
    return f"{base}{_ACCOUNT_SESSION_MARKER}{account}"


def split_account_session_key(session_key: str) -> tuple[str, Optional[str]]:
    """Return ``(base_key, named_account_or_none)`` for a trailing suffix."""
    if _ACCOUNT_SESSION_MARKER not in (session_key or ""):
        return session_key, None
    base, raw_account = session_key.rsplit(_ACCOUNT_SESSION_MARKER, 1)
    account = normalize_account_id(raw_account)
    if not account:
        return session_key, None
    return base, account


def restore_account_session_source(source: Any, session_key: Any) -> Optional[Any]:
    """Re-pin runtime-only Telegram account identity from a trusted route key.

    ``SessionSource.account_id`` is intentionally excluded from peer-controlled
    wire data. Durable Gateway routes already carry the account suffix, so
    synthetic/restored events recover from that key and fail closed on any
    conflict instead of silently falling back to the primary Bot.
    """
    _base, key_account = split_account_session_key(session_key)
    platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None))
    raw_source_account = getattr(source, "account_id", None)
    source_account = normalize_account_id(raw_source_account)
    if raw_source_account not in (None, "") and source_account is None:
        return None
    if str(platform or "").lower() != "telegram":
        return source if key_account is None and source_account is None else None
    if source_account is not None and source_account != key_account:
        logger.warning(
            "Refusing Telegram route with conflicting account identity: source=%s key=%s",
            source_account,
            key_account,
        )
        return None
    if source_account is None and key_account is not None:
        source.account_id = key_account
    return source


def discover_named_telegram_accounts(
    token_env: Mapping[str, Any],
    *,
    primary_token: Any,
) -> dict[str, dict[str, str]]:
    """Discover deterministic named Telegram token entries from one secret scope.

    The stable primary token is mandatory. Account names normalize to lowercase;
    when case variants provide different tokens the first mapping entry wins.
    Tokens equal to the primary or already assigned to an earlier sorted account
    are skipped so one credential is never polled by two adapters.
    """
    discovered: dict[str, str] = {}
    for raw_key, raw_value in token_env.items():
        env_key = str(raw_key)
        match = _ACCOUNT_ENV_RE.fullmatch(env_key)
        if match is None:
            continue
        token = str(raw_value or "").strip()
        if not token:
            continue
        account = normalize_account_id(match.group(1))
        if not account:
            logger.warning(
                "Ignoring %s: account id %r is invalid "
                "(use [A-Za-z0-9_-], start with alphanumeric, max 32)",
                env_key,
                match.group(1),
            )
            continue
        if account in discovered and discovered[account] != token:
            logger.warning(
                "Duplicate TELEGRAM_BOT_TOKEN_%s (case variants); keeping first",
                account.upper(),
            )
            continue
        discovered[account] = token

    if not discovered:
        return {}

    primary = str(primary_token or "").strip()
    if not primary:
        logger.warning(
            "Ignoring named Telegram bot tokens: TELEGRAM_BOT_TOKEN "
            "must be configured explicitly as the stable primary account"
        )
        return {}

    accounts: dict[str, dict[str, str]] = {}
    used_tokens: set[str] = set()
    for account, token in sorted(discovered.items()):
        if token == primary:
            logger.warning(
                "Skipping TELEGRAM_BOT_TOKEN_%s: token equals primary "
                "TELEGRAM_BOT_TOKEN",
                account.upper(),
            )
            continue
        if token in used_tokens:
            logger.warning(
                "Skipping TELEGRAM_BOT_TOKEN_%s: duplicate token already "
                "assigned to another account",
                account.upper(),
            )
            continue
        accounts[account] = {"token": token}
        used_tokens.add(token)
    return accounts


__all__ = [
    "append_account_session_key",
    "discover_named_telegram_accounts",
    "normalize_account_id",
    "restore_account_session_source",
    "split_account_session_key",
]
