"""Pinned socket operations cannot jump to a replacement agent-browser daemon."""
import json
import os
import socket
import threading

import pytest


def test_short_ids_change_when_daemon_changes_under_same_session():
    from fork_features.browser_tabs import public_id
    first = {'session_name': 'same', '_tab_daemon_identity': 'pid1:birth1:socket1'}
    second = dict(first, _tab_daemon_identity='pid2:birth2:socket2')
    assert public_id(first, 't2') != public_id(second, 't2')


@pytest.fixture
def daemon(tmp_path):
    # Keep AF_UNIX path shorter than macOS sockaddr_un's limit.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='btg-', dir=os.environ.get('TMPDIR')) as folder:
        path = os.path.join(folder, 's.sock')
        if len(path.encode()) >= 104:
            pytest.skip('test AF_UNIX path exceeds platform limit')
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path); server.listen(1); server.settimeout(3)
        received = []
        def run():
            try:
                conn, _ = server.accept()
                with conn, conn.makefile('rb') as stream:
                    line = stream.readline()
                    if line:
                        received.append(json.loads(line))
                        conn.sendall(b'{"success":true,"data":{"tabId":"t2"}}\n')
            except (TimeoutError, OSError):
                pass
        worker = threading.Thread(target=run)
        worker.start()
        yield path, received
        server.close(); worker.join(4)


def test_same_daemon_receives_native_switch_without_cli(monkeypatch, daemon):
    from fork_features import browser_daemon as guard
    path, received = daemon
    monkeypatch.setattr(guard, 'daemon_identity', lambda info: ('generation-a', path))
    result = guard.run_pinned({}, 'tab', ['t2'], 'generation-a', 2)
    assert result['success'] is True
    assert received[0]['action'] == 'tab_switch'
    assert received[0]['tabId'] == 't2'


def test_replacement_between_check_and_connect_receives_no_mutation(monkeypatch, daemon):
    from fork_features import browser_daemon as guard
    path, received = daemon
    identities = iter(['generation-a', 'generation-b'])
    monkeypatch.setattr(guard, 'daemon_identity', lambda info: (next(identities), path))
    result = guard.run_pinned({}, 'open', ['https://example.com/'], 'generation-a', 2)
    assert result['success'] is False
    assert 'changed' in result['error']
    assert received == []


def test_dead_daemon_is_not_spawned_or_retried(monkeypatch):
    from fork_features import browser_daemon as guard
    def missing(info):
        raise RuntimeError('daemon unavailable')
    monkeypatch.setattr(guard, 'daemon_identity', missing)
    result = guard.run_pinned({}, 'tab', ['close', 't2'], 'generation-a', 2)
    assert result['success'] is False
