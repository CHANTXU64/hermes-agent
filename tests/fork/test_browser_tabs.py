"""Explicit tab addressing without changing the other browser tool schemas."""
import json

import tools.browser_tool as bt
import pytest


@pytest.fixture(autouse=True)
def _fixture_daemon_identity(monkeypatch):
    monkeypatch.setattr('fork_features.browser_daemon.daemon_identity', lambda info: ('fixture-generation', '/unused'))

from tools import browser_tool_session as session
from tools.registry import registry


def test_tabs_list_exposes_titles_and_separate_selection_states(monkeypatch):
    from tools import browser_tool_session as session
    monkeypatch.setattr(session, '_get_session_info', lambda key: {'session_name': 'cdp_abcdef1234', 'features': {}})
    monkeypatch.setattr(session, '_run_browser_command', lambda *a, **k: {
        'success': True, 'data': {'tabs': [
            {'tabId': 't1', 'title': 'Keep', 'url': 'https://one.example/', 'active': False},
            {'tabId': 't2', 'title': 'Work', 'url': 'https://two.example/', 'active': True},
        ]}})
    entry = registry.get_entry('browser_tabs')
    assert entry is not None, 'browser_tabs must be registered'
    result = json.loads(registry.dispatch('browser_tabs', {'action': 'list'}, task_id='tabs-test'))
    assert result['success'] is True
    assert [t['title'] for t in result['tabs']] == ['Keep', 'Work']
    assert [t['agent_active'] for t in result['tabs']] == [False, True]
    assert all('chrome_active' in t and 'window_id' in t for t in result['tabs'])
    assert all(t['chrome_active'] is None for t in result['tabs'])
    assert len({t['id'] for t in result['tabs']}) == 2


def test_navigate_schema_requires_tab_id():
    schema = next(s for s in bt.BROWSER_TOOL_SCHEMAS if s['name'] == 'browser_navigate')
    assert 'tab_id' in schema['parameters']['required']


def _driver(monkeypatch, *, real_policy=False):
    from fork_features import browser_tabs as tabs
    from tools import browser_tool_session as session
    pages = [{'tabId': 't1', 'title': 'Keep', 'url': 'https://one.example/', 'active': True},
             {'tabId': 't2', 'title': 'Work', 'url': 'https://two.example/', 'active': False}]
    info = {'session_name': 'cdp_abcdef1234', '_tab_daemon_identity': 'fixture-generation', 'features': {'local': True, 'proxies': True}}
    calls = []
    def run(key, command, args=None, **kw):
        args = args or []
        calls.append((command, args))
        if command == 'tab':
            if args == ['list']:
                return {'success': True, 'data': {'tabs': [dict(p) for p in pages]}}
            if args == ['new']:
                for p in pages: p['active'] = False
                pages.append({'tabId': 't3', 'title': '', 'url': 'about:blank', 'active': True})
                return {'success': True, 'data': {'tabId': 't3'}}
            if args[0] == 'close':
                pages[:] = [p for p in pages if p['tabId'] != args[1]]
                return {'success': True, 'data': {'closed': True, 'tabId': args[1]}}
            for p in pages: p['active'] = p['tabId'] == args[0]
            return {'success': True, 'data': {'tabId': args[0]}}
        page = next(p for p in pages if p['active'])
        if command == 'open': page['url'] = args[0]
        if command == 'snapshot': return {'success': True, 'data': {'snapshot': page['tabId'], 'refs': {}}}
        return {'success': True, 'data': {'url': page['url'], 'title': page['title']}}
    monkeypatch.setattr(session, '_get_session_info', lambda key: info)
    monkeypatch.setattr(session, '_run_browser_command', run)
    monkeypatch.setattr(bt, '_is_camofox_mode', lambda: False)
    monkeypatch.setattr(bt, '_navigation_session_key', lambda key, url: key)
    if real_policy:
        # Exercise real normalization, SSRF and website policy, not a blanket
        # allow mock. Simulate transport, backend settings and public DNS only.
        from tools import url_safety
        resolve = url_safety._getaddrinfo
        monkeypatch.setattr(url_safety, '_getaddrinfo', lambda host, *a, **kw:
            [(2, 1, 6, '', ('93.184.216.34', 443))] if host == 'example.com'
            else resolve(host, *a, **kw))
        monkeypatch.setattr(bt._cloud, '_is_local_backend', lambda: False)
        monkeypatch.setattr(bt._cloud, '_allow_private_urls', lambda: False)
        monkeypatch.setattr(bt, '_is_local_sidecar_key', lambda key: False)
    else:
        monkeypatch.setattr(bt, '_secret_url_error_normalized', lambda url: (url, None))
        monkeypatch.setattr(bt, '_url_policy_error', lambda *a, **k: None)
    monkeypatch.setattr(bt, '_maybe_start_recording', lambda *a: None)
    return tabs, pages, info, calls


def test_new_blank_then_navigate_with_real_url_policy(monkeypatch):
    _tabs, pages, _info, calls = _driver(monkeypatch, real_policy=True)
    created = json.loads(bt.browser_tabs(action='new', task_id='blank-policy'))
    assert created['success'] is True
    assert pages[-1]['url'] == 'about:blank'
    assert bt._url_policy_error('about:blank') is not None
    result = json.loads(bt.browser_navigate('https://example.com/',
        task_id='blank-policy', tab_id=created['tab_id']))
    assert result['success'] is True, result
    assert result['snapshot'] == 't3'
    assert pages[-1]['url'] == 'https://example.com/'
    assert [p['url'] for p in pages[:2]] == ['https://one.example/', 'https://two.example/']
    assert calls.index(('tab', ['t3'])) < calls.index(('open', ['https://example.com/']))


def test_new_with_url_uses_real_navigation_policy(monkeypatch):
    _tabs, pages, _info, calls = _driver(monkeypatch, real_policy=True)
    result = json.loads(bt.browser_tabs(action='new', url='https://example.com/', task_id='new-policy'))
    assert result['success'] is True, result
    assert result['snapshot'] == 't3'
    assert pages[-1]['url'] == 'https://example.com/'
    assert calls.count(('tab', ['new'])) == 1


@pytest.mark.parametrize('old_url', [
    'about:blank', 'chrome://newtab/', 'chrome-error://chromewebdata/',
    'http://127.0.0.1/private', 'http://169.254.169.254/latest/meta-data/',
])
def test_navigation_leaves_blocked_document_but_switch_cannot_access_it(monkeypatch, old_url):
    tabs, pages, info, calls = _driver(monkeypatch, real_policy=True)
    pages[1]['url'] = old_url
    tab_id = tabs.public_id(info, 't2')
    switched = json.loads(bt.browser_tabs(action='switch', tab_id=tab_id, task_id='old-policy'))
    assert switched['success'] is False
    assert pages[0]['active'] is True
    assert calls == [('tab', ['list'])]
    calls.clear()
    navigated = json.loads(bt.browser_navigate('https://example.com/', task_id='old-policy', tab_id=tab_id))
    assert navigated['success'] is True, navigated
    assert pages[0]['url'] == 'https://one.example/'
    assert pages[1]['url'] == 'https://example.com/'
    assert calls[:3] == [('tab', ['list']), ('tab', ['t2']), ('open', ['https://example.com/'])]


@pytest.mark.parametrize('url', [
    'about:blank', 'chrome://settings/', 'file:///etc/passwd', 'javascript:alert(1)',
    'http://127.0.0.1/private', 'http://10.0.0.1/',
    'http://169.254.169.254/latest/meta-data/', 'http://metadata.google.internal/',
])
@pytest.mark.parametrize('action', ['navigate', 'new'])
def test_unsafe_destination_rejected_before_tab_mutation(monkeypatch, url, action):
    tabs, pages, info, calls = _driver(monkeypatch, real_policy=True)
    if action == 'new':
        result = json.loads(bt.browser_tabs(action='new', url=url, task_id='unsafe-policy'))
    else:
        result = json.loads(bt.browser_navigate(url, task_id='unsafe-policy', tab_id=tabs.public_id(info, 't2')))
    assert result['success'] is False, result
    assert calls == []
    assert pages[0]['active'] is True
    assert pages[1]['url'] == 'https://two.example/'


def test_closed_wrong_and_old_generation_ids_cannot_navigate_with_real_policy(monkeypatch):
    tabs, pages, info, calls = _driver(monkeypatch, real_policy=True)
    created = json.loads(bt.browser_tabs(action='new', task_id='expired-policy'))
    assert json.loads(bt.browser_tabs(action='close', tab_id=created['tab_id'], task_id='expired-policy'))['success']
    calls.clear()
    before = [dict(p) for p in pages]
    stale_generation = tabs.public_id(dict(info, _tab_daemon_identity='previous-generation'), 't2')
    for tab_id in [created['tab_id'], 't2', 'wrong:t2', stale_generation, None, '']:
        result = json.loads(bt.browser_navigate('https://example.com/', task_id='expired-policy', tab_id=tab_id))
        assert result['success'] is False
    assert pages == before
    assert all(c == 'tab' and a == ['list'] for c, a in calls)


@pytest.mark.parametrize('redirect_url', ['http://127.0.0.1/private', 'http://169.254.169.254/latest/meta-data/'])
def test_redirect_policy_still_blocks_before_snapshot_after_blank_navigation(monkeypatch, redirect_url):
    _tabs, pages, _info, calls = _driver(monkeypatch, real_policy=True)
    created = json.loads(bt.browser_tabs(action='new', task_id='redirect-policy'))
    run = session._run_browser_command
    def redirect(key, command, args=None, **kw):
        result = run(key, command, args, **kw)
        if command == 'open' and args == ['https://example.com/']:
            pages[-1]['url'] = redirect_url
            result['data']['url'] = redirect_url
        return result
    monkeypatch.setattr(session, '_run_browser_command', redirect)
    result = json.loads(bt.browser_navigate('https://example.com/', task_id='redirect-policy', tab_id=created['tab_id']))
    assert result['success'] is False
    assert 'redirect landed' in result['error']
    assert pages[-1]['url'] == 'about:blank'
    assert not any(c == 'snapshot' for c, _ in calls)
    assert pages[0]['url'] == 'https://one.example/'


def test_navigate_selects_requested_tab_before_open_without_new(monkeypatch):
    tabs, pages, info, calls = _driver(monkeypatch)
    result = json.loads(bt.browser_navigate('https://next.example/', task_id='selected',
                                            tab_id=tabs.public_id(info, 't2')))
    assert result['success'] is True
    assert result['snapshot'] == 't2'
    assert pages[0]['url'] == 'https://one.example/'
    assert pages[1]['url'] == 'https://next.example/'
    assert ('tab', ['new']) not in calls
    assert calls.index(('tab', ['t2'])) < calls.index(('open', ['https://next.example/']))


def test_missing_and_stale_ids_never_navigate_or_select(monkeypatch):
    tabs, pages, info, calls = _driver(monkeypatch)
    for tab_id in (None, '', 't2', tabs.public_id(dict(info, session_name='old-session'), 't2')):
        result = json.loads(bt.browser_navigate('https://next.example/', task_id='invalid', tab_id=tab_id))
        assert result['success'] is False
    assert all(c == 'tab' and a == ['list'] for c, a in calls)
    assert pages[0]['active'] is True


def test_tab_new_switch_and_close(monkeypatch):
    tabs, pages, info, _calls = _driver(monkeypatch)
    created = json.loads(bt.browser_tabs(action='new', task_id='actions'))
    assert created['success'] is True
    assert created['tab_id'] == tabs.public_id(info, 't3')
    selected = json.loads(bt.browser_tabs(action='switch', tab_id=tabs.public_id(info, 't2'), task_id='actions'))
    assert selected['success'] is True
    assert next(p['tabId'] for p in pages if p['active']) == 't2'
    closed = json.loads(bt.browser_tabs(action='close', tab_id=created['tab_id'], task_id='actions'))
    assert closed['success'] is True
    assert [p['tabId'] for p in pages] == ['t1', 't2']


def test_chrome_window_selection_is_distinct_and_ambiguous_urls_are_unknown():
    from fork_features import browser_tabs_chrome as chrome
    native = [{'tabId': 't1', 'url': 'https://one.example/'},
              {'tabId': 't2', 'url': 'https://two.example/'},
              {'tabId': 't3', 'url': 'https://duplicate.example/'},
              {'tabId': 't4', 'url': 'https://duplicate.example/'}]
    observed = [
        {'url': 'https://one.example/', 'window_id': 7, 'visible': False},
        {'url': 'https://two.example/', 'window_id': 7, 'visible': True},
        {'url': 'https://duplicate.example/', 'window_id': 8, 'visible': True},
        {'url': 'https://duplicate.example/', 'window_id': 8, 'visible': False},
    ]
    states = chrome.match_selection(native, observed)
    assert states['t1'] == {'window_id': 7, 'chrome_active': False}
    assert states['t2'] == {'window_id': 7, 'chrome_active': True}
    assert states['t3']['chrome_active'] is None
    assert states['t4']['chrome_active'] is None
    observed[1]['visible'] = False  # minimized/hidden: no positively selected tab
    assert chrome.match_selection(native, observed)['t1']['chrome_active'] is None


def test_js_description_warns_about_independent_cdp_target():
    description = next(s['description'] for s in bt.BROWSER_TOOL_SCHEMAS if s['name'] == 'browser_console')
    assert 'target_id' in description
    assert 'browser_cdp' in description
    assert 'initial tab' in description


def test_navigation_serializes_select_open_and_snapshot_per_task():
    import threading
    from fork_features import browser_tabs as tabs
    started = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    order = []
    @tabs.serialize_navigation
    def navigate(url, task_id=None, tab_id=None):
        order.append(tab_id)
        if tab_id == 'first':
            started.set()
            assert release.wait(3)
        else:
            second_started.set()
        order.append(url)
    first = threading.Thread(target=navigate, args=('one',), kwargs={'task_id': 'same', 'tab_id': 'first'})
    second = threading.Thread(target=navigate, args=('two',), kwargs={'task_id': 'same', 'tab_id': 'second'})
    first.start()
    assert started.wait(3)
    second.start()
    assert not second_started.is_set()
    release.set()
    first.join(3); second.join(3)
    assert order == ['first', 'one', 'second', 'two']


def test_registry_missing_tab_id_fails_before_routing(monkeypatch):
    def unexpected(*a, **k):
        raise AssertionError('must reject before routing')
    monkeypatch.setattr(bt, 'routed_browser_handler', unexpected)
    result = json.loads(registry.dispatch('browser_navigate', {'url': 'https://example.com'}, task_id='bad'))
    assert result['success'] is False
    assert 'tab_id' in result['error']


def test_targeted_command_rejects_replaced_session_before_dispatch(monkeypatch):
    monkeypatch.setattr(session, '_browser_command_preflight', lambda: {'browser_cmd': ['mock']})
    monkeypatch.setattr(session, '_get_session_info', lambda task: {'session_name': 'replacement'})
    def unexpected(*a, **k):
        raise AssertionError('must not execute on replacement session')
    monkeypatch.setattr(session, '_dispatch_browser_command', unexpected)
    result = session._run_browser_command('task', 'open', ['https://example.com'], expected_session_name='original')
    assert result['success'] is False
    assert 'changed' in result['error']


def test_targeted_command_does_not_retry_mutation_on_replacement(monkeypatch):
    monkeypatch.setattr(session, '_browser_command_preflight', lambda: {'browser_cmd': ['mock']})
    monkeypatch.setattr(session, '_get_session_info', lambda task: {'session_name': 'original'})
    monkeypatch.setattr(session, 'run_fenced_pair', lambda info, fn: fn())
    calls = []
    def failed(*a, **k):
        calls.append(a)
        return 'chrome', {'success': False, 'error': 'daemon failed', 'returncode': 101}
    monkeypatch.setattr(session, '_dispatch_browser_command', failed)
    result = session._run_browser_command('task', 'tab', ['close', 't1'], expected_session_name='original')
    assert result['success'] is False
    assert len(calls) == 1
