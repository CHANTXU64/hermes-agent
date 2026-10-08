"""Delivery retries honor the platform deadline without crossing ownership boundaries."""
import asyncio
import os
import sqlite3
import time
from typing import Any

import pytest

from gateway import delivery_ledger as dl


def record(oid, *, profile=None):
    dl.record_obligation(obligation_id=oid, session_key='session-' + oid, platform='telegram',
                         chat_id='123', thread_id='77', content='.' * 3000, adapter_profile=profile)
    dl.mark_failed(oid, 'flood_control:185')


def read(oid):
    with sqlite3.connect(dl._db_path()) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute('SELECT * FROM delivery_obligations WHERE obligation_id=?', (oid,)).fetchone())


def test_runtime_deadline_preserves_scope_and_retry_budget():
    record('due')
    record('other', profile='other')
    record('blocked')
    dl.mark_failed('blocked', 'Forbidden: bot was blocked by the user')
    stamp = read('due')['updated_at']
    assert dl.sweep_failed_for_runtime('telegram', now=stamp + 184) == []
    assert read('due')['attempts'] == 0
    claimed = dl.sweep_failed_for_runtime('telegram', now=stamp + 186)
    assert [r['obligation_id'] for r in claimed] == ['due']
    assert claimed[0]['needs_marker'] and 'rate limit' in claimed[0]['marker']
    assert read('due')['last_error'] is None
    assert dl.sweep_failed_for_runtime('telegram', now=stamp + 187) == []
    assert dl.release_runtime_claim('due', claimed[0]['last_error'])
    assert read('due')['attempts'] == 0
    assert dl.sweep_failed_for_runtime('telegram', now=time.time()) == []
    claimed = dl.sweep_failed_for_runtime('telegram', now=time.time() + 186)
    assert len(claimed) == 1
    dl.mark_delivered('due')
    assert dl.sweep_failed_for_runtime('telegram', now=time.time() + 187) == []
    assert read('other')['attempts'] == read('blocked')['attempts'] == 0


def test_boot_adopts_waiting_rows_without_spending_or_losing_deadline():
    record('boot')
    original = read('boot')
    # No PID means a genuinely ownerless persisted row; do not patch the liveness predicate.
    with sqlite3.connect(dl._db_path()) as conn:
        conn.execute("UPDATE delivery_obligations SET owner_pid=NULL, owner_started_at=NULL, adapter_profile=NULL")
    claimed = dl.sweep_recoverable(now=original['updated_at'] + 20,
                                    deliverable_targets={('telegram', None)})
    assert len(claimed) == 1 and claimed[0].get('adopted')
    adopted = read('boot')
    assert adopted['owner_pid'] == os.getpid()
    assert adopted['adapter_profile'] == 'default'
    assert adopted['attempts'] == 0 and adopted['updated_at'] == original['updated_at']
    assert dl.sweep_recoverable(now=original['updated_at'] + 30) == []
    assert dl.sweep_failed_for_runtime('telegram', now=original['updated_at'] + 184) == []
    claimed = dl.sweep_failed_for_runtime('telegram', now=original['updated_at'] + 186)
    assert len(claimed) == 1 and claimed[0]['needs_marker']
    assert read('boot')['state'] == 'attempting' and read('boot')['last_error'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('error', ['flood_control:60', 'temporary server error'])
async def test_offline_retry_schedule_survives_until_own_profile_reconnects(tmp_path, error):
    """Scheduling is durable-deadline based; delivery alone requires the owning adapter."""
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.base import SendResult
    from gateway.run import GatewayRunner
    from gateway.session import SessionStore

    class Transport:
        def __init__(self):
            self.sent = []

        async def send(self, **payload):
            self.sent.append(payload)
            return SendResult(success=True)

    runner: Any = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.adapters = {}
    runner._profile_adapters = {'secondary': {}}
    runner._primary_profile_name = 'default'
    runner.session_store = SessionStore(tmp_path / 'sessions', runner.config)
    runner._running = True
    primary, secondary = Transport(), Transport()
    for oid, namespace, profile in [('primary', 'main', 'default'), ('secondary', 'secondary', 'secondary')]:
        dl.record_obligation(
            obligation_id=oid, session_key=f'agent:{namespace}:telegram:dm:123',
            platform='telegram', chat_id='123', thread_id=None, content=oid,
            adapter_profile=profile,
        )
        dl.mark_failed(oid, error)

    try:
        await runner._arm_flood_timers_for_waiting_rows()
        assert set(getattr(runner, '_flood_redelivery_tasks', {})) == {
            ('telegram', 'default'), ('telegram', 'secondary'),
        }
        # Stop timer workers before advancing persisted deadlines: manually drive
        # each reconnect below so the test has no wall-clock or concurrent-send race.
        runner._running = False
        timers = list(runner._flood_redelivery_tasks.values())
        for task in timers:
            task.cancel()
        await asyncio.gather(*timers, return_exceptions=True)
        runner._flood_redelivery_tasks.clear()
        runner._flood_redelivery_wakes.clear()

        assert await runner._redeliver_failed_obligations_for_platform(Platform.TELEGRAM) == 0
        assert read('primary')['attempts'] == read('secondary')['attempts'] == 0
        with sqlite3.connect(dl._db_path()) as conn:
            conn.execute('UPDATE delivery_obligations SET updated_at=?', (time.time() - 1000,))
        # A due row with no adapter is released unsent, with its budget intact.
        assert await runner._redeliver_failed_obligations_for_platform(Platform.TELEGRAM) == 0
        assert read('primary')['state'] == 'failed' and read('primary')['attempts'] == 0

        runner.adapters[Platform.TELEGRAM] = primary
        if dl.is_flood_error(error):
            # Releasing an unsent flood claim renews the wait, not the retry count.
            assert await runner._redeliver_failed_obligations_for_platform(Platform.TELEGRAM) == 0
            with sqlite3.connect(dl._db_path()) as conn:
                conn.execute('UPDATE delivery_obligations SET updated_at=? WHERE obligation_id=?',
                             (time.time() - 1000, 'primary'))
        assert await runner._redeliver_failed_obligations_for_platform(Platform.TELEGRAM) == 1
        assert len(primary.sent) == 1 and primary.sent[0]['content'].endswith('primary')
        assert not secondary.sent
        assert read('secondary')['state'] == 'failed' and read('secondary')['attempts'] == 0
        assert await runner._redeliver_failed_obligations_for_platform(Platform.TELEGRAM) == 0

        runner._profile_adapters['secondary'][Platform.TELEGRAM] = secondary
        assert await runner._redeliver_failed_obligations_for_platform(
            Platform.TELEGRAM, profile='secondary') == 1
        assert len(secondary.sent) == 1 and secondary.sent[0]['content'].endswith('secondary')
        assert read('primary')['state'] == read('secondary')['state'] == 'delivered'
        assert read('primary')['attempts'] == read('secondary')['attempts'] == 1
        if dl.is_flood_error(error):
            assert primary.sent[0]['content'].startswith(dl.FLOOD_MARKER)
    finally:
        runner._running = False
        tasks = list(getattr(runner, '_flood_redelivery_tasks', {}).values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        runner.session_store._db.close()
