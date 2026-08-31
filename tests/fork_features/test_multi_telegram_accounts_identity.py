from __future__ import annotations

from collections import OrderedDict

import pytest

from fork_features.multi_telegram_accounts.identity import (
    append_account_session_key,
    discover_named_telegram_accounts,
    normalize_account_id,
    split_account_session_key,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("  WORK_BOT-2  ", "work_bot-2"),
        ("0", "0"),
        ("-work", None),
        ("work/account", None),
        ("work bot", None),
        ("a" * 32, "a" * 32),
        ("a" * 33, None),
        ("机器人", None),
    ],
)
def test_account_id_input_transformations(raw, expected) -> None:
    assert normalize_account_id(raw) == expected


def test_session_suffix_round_trip_and_replacement() -> None:
    base = "agent:main:telegram:dm:5612546357"

    assert append_account_session_key(base, None) == base
    assert append_account_session_key(base, "WORK") == f"{base}:account:work"
    assert append_account_session_key(f"{base}:account:home", "WORK") == (
        f"{base}:account:work"
    )
    assert split_account_session_key(f"{base}:account:work") == (base, "work")
    assert split_account_session_key(f"{base}:account:bad!") == (
        f"{base}:account:bad!",
        None,
    )


def test_named_token_discovery_preserves_first_case_variant_and_deduplicates_tokens() -> None:
    env = OrderedDict(
        [
            ("TELEGRAM_BOT_TOKEN_WORK", " token-work "),
            ("TELEGRAM_BOT_TOKEN_work", "token-shadow"),
            ("TELEGRAM_BOT_TOKEN_ALPHA", "token-shared"),
            ("TELEGRAM_BOT_TOKEN_BETA", "token-shared"),
            ("TELEGRAM_BOT_TOKEN_PRIMARYCOPY", "token-primary"),
            ("TELEGRAM_BOT_TOKEN_EMPTY", "  "),
            ("TELEGRAM_BOT_TOKEN_-INVALID", "token-invalid"),
            ("telegram_bot_token_lower", "token-lower-key"),
            ("UNRELATED", "value"),
        ]
    )

    assert discover_named_telegram_accounts(
        env,
        primary_token=" token-primary ",
    ) == {
        "alpha": {"token": "token-shared"},
        "work": {"token": "token-work"},
    }


def test_named_token_discovery_requires_stable_primary() -> None:
    assert discover_named_telegram_accounts(
        {"TELEGRAM_BOT_TOKEN_WORK": "token-work"},
        primary_token="",
    ) == {}
