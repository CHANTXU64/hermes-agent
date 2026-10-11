"""Pin targeted operations to one real agent-browser daemon, not its reusable name.

The Unix line-JSON wire format is from agent-browser v0.26.0
cli/src/{connection,commands}.rs. Only our tab/navigation subset is encoded here.
A connected socket cannot auto-start or hop to a new daemon as the CLI can.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import stat
import uuid

import psutil


def daemon_identity(info: dict) -> tuple[str, str]:
    from tools import browser_tool_session as session
    name = info['session_name']
    folder = session._prepare_session_socket_dir(name)
    path = str(Path(folder) / f'{name}.sock')
    try:
        pid = session._read_browser_daemon_pid(folder, name)
        if pid is None:
            raise RuntimeError('Browser daemon is unavailable; list browser_tabs again.')
        proc = psutil.Process(pid)
        stamp = os.stat(path)
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE or not stat.S_ISSOCK(stamp.st_mode):
            raise RuntimeError('Browser daemon is unavailable; list browser_tabs again.')
        identity = f'{pid}:{proc.create_time()}:{stamp.st_dev}:{stamp.st_ino}'
        return identity, path
    except (OSError, psutil.Error) as exc:
        raise RuntimeError('Browser daemon is unavailable; list browser_tabs again.') from exc


def _request(command: str, args: list[str]) -> dict:
    if command == 'open' and len(args) == 1:
        payload = {'action': 'navigate', 'url': args[0]}
    elif command == 'tab' and args == ['list']:
        payload = {'action': 'tab_list'}
    elif command == 'tab' and args == ['new']:
        payload = {'action': 'tab_new'}
    elif command == 'tab' and len(args) == 2 and args[0] == 'close':
        payload = {'action': 'tab_close', 'tabId': args[1]}
    elif command == 'tab' and len(args) == 1 and args[0].startswith('t'):
        payload = {'action': 'tab_switch', 'tabId': args[0]}
    else:
        raise ValueError('Unsupported pinned browser command; not sent.')
    return {'id': uuid.uuid4().hex, **payload}


def run_pinned(info: dict, command: str, args: list[str], expected: str, timeout: float) -> dict:
    """No spawn or retry. Check identity after connect, before sending any action."""
    try:
        payload = _request(command, args)
        identity, path = daemon_identity(info)
        if identity != expected:
            raise RuntimeError('Browser daemon changed; list browser_tabs again before retrying.')
        if not hasattr(socket, 'AF_UNIX'):
            raise RuntimeError('Pinned browser tab commands require a Unix domain socket.')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(path)
            # A restart between the first check and connect must receive no command.
            if daemon_identity(info) != (expected, path):
                raise RuntimeError('Browser daemon changed; list browser_tabs again before retrying.')
            connection.sendall((json.dumps(payload) + '\n').encode())
            with connection.makefile('rb') as stream:
                raw = stream.readline(16 * 1024 * 1024 + 1)
            if not raw.endswith(b'\n') or len(raw) > 16 * 1024 * 1024:
                raise RuntimeError('Browser daemon response missing or truncated; action was not retried.')
            result = json.loads(raw)
            if not isinstance(result, dict) or not isinstance(result.get('success'), bool):
                raise RuntimeError('Invalid browser daemon response; action was not retried.')
            return result
    except (OSError, ValueError, RuntimeError) as exc:
        return {'success': False, 'error': str(exc)}
