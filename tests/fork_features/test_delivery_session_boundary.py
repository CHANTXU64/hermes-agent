"""Fork policy for retiring durable replies at a real session boundary."""

from __future__ import annotations

import threading

import pytest

from gateway import delivery_ledger as dl
from gateway.config import Platform
from gateway.session import SessionSource, build_session_key


def _state(obligation_id: str) -> str | None:
    with dl._connect() as conn:
        row = conn.execute(
            "SELECT state FROM delivery_obligations WHERE obligation_id=?",
            (obligation_id,),
        ).fetchone()
    return None if row is None else str(row[0])


@pytest.fixture(autouse=True)
def _isolated_delivery_ledger(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(dl, "_db_path", lambda: home / "state.db")


@pytest.mark.asyncio
async def test_named_telegram_account_boundary_only_retires_that_bot() -> None:
    from fork_features.delivery_session_boundary import retire_session_deliveries

    primary_key = build_session_key(
        SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="5612546357",
            chat_type="dm",
            user_id="5612546357",
        )
    )
    work_key = build_session_key(
        SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="5612546357",
            chat_type="dm",
            user_id="5612546357",
            account_id="work",
        )
    )
    for obligation_id, session_key in (
        ("primary-reply", primary_key),
        ("work-reply", work_key),
    ):
        dl.record_obligation(
            obligation_id=obligation_id,
            session_key=session_key,
            platform="telegram",
            chat_id="5612546357",
            thread_id=None,
            content=obligation_id,
        )
        dl.mark_failed(obligation_id, "network down")

    assert await retire_session_deliveries(work_key) == 1
    assert _state("work-reply") == "superseded"
    assert _state("primary-reply") == "failed"


@pytest.mark.asyncio
async def test_boundary_runs_ledger_write_off_event_loop(monkeypatch) -> None:
    from fork_features.delivery_session_boundary import retire_session_deliveries

    event_loop_thread = threading.get_ident()
    worker_threads: list[int] = []

    def _supersede(session_key: str) -> int:
        assert session_key == "agent:main:telegram:dm:1"
        worker_threads.append(threading.get_ident())
        return 2

    monkeypatch.setattr(dl, "supersede_session_obligations", _supersede)

    assert await retire_session_deliveries("agent:main:telegram:dm:1") == 2
    assert worker_threads and worker_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_boundary_is_fail_open(monkeypatch) -> None:
    from fork_features.delivery_session_boundary import retire_session_deliveries

    def _fail(_session_key: str) -> int:
        raise RuntimeError("state db unavailable")

    monkeypatch.setattr(dl, "supersede_session_obligations", _fail)

    assert await retire_session_deliveries("agent:main:telegram:dm:1") == 0
    assert await retire_session_deliveries("") == 0
