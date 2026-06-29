"""Fork-owned Codex prompt-cache routing regressions.

These tests protect local cache-routing behavior while upstream owns the
baseline Responses API transport tests.
"""

from agent.transports.codex import ResponsesApiTransport
from tests.run_agent.test_run_agent_codex_responses import _build_agent


def test_codex_backend_uses_content_addressed_thread_headers_without_stable_scope():
    """Cron-like physical sessions should not become the logical cache thread."""
    transport = ResponsesApiTransport()

    kw = transport.build_kwargs(
        model="gpt-5.4",
        messages=[{"role": "user", "content": "Hi"}],
        tools=[],
        session_id="conv-codex-1",
        is_codex_backend=True,
    )

    pck = kw["prompt_cache_key"]
    assert pck.startswith("pck_")
    assert pck != "conv-codex-1"
    assert kw["extra_headers"] == {
        "session_id": "conv-codex-1",
        "thread-id": pck,
        "x-client-request-id": pck,
    }
    assert "session-id" not in kw["extra_headers"]


def test_codex_backend_explicit_prompt_cache_key_keeps_stable_thread():
    """Gateway/compression logical cache scope wins over content hash."""
    transport = ResponsesApiTransport()

    kw = transport.build_kwargs(
        model="gpt-5.4",
        messages=[{"role": "user", "content": "Hi"}],
        tools=[],
        session_id="physical-child-session",
        prompt_cache_key="agent:main:telegram:dm:123",
        is_codex_backend=True,
    )

    assert kw["prompt_cache_key"] == "agent:main:telegram:dm:123"
    assert kw["extra_headers"] == {
        "session_id": "physical-child-session",
        "thread-id": "agent:main:telegram:dm:123",
        "x-client-request-id": "agent:main:telegram:dm:123",
    }
    assert "session-id" not in kw["extra_headers"]


def test_codex_backend_content_thread_headers_without_session_id():
    transport = ResponsesApiTransport()

    kw = transport.build_kwargs(
        model="gpt-5.4",
        messages=[{"role": "user", "content": "Hi"}],
        tools=[],
        is_codex_backend=True,
    )

    pck = kw["prompt_cache_key"]
    assert pck.startswith("pck_")
    assert kw["extra_headers"] == {
        "thread-id": pck,
        "x-client-request-id": pck,
    }
    assert "session_id" not in kw["extra_headers"]


def test_agent_codex_uses_gateway_key_as_cache_thread(monkeypatch):
    agent = _build_agent(monkeypatch)
    agent.session_id = "20260615_153111_a37238"
    agent._gateway_session_key = "agent:main:telegram:dm:5612546357"

    kwargs = agent._build_api_kwargs(
        [
            {"role": "system", "content": "You are Hermes."},
            {"role": "user", "content": "Ping"},
        ]
    )

    assert kwargs["prompt_cache_key"] == "agent:main:telegram:dm:5612546357"
    headers = kwargs.get("extra_headers") or {}
    assert headers.get("session_id") == "20260615_153111_a37238"
    assert headers.get("thread-id") == "agent:main:telegram:dm:5612546357"
    assert headers.get("x-client-request-id") == "agent:main:telegram:dm:5612546357"
    assert "session-id" not in headers


def test_agent_codex_uses_compression_root_as_cache_thread(monkeypatch):
    class FakeSessionDB:
        def __init__(self):
            self.rows = {
                "root-session": {
                    "id": "root-session",
                    "parent_session_id": None,
                    "end_reason": "compression",
                    "started_at": 1,
                    "ended_at": 2,
                },
                "mid-session": {
                    "id": "mid-session",
                    "parent_session_id": "root-session",
                    "end_reason": "compression",
                    "started_at": 3,
                    "ended_at": 4,
                },
                "child-session": {
                    "id": "child-session",
                    "parent_session_id": "mid-session",
                    "started_at": 5,
                },
            }

        def get_session(self, session_id):
            return self.rows.get(session_id)

    agent = _build_agent(monkeypatch)
    agent.session_id = "child-session"
    agent._session_db = FakeSessionDB()

    kwargs = agent._build_api_kwargs(
        [
            {"role": "system", "content": "You are Hermes."},
            {"role": "user", "content": "Ping"},
        ]
    )

    assert kwargs["prompt_cache_key"] == "root-session"
    headers = kwargs.get("extra_headers") or {}
    assert headers.get("session_id") == "child-session"
    assert headers.get("thread-id") == "root-session"
    assert headers.get("x-client-request-id") == "root-session"
    assert "session-id" not in headers


def test_agent_codex_content_addresses_non_compression_child_cache_thread(monkeypatch):
    class FakeSessionDB:
        def __init__(self):
            self.rows = {
                "parent-session": {
                    "id": "parent-session",
                    "parent_session_id": None,
                    "started_at": 1,
                    "ended_at": None,
                },
                "child-session": {
                    "id": "child-session",
                    "parent_session_id": "parent-session",
                    "started_at": 2,
                },
            }

        def get_session(self, session_id):
            return self.rows.get(session_id)

    agent = _build_agent(monkeypatch)
    agent.session_id = "child-session"
    agent._session_db = FakeSessionDB()

    kwargs = agent._build_api_kwargs(
        [
            {"role": "system", "content": "You are Hermes."},
            {"role": "user", "content": "Ping"},
        ]
    )

    pck = kwargs["prompt_cache_key"]
    assert pck.startswith("pck_")
    assert pck not in {"child-session", "parent-session"}
    headers = kwargs.get("extra_headers") or {}
    assert headers.get("session_id") == "child-session"
    assert headers.get("thread-id") == pck
    assert headers.get("x-client-request-id") == pck
