"""Fork-owned Telegram multi-account boundaries.

The package deliberately keeps pure identity/config helpers separate from the
runtime facade so ``gateway.config`` and ``gateway.session`` can import them
without pulling the Gateway runner back through an import cycle.
"""

from .identity import (
    append_account_session_key,
    discover_named_telegram_accounts,
    normalize_account_id,
    restore_account_session_source,
    split_account_session_key,
)

__all__ = [
    "append_account_session_key",
    "discover_named_telegram_accounts",
    "normalize_account_id",
    "restore_account_session_source",
    "split_account_session_key",
]
