"""Bounded plugin context at host-owned recovery boundaries.

The host creates/repoints sessions and owns transcript writes. Plugins decide
which task state may follow an explicit recovery transition, never a /new.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)
MAX_CONTEXT_CHARS = 64_000


def recovery_context_message(results: Any, *, max_context_chars: int = MAX_CONTEXT_CHARS):
    """Select one valid plugin result using the compression envelope contract."""
    prepared, accepted_source = None, ""
    for result in results or []:
        if not isinstance(result, dict):
            continue
        source = str(result.get("source") or "").strip()
        context = result.get("context")
        if (not source or len(source) > 64
                or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in source)
                or not isinstance(context, str) or not context.strip()):
            logger.warning("Ignoring invalid persistent recovery context result")
            continue
        context = context.strip()
        if len(context) > max_context_chars:
            logger.warning("Ignoring oversized persistent recovery context from source=%s", source)
            continue
        if prepared is not None:
            logger.warning("Ignoring additional persistent recovery context from source=%s", source)
            continue
        prepared = {
            "role": "user",
            "content": f'<hermes-runtime-context user-authored="false" source="{source}">\n{context}\n</hermes-runtime-context>',
            "display_kind": "hidden",
        }
        accepted_source = source
    return prepared, accepted_source


async def restore_reset_context(store: Any, *, old_session_id: str, new_session_id: str, platform: str) -> bool | None:
    """Recover once after an exhaustion reset; acknowledge only a durable write.

    None means no accepted context (including disabled plugins). False means a
    requested transcript write failed; unacknowledged plugin state remains retryable.
    """
    from hermes_cli.lifecycle import invoke_hook

    payload = dict(session_id=new_session_id, old_session_id=old_session_id,
                   new_session_id=new_session_id, reason="compression_exhausted",
                   platform=platform)
    source, outcome = "", "not_accepted"
    try:
        results = await asyncio.to_thread(invoke_hook, "on_session_reset", max_context_chars=MAX_CONTEXT_CHARS, **payload)
        message, source = recovery_context_message(results)
        if message is None:
            return None
        await store.append_to_transcript(new_session_id, message)
        # SessionStore may queue a failed write and return normally. A durable
        # readback, not the append call, is the acknowledgement boundary.
        persisted = await store.load_transcript(new_session_id)
        if not any(isinstance(row, dict) and row.get("role") == "user"
                   and message["content"] in str(row.get("content", "")) for row in persisted):
            raise OSError("Recovery context not present in durable transcript")
        outcome = "persisted"
        return True
    except Exception:
        outcome = "failed"
        logger.exception("Session recovery context was not persisted for %s", new_session_id)
        return False
    finally:
        try:
            await asyncio.to_thread(invoke_hook, "on_session_reset_complete",
                                    outcome=outcome,
                                    persistent_context_source=source if outcome == "persisted" else "",
                                    **payload)
        except Exception:
            logger.exception("Session recovery acknowledgement failed for %s", new_session_id)
