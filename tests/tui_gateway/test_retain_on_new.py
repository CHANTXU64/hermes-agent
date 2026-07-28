"""TUI/Desktop retain-before-new session gate regressions."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import tui_gateway.server as server


def _install_session(provider):
    sid = "live-sid"
    memory_manager = SimpleNamespace(
        get_provider=lambda name: provider if name == "hindsight" else None,
        flush_pending=MagicMock(return_value=True),
    )
    agent = SimpleNamespace(
        _memory_manager=memory_manager,
        session_id="persisted-sid",
    )
    session = {
        "agent": agent,
        "session_key": "persisted-sid",
        "running": False,
    }
    server._sessions[sid] = session
    return sid, session, memory_manager


def test_tui_retain_before_new_waits_for_provider_acknowledgement(monkeypatch):
    provider = MagicMock()
    provider.retain_on_new_enabled = True
    provider.retain_before_session_reset.return_value = {
        "queued": True,
        "turn_count": 2,
    }
    sid, session, memory_manager = _install_session(provider)
    monkeypatch.setattr(
        server,
        "_get_db",
        lambda: SimpleNamespace(
            get_session=lambda session_id: {"parent_session_id": "parent-sid"}
        ),
    )

    try:
        response = server._methods["session.retain_before_new"](
            "r1",
            {"session_id": sid},
        )
    finally:
        server._sessions.pop(sid, None)

    assert response["result"] == {"queued": True, "turn_count": 2}
    assert server._sessions.get(sid) is None
    provider.retain_before_session_reset.assert_called_once_with(
        session_id="persisted-sid",
        parent_session_id="parent-sid",
        flush_pending=memory_manager.flush_pending,
    )
    assert session["agent"] is not None


def test_tui_retain_before_new_returns_error_without_mutating_session(monkeypatch):
    provider = MagicMock()
    provider.retain_on_new_enabled = True
    provider.retain_before_session_reset.side_effect = RuntimeError(
        "retain api unavailable"
    )
    sid, session, _ = _install_session(provider)
    monkeypatch.setattr(
        server,
        "_get_db",
        lambda: SimpleNamespace(
            get_session=lambda session_id: {"parent_session_id": ""}
        ),
    )

    try:
        response = server._methods["session.retain_before_new"](
            "r1",
            {"session_id": sid},
        )
        assert "retain api unavailable" in response["error"]["message"]
        assert server._sessions[sid] is session
        assert session["agent"] is not None
    finally:
        server._sessions.pop(sid, None)
