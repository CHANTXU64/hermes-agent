"""Codex auxiliary credentials honor the requested model, not sibling cooldowns."""

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import auxiliary_client as aux
from agent.credential_pool import CredentialPool, PooledCredential

ALLOWED = "allowed-model"
BLOCKED = "blocked-model"


@pytest.fixture
def scoped_pool(monkeypatch):
    entry = PooledCredential.from_dict("openai-codex", {
        "id": "seat", "label": "test", "auth_type": "api_key", "source": "manual",
        "access_token": "test-only-key", "base_url": "https://codex.example.test/v1",
        "model_cooldowns": {BLOCKED: time.time() + 3600},
    })
    pool = CredentialPool("openai-codex", [entry])
    monkeypatch.setattr(pool, "_persist", Mock())
    monkeypatch.setattr(pool, "_resync_stale_entry", lambda value: value)
    monkeypatch.setattr(pool, "_refresh_pending_entries", Mock(side_effect=AssertionError("no network refresh")))
    monkeypatch.setattr(aux, "load_pool", lambda provider: pool)
    monkeypatch.setattr(aux, "_read_codex_singleton_token", lambda: None)
    monkeypatch.setattr(aux, "_codex_base_url_override", lambda: "")
    monkeypatch.setattr(aux, "_create_openai_client", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr(aux, "_client_cache", {})
    return pool


@pytest.mark.parametrize("route", ["adapted", "raw", "cached"])
def test_sibling_model_restriction_does_not_disable_auxiliary_route(scoped_pool, route):
    if route == "cached":
        client, model = aux._get_cached_client("openai-codex", ALLOWED)
    else:
        client, model = aux.resolve_provider_client("openai-codex", ALLOWED, raw_codex=route == "raw")
    assert client is not None
    assert model == ALLOWED
    assert scoped_pool.select(model=BLOCKED) is None
    assert BLOCKED in scoped_pool.entries()[0].model_cooldowns


@pytest.mark.parametrize("route", ["adapted", "raw", "cached"])
def test_restricted_target_stays_unavailable(scoped_pool, route):
    if route == "cached":
        client, _ = aux._get_cached_client("openai-codex", BLOCKED)
    else:
        client, _ = aux.resolve_provider_client("openai-codex", BLOCKED, raw_codex=route == "raw")
    assert client is None


def test_warm_cache_cannot_reuse_an_account_after_target_model_is_restricted(scoped_pool):
    # Warm the cache after the pool has a current selection as well as before it.
    for _ in range(3):
        client, _ = aux._get_cached_client("openai-codex", ALLOWED)
        assert client is not None
    scoped_pool.entries()[0].model_cooldowns[ALLOWED] = time.time() + 3600
    client, _ = aux._get_cached_client("openai-codex", ALLOWED)
    assert client is None


def test_model_aware_peek_does_not_mutate_selection_or_bypass_target_restrictions(scoped_pool):
    initial = [(e.id, e.request_count) for e in scoped_pool.entries()]
    assert scoped_pool.peek(model=ALLOWED) is not None
    assert scoped_pool.peek(model=BLOCKED) is None
    assert scoped_pool.current() is None
    assert [(e.id, e.request_count) for e in scoped_pool.entries()] == initial
    scoped_pool._persist.assert_not_called()


@pytest.mark.parametrize("status", ["exhausted", "dead"])
def test_account_wide_restriction_still_blocks_allowed_model(scoped_pool, status):
    scoped_pool._adopt(
        scoped_pool.entries()[0], persist=False, last_status=status,
        last_status_at=time.time(), last_error_reset_at=time.time() + 3600,
    )
    client, _ = aux._get_cached_client("openai-codex", ALLOWED)
    assert client is None


def test_configured_codex_fallback_uses_requested_model(scoped_pool):
    client, model = aux._resolve_fallback_entry({"provider": "openai-codex", "model": ALLOWED})
    assert client is not None
    assert model == ALLOWED


def test_model_aware_peek_skips_restricted_current_entry_without_selecting(scoped_pool):
    current = scoped_pool.select(model=ALLOWED)
    assert current is not None
    sibling = PooledCredential.from_dict("openai-codex", {
        "id": "second", "auth_type": "api_key", "source": "manual",
        "access_token": "another-test-key", "base_url": "https://codex.example.test/v1",
    })
    scoped_pool._entries.append(sibling)
    counts = [(entry.id, entry.request_count) for entry in scoped_pool.entries()]
    assert scoped_pool.peek(model=BLOCKED).id == "second"
    assert scoped_pool.current().id == current.id
    assert [(entry.id, entry.request_count) for entry in scoped_pool.entries()] == counts
    scoped_pool._persist.assert_not_called()
