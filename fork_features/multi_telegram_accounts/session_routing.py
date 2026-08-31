"""Fork-owned cross-Bot session-move policy for Telegram accounts.

The host slash-command flow owns authentication, target resolution, messages,
and its generic conversation-boundary cleanup. This module owns only the
account-suffix comparison that decides switch versus transfer and applies the
same host cleanup funnel to routes detached by a cross-Bot move.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .identity import split_account_session_key


@dataclass(frozen=True)
class CrossAccountResumePlan:
    route_keys: tuple[str, ...]
    blocked_by_running: bool


async def plan_cross_account_resume(
    store: Any,
    *,
    session_key: str,
    target_id: str,
    running_session_keys: Iterable[str],
) -> CrossAccountResumePlan:
    """Find routes owned by other named/primary Telegram accounts."""
    target_routes = await store.routing_keys_for_session_id(target_id)
    current_account = split_account_session_key(session_key)[1]
    route_keys = tuple(
        key
        for key in target_routes
        if key != session_key
        and split_account_session_key(key)[1] != current_account
    )
    running = set(running_session_keys)
    return CrossAccountResumePlan(
        route_keys=route_keys,
        blocked_by_running=any(key in running for key in route_keys),
    )


async def switch_resumed_session(
    store: Any,
    *,
    session_key: str,
    target_id: str,
    plan: CrossAccountResumePlan,
):
    """Transfer across accounts, otherwise use the host's normal switch."""
    if plan.route_keys:
        return await store.transfer_session(session_key, target_id)
    entry = await store.switch_session(session_key, target_id)
    return entry, []


def cleanup_detached_session_routes(host: Any, route_keys: Iterable[str]) -> None:
    """Apply existing host conversation-boundary cleanup to detached routes."""
    for route_key in route_keys:
        release_running = getattr(host, "_release_running_agent_state", None)
        if callable(release_running):
            release_running(route_key)
        clear_scope = getattr(host, "_clear_conversation_scope", None)
        if callable(clear_scope):
            clear_scope(route_key, reason="resume_transfer")
        evict_agent = getattr(host, "_evict_cached_agent", None)
        if callable(evict_agent):
            evict_agent(route_key)
        for attr in (
            "_pending_messages",
            "_pending_native_image_paths_by_session",
        ):
            state = getattr(host, attr, None)
            if isinstance(state, dict):
                state.pop(route_key, None)


__all__ = [
    "CrossAccountResumePlan",
    "cleanup_detached_session_routes",
    "plan_cross_account_resume",
    "switch_resumed_session",
]
