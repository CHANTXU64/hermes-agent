"""Auto credential scope follows the final wire model, including configured overrides."""
import socket
import time
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from agent import auxiliary_client as aux
from agent.credential_pool import CredentialPool, PooledCredential

TARGET = 'gpt-6-luna'
SIBLING = 'gpt-6.1-astra'

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def make_pool(monkeypatch):
    def make(rows):
        entries = [PooledCredential.from_dict('openai-codex', {
            'id': name, 'label': name, 'auth_type': 'api_key', 'source': 'manual',
            'access_token': 'FAKE-' + name, 'base_url': f'https://{name.lower()}.example.test/v1',
            'model_cooldowns': cooldowns,
        }) for name, cooldowns in rows]
        pool = CredentialPool('openai-codex', entries)
        monkeypatch.setattr(pool, '_persist', Mock())
        monkeypatch.setattr(pool, '_resync_stale_entry', lambda entry: entry)
        monkeypatch.setattr(pool, '_refresh_pending_entries', Mock(side_effect=AssertionError('network forbidden')))
        monkeypatch.setattr(aux, 'load_pool', lambda provider: pool if provider == 'openai-codex' else None)
        monkeypatch.setattr(aux, '_read_codex_singleton_token', lambda: None)
        monkeypatch.setattr(aux, '_codex_base_url_override', lambda: '')
        monkeypatch.setattr(aux, '_create_openai_client', lambda **kwargs: SimpleNamespace(**kwargs, close=Mock()))
        monkeypatch.setattr(aux, '_client_cache', {})
        monkeypatch.setattr(aux, '_task_prefers_fast_model', lambda task: False)
        return pool
    return make

@pytest.mark.parametrize('async_mode', [False, True])
@pytest.mark.parametrize('route', ['resolve', 'cached', 'task-route'])
def test_auto_model_override_selects_for_actual_requested_model(make_pool, monkeypatch, route, async_mode):
    pool = make_pool([('A', {TARGET: time.time()+3600}), ('B', {})])
    monkeypatch.setattr(aux, '_current_event_loop', lambda: SimpleNamespace(is_closed=lambda: False))
    runtime = {'provider': 'openai-codex', 'model': SIBLING}
    if route == 'task-route':
        monkeypatch.setattr(aux, '_get_auxiliary_task_config', lambda task: {'provider': 'auto', 'model': TARGET})
        rp, rm, rb, rk, ra = aux._resolve_task_provider_model('summarize')
        result = aux._resolve_call_client(
            'summarize', provider=None, model=None, base_url=None, api_key=None,
            resolved_provider=rp, resolved_model=rm, resolved_base_url=rb,
            resolved_api_key=rk, resolved_api_mode=ra, main_runtime=runtime, async_mode=async_mode,
        )
        client, model = result.client, result.final_model
    else:
        resolver = aux._get_cached_client if route == 'cached' else aux.resolve_provider_client
        client, model = resolver('auto', TARGET, async_mode=async_mode, main_runtime=runtime)
    assert client is not None
    adapter = client.chat.completions
    if async_mode:
        adapter = adapter._sync
    payload, _, _ = adapter._build_responses_kwargs({'model': model, 'messages': [{'role': 'user', 'content': 'fake request; no send'}]})
    print('route', route, 'async', async_mode, 'credential', client.api_key, 'resolved', model, 'wire', payload['model'])
    assert pool.select(model=TARGET).id == 'B'
    assert model == TARGET
    assert payload['model'] == TARGET
    assert client.api_key == 'FAKE-B', 'auto built for main model but will send restricted target using A'

@pytest.mark.parametrize('async_mode', [False, True])
def test_auto_inherited_alias_keeps_real_credential_binding(make_pool, monkeypatch, async_mode):
    pool = make_pool([('A', {SIBLING: time.time()+3600}), ('B', {})])
    assert pool.select(model=SIBLING).id == 'B'
    loop = SimpleNamespace(is_closed=lambda: False)
    monkeypatch.setattr(aux, '_current_event_loop', lambda: loop)
    runtime = {'provider': 'openai-codex', 'model': 'openai/' + TARGET}
    first, first_model = aux._get_cached_client('auto', async_mode=async_mode, main_runtime=runtime)
    assert first is not None
    assert first_model == TARGET
    assert first.api_key == 'FAKE-A'
    pool.entries()[0].model_cooldowns[TARGET] = time.time()+3600
    second, second_model = aux._get_cached_client('auto', async_mode=async_mode, main_runtime=runtime)
    assert second is not None
    assert second.api_key == 'FAKE-B'
    assert second_model == TARGET
    assert aux._get_cached_client('auto', async_mode=async_mode, main_runtime=runtime)[0] is second


def test_singleton_fallback_never_stored_under_ineligible_pool_hint(make_pool, monkeypatch):
    make_pool([('A', {TARGET: time.time()+3600})])
    monkeypatch.setattr(aux, '_read_codex_singleton_token', lambda: 'FAKE-A')
    expected_lookup = aux._client_cache_key('openai-codex', async_mode=False, model=TARGET)
    client, _ = aux._get_cached_client('openai-codex', TARGET)
    assert client is not None
    assert client.api_key == 'FAKE-A'  # explicitly preserved policy
    assert expected_lookup not in aux._client_cache
    assert all(key[9] == aux._codex_credential_cache_hint(value[0].api_key, value[0].base_url) for key, value in aux._client_cache.items())

@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("provider", ["openai-codex", "codex"])
@pytest.mark.parametrize("override,expected", [
    (TARGET, TARGET),
    ("openai:" + TARGET, TARGET),
    # Auto's longstanding slash guard is intentionally NOT explicit Codex routing.
    ("openai/" + TARGET, SIBLING),
    ("openai-codex/" + TARGET, SIBLING),
    ("anthropic/claude-other", SIBLING),
    (None, SIBLING),
])
def test_auto_cache_uses_effective_model_everywhere(
    make_pool, monkeypatch, async_mode, provider, override, expected,
):
    pool = make_pool([("A", {expected: time.time() + 3600}), ("B", {})])
    select = Mock(wraps=pool.select)
    peek = Mock(wraps=pool.peek)
    monkeypatch.setattr(pool, "select", select)
    monkeypatch.setattr(pool, "peek", peek)
    loop = SimpleNamespace(is_closed=lambda: False)
    monkeypatch.setattr(aux, "_current_event_loop", lambda: loop)
    runtime = {"provider": provider, "model": "openai/" + SIBLING}
    first, model = aux._get_cached_client("auto", override, async_mode=async_mode, main_runtime=runtime)
    second, cached_model = aux._get_cached_client("auto", override, async_mode=async_mode, main_runtime=runtime)
    assert first is not None
    assert first is second
    assert first.api_key == "FAKE-B"
    assert model == cached_model == expected
    adapter = first.chat.completions
    if async_mode:
        adapter = adapter._sync
    payload, _, _ = adapter._build_responses_kwargs({"model": cached_model, "messages": []})
    assert payload["model"] == expected
    assert select.call_count == 1
    assert all(call.kwargs.get("model") == expected for call in select.call_args_list)
    assert peek.call_count > 0
    assert all(call.kwargs.get("model") == expected for call in peek.call_args_list)
    assert all(key[10] == expected for key in aux._client_cache)


@pytest.mark.parametrize("route_kind", ["fast", "moa"])
@pytest.mark.parametrize("override", [None, "openai:" + SIBLING])
def test_auto_substitutions_precede_override_and_cache_hint(make_pool, monkeypatch, route_kind, override):
    expected = SIBLING if override else TARGET
    make_pool([("A", {expected: time.time() + 3600}), ("B", {})])
    if route_kind == "fast":
        monkeypatch.setattr(aux, "_task_prefers_fast_model", lambda task: True)
        monkeypatch.setattr(aux, "_get_aux_model_for_provider", lambda *args, **kwargs: TARGET)
        runtime = {"provider": "openai-codex", "model": SIBLING}
    else:
        monkeypatch.setattr(aux, "_resolve_moa_aggregator", lambda preset: ("openai-codex", TARGET))
        runtime = {"provider": "moa", "model": "test-preset", "base_url": "moa://local", "api_key": "facade"}
    first, model = aux._get_cached_client("auto", override, main_runtime=runtime, task="title_generation")
    second, cached = aux._get_cached_client("auto", override, main_runtime=runtime, task="title_generation")
    assert first is not None
    assert first is second
    assert first.api_key == "FAKE-B"
    assert model == cached == expected
    assert all(key[10] == expected for key in aux._client_cache)


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("chain", ["task", "main"])
def test_auto_unavailable_main_applies_override_before_fallback_selection(make_pool, monkeypatch, chain, async_mode):
    pool = make_pool([("A", {TARGET: time.time() + 3600}), ("B", {})])
    monkeypatch.setattr(aux, "_try_main_provider_route", lambda *args: None)
    entry = {"provider": "codex", "model": SIBLING}
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: {"fallback_chain": [entry]} if chain == "task" else {})
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"fallback_providers": [entry]} if chain == "main" else {})
    monkeypatch.setattr(aux, "_is_provider_unhealthy", lambda *args: False)
    loop = SimpleNamespace(is_closed=lambda: False)
    monkeypatch.setattr(aux, "_current_event_loop", lambda: loop)
    runtime = {"provider": "openrouter", "model": "vendor/main"}
    first, model = aux._get_cached_client("auto", TARGET, main_runtime=runtime, task="summarize", async_mode=async_mode)
    assert first is not None
    assert first.api_key == "FAKE-B"
    assert model == TARGET
    pool.entries()[1].model_cooldowns[TARGET] = time.time() + 3600
    # A fallback client may not be cached behind the unrelated main-provider hint.
    second, _ = aux._get_cached_client("auto", TARGET, main_runtime=runtime, task="summarize", async_mode=async_mode)
    assert second is None

@pytest.mark.parametrize("override", [None, "gpt-5.5"])
def test_auto_missing_main_model_does_not_enable_previously_skipped_main_route(make_pool, monkeypatch, override):
    make_pool([("A", {}), ("B", {})])
    monkeypatch.setattr(aux, "_read_main_model", lambda: "")
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: {})
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", dict)
    assert aux.resolve_provider_client("auto", override, main_runtime={"provider": "codex", "model": ""}) == (None, None)


def test_auto_non_codex_override_keeps_existing_wire_spelling(monkeypatch):
    client = SimpleNamespace(base_url="https://openrouter.ai/api/v1")
    monkeypatch.setattr(aux, "_try_openrouter", lambda **kwargs: (client, "openai/gpt-5.5"))
    actual, model = aux.resolve_provider_client("auto", "gpt-5.5", main_runtime={"provider": "openrouter", "model": "vendor/main"})
    assert actual is client
    assert model == "gpt-5.5"


@pytest.mark.parametrize("target", ["gpt-5.5", TARGET])
def test_auto_restricted_main_uses_configured_fallback_not_discovery(make_pool, monkeypatch, target):
    make_pool([("A", {target: time.time() + 3600})])
    fallback = SimpleNamespace(base_url="https://openrouter.ai/api/v1")
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: {
        "fallback_chain": [{"provider": "openrouter", "model": "vendor/fallback", "api_mode": "chat_completions"}]
    })
    monkeypatch.setattr(aux, "_try_openrouter", lambda **kwargs: (fallback, "vendor/fallback"))
    main_chain = Mock(side_effect=AssertionError("task fallback must win"))
    discovery = Mock(side_effect=AssertionError("must not guess providers"))
    monkeypatch.setattr(aux, "_try_main_fallback_chain", main_chain)
    monkeypatch.setattr(aux, "_try_discovery_chain", discovery)
    client, model = aux.resolve_provider_client("auto", target, task="summarize", main_runtime={"provider": "codex", "model": SIBLING})
    assert client is fallback
    assert model == target
    main_chain.assert_not_called()
    discovery.assert_not_called()



