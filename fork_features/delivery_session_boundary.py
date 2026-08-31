"""Fork-owned session-boundary policy for durable gateway replies."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def retire_session_deliveries(session_key: str) -> int:
    """Retire undelivered replies after a replacement session exists.

    The delivery ledger deliberately keeps a stable platform route across
    ``/new`` and ``/reset``.  Crossing that conversation boundary therefore
    has to terminate the old route's recoverable replies before a later
    gateway restart can deliver them into the new session.

    The ledger write is synchronous SQLite work, so it runs off the event loop.
    This boundary remains best-effort like the rest of final-response delivery:
    a ledger failure must not make the session reset fail.
    """
    if not session_key:
        return 0

    try:
        from gateway.delivery_ledger import supersede_session_obligations

        superseded = int(
            await asyncio.to_thread(supersede_session_obligations, session_key) or 0
        )
    except Exception:
        logger.debug(
            "delivery ledger session-reset boundary update failed for %s",
            session_key,
            exc_info=True,
        )
        return 0

    if superseded:
        logger.info(
            "Superseded %d undelivered response(s) at session reset for %s",
            superseded,
            session_key,
        )
    return superseded
