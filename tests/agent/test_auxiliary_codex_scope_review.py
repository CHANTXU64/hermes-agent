"""Independent R1/R2 regressions: real pools, fake credentials, no network."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from agent import auxiliary_client as aux
from agent.credential_pool import CredentialPool, PooledCredential

TARGET = "gpt-6-luna"
SIBLING = "gpt-6.1-astra"


@pytest.fixture
def make_pool(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

    def create(rows):
        entries = [
            PooledCredential.from_dict(
                "openai-codex",
                {
                    "id": name,
                    "label": name,
                    "auth_type": "api_key",
                    "source": "manual",
                    "access_token": "FAKE-" + name,
                    "base_url": f"https://{name.lower()}.example.test/v1",
                    "model_cooldowns": cooldowns,
                },
            )
            for name, cooldowns in rows
        ]
        pool = CredentialPool("openai-codex", entries)
        monkeypatch.setattr(pool, "_persist", Mock())
        monkeypatch.setattr(pool, "_resync_stale_entry", lambda value: value)
        monkeypatch.setattr(
            pool,
            "_refresh_pending_entries",
            Mock(side_effect=AssertionError("network refresh forbidden")),
        )
        monkeypatch.setattr(aux, "load_pool", lambda provider: pool)
        monkeypatch.setattr(aux, "_read_codex_singleton_token", lambda: None)
        monkeypatch.setattr(aux, "_codex_base_url_override", lambda: "")
        monkeypatch.setattr(
            aux, "_create_openai_client", lambda **kw: SimpleNamespace(**kw)
        )
        monkeypatch.setattr(aux, "_client_cache", {})
        return pool

    return create


def test_warm_cache_tracks_actual_selected_account(make_pool):
    pool = make_pool([("A", {SIBLING: time.time() + 3600}), ("B", {})])
    assert pool.select(model=SIBLING).id == "B"
    first, _ = aux._get_cached_client("openai-codex", TARGET)
    assert first is not None
    # Restrict A for the target, as another request or process can do.
    pool.entries()[0].model_cooldowns[TARGET] = time.time() + 3600
    second, _ = aux._get_cached_client("openai-codex", TARGET)
    assert second is not None and second.api_key == "FAKE-B"


@pytest.mark.parametrize("route", ["adapted", "raw", "cached", "fallback"])
def test_normalized_alias_cannot_bypass_target_restriction(make_pool, route):
    pool = make_pool([("A", {TARGET: time.time() + 3600})])
    model_alias = "openai/" + TARGET
    if route == "cached":
        client, _ = aux._get_cached_client("openai-codex", model_alias)
    elif route == "fallback":
        client, _ = aux._resolve_fallback_entry(
            {"provider": "openai-codex", "model": model_alias}
        )
    else:
        client, _ = aux.resolve_provider_client(
            "openai-codex", model_alias, raw_codex=(route == "raw")
        )
    assert pool.select(model=TARGET) is None
    assert client is None


@pytest.mark.parametrize(
    "alias", ["openai/" + TARGET, "openai:" + TARGET, "openai-codex/" + TARGET]
)
def test_alias_selection_cache_and_wire_use_same_model(make_pool, alias):
    pool = make_pool([("A", {TARGET: time.time() + 3600}), ("B", {})])
    assert aux._peek_pool_entry("openai-codex", model=alias).id == "B"
    assert aux._client_cache_key(
        "openai-codex", async_mode=False, model=alias
    ) == aux._client_cache_key(
        "openai-codex",
        async_mode=False,
        model=TARGET,
    )
    first, resolved = aux._get_cached_client("openai-codex", alias)
    assert first.api_key == "FAKE-B"
    assert resolved == TARGET
    assert first.chat.completions._model == TARGET
    second, resolved = aux._get_cached_client("openai-codex", TARGET)
    assert second is first
    assert resolved == TARGET
    assert TARGET in pool.entries()[0].model_cooldowns


@pytest.mark.parametrize("async_mode", [False, True])
def test_build_keeps_credential_binding_when_current_changes(
    make_pool, monkeypatch, async_mode
):
    pool = make_pool([("A", {SIBLING: time.time() + 3600}), ("B", {})])
    assert pool.select(model=SIBLING).id == "B"
    loop = SimpleNamespace(is_closed=lambda: False)
    monkeypatch.setattr(aux, "_current_event_loop", lambda: loop)

    def build(**kwargs):
        # Another selector can change current after our token/base have been read.
        assert pool.select(model=SIBLING).id == "B"
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(aux, "_create_openai_client", build)
    first, _ = aux._get_cached_client("openai-codex", TARGET, async_mode=async_mode)
    assert first.api_key == "FAKE-A"
    pool.entries()[0].model_cooldowns[TARGET] = time.time() + 3600
    second, _ = aux._get_cached_client("openai-codex", TARGET, async_mode=async_mode)
    assert second.api_key == "FAKE-B"
    assert str(second.base_url) == "https://b.example.test/v1"


def test_same_token_route_change_does_not_reuse_old_endpoint(make_pool):
    pool = make_pool([("A", {})])
    first, _ = aux._get_cached_client("openai-codex", TARGET)
    pool.entries()[0].base_url = "https://new.example.test/v1"
    second, _ = aux._get_cached_client("openai-codex", TARGET)
    assert first is not second
    assert str(second.base_url) == "https://new.example.test/v1"


def test_concurrent_round_robin_builds_keep_separate_accounts(make_pool, monkeypatch):
    pool = make_pool([("A", {SIBLING: time.time() + 3600}), ("B", {})])
    pool._strategy = "round_robin"
    assert pool.select(model=SIBLING).id == "B"
    a_building, release_a = Event(), Event()

    def build(**kwargs):
        if kwargs["api_key"] == "FAKE-A":
            a_building.set()
            assert release_a.wait(5), "timed out waiting for the other builder"
        return SimpleNamespace(**kwargs, close=Mock())

    monkeypatch.setattr(aux, "_create_openai_client", build)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(aux._get_cached_client, "openai-codex", TARGET)
        try:
            assert a_building.wait(5), (
                "first builder did not reach the interleave point"
            )
            second, _ = aux._get_cached_client("openai-codex", TARGET)
        finally:
            release_a.set()
        first, _ = pending.result(timeout=5)
    assert first.api_key == "FAKE-A"
    assert second.api_key == "FAKE-B"
    first._real_client.close.assert_not_called()
    second._real_client.close.assert_not_called()
    next(entry for entry in pool.entries() if entry.id == "A").model_cooldowns[
        TARGET
    ] = time.time() + 3600
    assert aux._get_cached_client("openai-codex", TARGET)[0] is second


def test_rotation_during_construction_is_not_misattributed_to_new_token(
    make_pool, monkeypatch
):
    pool = make_pool([("A", {})])

    def build(**kwargs):
        pool.entries()[0].access_token = "FAKE-ROTATED"
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(aux, "_create_openai_client", build)
    first, _ = aux._get_cached_client("openai-codex", TARGET)
    second, _ = aux._get_cached_client("openai-codex", TARGET)
    assert first.api_key == "FAKE-A"
    assert second.api_key == "FAKE-ROTATED"
    assert aux._get_cached_client("openai-codex", TARGET)[0] is second
    assert "FAKE-" not in repr(tuple(aux._client_cache))


def test_endpoint_override_and_sdk_trailing_slash_share_binding(make_pool, monkeypatch):
    make_pool([("A", {})])
    override = ["https://override.example.test/v1"]
    monkeypatch.setattr(aux, "_codex_base_url_override", lambda: override[0])

    def build(**kwargs):
        kwargs["base_url"] += "/"
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(aux, "_create_openai_client", build)
    first, _ = aux._get_cached_client("openai-codex", TARGET)
    assert aux._get_cached_client("openai-codex", TARGET)[0] is first
    override[0] = "https://new-override.example.test/v1"
    second, _ = aux._get_cached_client("openai-codex", TARGET)
    assert second is not first
    assert second.base_url == override[0] + "/"


def test_cooldown_expiry_and_profile_isolation(make_pool, monkeypatch):
    pool = make_pool([("A", {SIBLING: time.time() + 3600})])
    monkeypatch.setattr(aux, "hermes_home_key", lambda: "/isolated/profile-one")
    a, _ = aux._get_cached_client("openai-codex", TARGET)
    assert a is not None
    pool.entries()[0].model_cooldowns[TARGET] = time.time() + 3600
    assert aux._get_cached_client("openai-codex", TARGET)[0] is None
    pool.entries()[0].model_cooldowns[TARGET] = time.time() - 1
    assert aux._get_cached_client("openai-codex", TARGET)[0] is a
    monkeypatch.setattr(aux, "hermes_home_key", lambda: "/isolated/profile-two")
    assert aux._get_cached_client("openai-codex", TARGET)[0] is not a


def test_existing_singleton_fallback_bypasses_pool_restriction(make_pool, monkeypatch):
    pool = make_pool([("A", {TARGET: time.time() + 3600})])
    monkeypatch.setattr(aux, "_read_codex_singleton_token", lambda: "FAKE-A")
    client, resolved = aux.resolve_provider_client("openai-codex", TARGET)
    assert pool.select(model=TARGET) is None
    assert client is not None
    assert client.api_key == "FAKE-A"
    assert resolved == TARGET
