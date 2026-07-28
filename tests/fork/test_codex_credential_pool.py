"""Fork-owned Codex credential-pool regressions."""

import json
import time

from tests.agent.test_credential_pool import _write_auth_store


def test_recover_uses_actual_runtime_key_when_round_robin_current_is_stale(tmp_path, monkeypatch):
    """A Codex 429 must mark the credential that actually made the failed request."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(
        "hermes_cli.auth._import_codex_cli_tokens",
        lambda: None,
    )
    hermes_home = tmp_path / "hermes"
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "cred-1",
                        "label": "first",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:device_code",
                        "access_token": "tok-first",
                        "base_url": "https://chatgpt.com/backend-api/codex",
                    },
                    {
                        "id": "cred-2",
                        "label": "second",
                        "auth_type": "oauth",
                        "priority": 1,
                        "source": "manual:device_code",
                        "access_token": "tok-second",
                        "base_url": "https://chatgpt.com/backend-api/codex",
                    },
                ]
            },
        },
    )
    (hermes_home / "config.yaml").write_text(
        "credential_pool_strategies:\n  openai-codex: round_robin\n"
    )

    from agent.credential_pool import load_pool
    from agent.agent_runtime_helpers import recover_with_credential_pool
    from agent.error_classifier import FailoverReason

    first_pool = load_pool("openai-codex")
    selected = first_pool.select()
    assert selected.id == "cred-1"

    stale_pool = load_pool("openai-codex")

    class _Agent:
        provider = "openai-codex"
        api_key = "tok-first"
        _credential_pool = stale_pool

        def _swap_credential(self, entry):
            self.api_key = entry.runtime_api_key

    recovered, _ = recover_with_credential_pool(
        _Agent(),
        status_code=429,
        has_retried_429=False,
        classified_reason=FailoverReason.rate_limit,
        error_context={"reason": "usage_limit_reached", "message": "The usage limit has been reached"},
    )

    assert recovered is True
    persisted = json.loads((hermes_home / "auth.json").read_text())["credential_pool"]["openai-codex"]
    by_id = {entry["id"]: entry for entry in persisted}
    assert by_id["cred-1"]["last_status"] == "exhausted"
    assert by_id["cred-1"]["last_error_code"] == 429
    assert by_id["cred-2"].get("last_status") in {None, "ok"}


def test_openai_codex_exhausted_entry_probe_can_clear_before_provider_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(
        "hermes_cli.auth._import_codex_cli_tokens",
        lambda: None,
    )
    now = time.time()
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "cred-1",
                        "label": "weekly-reset",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:device_code",
                        "access_token": "tok-1",
                        "base_url": "https://chatgpt.com/backend-api/codex",
                        "last_status": "exhausted",
                        "last_status_at": now - 7200,
                        "last_error_code": 429,
                        "last_error_reason": "usage_limit_reached",
                        "last_error_message": "The usage limit has been reached",
                        "last_error_reset_at": now + 7 * 24 * 60 * 60,
                        "codex_probe_at": now - 1900,
                    }
                ]
            },
        },
    )

    calls = []

    def _probe(entry):
        calls.append(entry.id)
        return True

    monkeypatch.setattr("agent.credential_pool._probe_openai_codex_entry_available", _probe, raising=False)

    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    entry = pool.select()

    assert calls == ["cred-1"]
    assert entry is not None
    assert entry.id == "cred-1"
    assert entry.last_status == "ok"

    persisted = json.loads((tmp_path / "hermes" / "auth.json").read_text())["credential_pool"]["openai-codex"][0]
    assert persisted["last_status"] == "ok"
    assert persisted["last_error_code"] is None
    assert persisted["last_error_reset_at"] is None


def test_upstream_codex_quota_probe_clear_is_persisted(tmp_path, monkeypatch):
    """The upstream early-reset probe must clear the observed cooldown on disk."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(
        "hermes_cli.auth._import_codex_cli_tokens",
        lambda: None,
    )
    now = time.time()
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "cred-1",
                        "label": "weekly-reset",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:device_code",
                        "access_token": "tok-1",
                        "base_url": "https://chatgpt.com/backend-api/codex",
                        "last_status": "exhausted",
                        "last_status_at": now - 7200,
                        "last_error_code": 429,
                        "last_error_reason": "usage_limit_reached",
                        "last_error_message": "The usage limit has been reached",
                        "last_error_reset_at": now + 7 * 24 * 60 * 60,
                        # Keep the fork probe inside its own interval so this
                        # regression exercises the upstream probe path only.
                        "codex_probe_at": now,
                    }
                ]
            },
        },
    )
    monkeypatch.setattr(
        "agent.credential_pool.CredentialPool._codex_quota_restored_upstream",
        lambda self, entry: True,
    )

    from agent.credential_pool import load_pool

    entry = load_pool("openai-codex").select()

    assert entry is not None
    assert entry.last_status == "ok"
    persisted = json.loads(
        (tmp_path / "hermes" / "auth.json").read_text()
    )["credential_pool"]["openai-codex"][0]
    assert persisted["last_status"] == "ok"
    assert persisted["last_error_code"] is None
    assert persisted["last_error_reset_at"] is None


def test_codex_probe_clear_does_not_erase_newer_concurrent_cooldown(
    tmp_path, monkeypatch
):
    """A probe may clear only the exact cooldown it observed before probing."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(
        "hermes_cli.auth._import_codex_cli_tokens",
        lambda: None,
    )
    auth_path = tmp_path / "hermes" / "auth.json"
    now = time.time()
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "cred-1",
                        "label": "weekly-reset",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:device_code",
                        "access_token": "tok-1",
                        "base_url": "https://chatgpt.com/backend-api/codex",
                        "last_status": "exhausted",
                        "last_status_at": now - 7200,
                        "last_error_code": 429,
                        "last_error_reason": "usage_limit_reached",
                        "last_error_message": "The old usage limit",
                        "last_error_reset_at": now + 7 * 24 * 60 * 60,
                        "codex_probe_at": now - 1900,
                    }
                ]
            },
        },
    )

    def _probe_then_concurrent_429(entry):
        payload = json.loads(auth_path.read_text())
        disk_entry = payload["credential_pool"]["openai-codex"][0]
        disk_entry.update(
            last_status="exhausted",
            last_status_at=now + 1,
            last_error_code=429,
            last_error_reason="newer_concurrent_rate_limit",
            last_error_message="A newer process observed another 429",
            last_error_reset_at=now + 14 * 24 * 60 * 60,
        )
        auth_path.write_text(json.dumps(payload))
        return True

    monkeypatch.setattr(
        "agent.credential_pool._probe_openai_codex_entry_available",
        _probe_then_concurrent_429,
    )

    from agent.credential_pool import load_pool

    load_pool("openai-codex").select()

    persisted = json.loads(auth_path.read_text())["credential_pool"][
        "openai-codex"
    ][0]
    assert persisted["last_status"] == "exhausted"
    assert persisted["last_error_reason"] == "newer_concurrent_rate_limit"
    assert persisted["last_error_reset_at"] == now + 14 * 24 * 60 * 60


def test_openai_codex_probe_does_not_clear_when_any_usage_window_is_full(monkeypatch):
    from agent.credential_pool import PooledCredential, _probe_openai_codex_entry_available

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "rate_limit": {
                    "primary_window": {"used_percent": 50},
                    "secondary_window": {"used_percent": 100},
                }
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            return _Response()

    monkeypatch.setattr("httpx.Client", _Client)
    entry = PooledCredential(
        provider="openai-codex",
        id="cred-1",
        label="weekly-limited",
        auth_type="oauth",
        priority=0,
        source="manual:device_code",
        access_token="tok-1",
        base_url="https://chatgpt.com/backend-api/codex",
    )

    assert _probe_openai_codex_entry_available(entry) is False
