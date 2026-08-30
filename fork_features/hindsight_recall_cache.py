"""Fork-owned lifecycle for Hindsight's carried recall cache.

The Hindsight provider owns API calls and context formatting. This module owns
only the thread-safe cache/generation state that must survive provider code
changes without allowing a stale turn or session to overwrite newer recall.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class RecallSnapshot:
    query: str
    results: tuple[str, ...]


@dataclass(frozen=True)
class RecallTurn:
    generation: int
    turn_id: str
    result: str
    snapshot: RecallSnapshot | None


class CarryResult(str, Enum):
    APPLIED = "applied"
    STALE_GENERATION = "stale_generation"
    STALE_SESSION = "stale_session"


def render_snapshot(snapshot: RecallSnapshot) -> str:
    return "\n".join(f"- {text}" for text in snapshot.results if text)


def should_sync_cache_miss(
    *,
    sync_enabled: bool,
    memory_mode: str,
    auto_recall: bool,
    shutting_down: bool,
    query: str,
) -> bool:
    return bool(
        sync_enabled
        and memory_mode != "tools"
        and auto_recall
        and not shutting_down
        and str(query or "").strip()
    )


class HindsightRecallCache:
    """One-consumer carried recall state scoped to one Hindsight session."""

    def __init__(self, *, initial_session_id: str = "") -> None:
        self._lock = threading.Lock()
        self._session_id = str(initial_session_id or "").strip()
        self._generation = 0
        self._result = ""
        self._snapshot: RecallSnapshot | None = None
        self._active_turn: tuple[str, int] | None = None

    @property
    def result(self) -> str:
        with self._lock:
            return self._result

    @property
    def session_id(self) -> str:
        with self._lock:
            return self._session_id

    @property
    def snapshot(self) -> RecallSnapshot | None:
        with self._lock:
            return self._snapshot

    def seed(self, snapshot: RecallSnapshot) -> None:
        """Test-fixture preload; production writes must use generation-checked carry."""
        normalized = RecallSnapshot(
            query=str(snapshot.query or ""),
            results=tuple(str(text) for text in snapshot.results),
        )
        with self._lock:
            self._snapshot = normalized
            self._result = render_snapshot(normalized)

    def begin_turn(
        self,
        *,
        requested_session_id: str = "",
        turn_id: str = "",
    ) -> RecallTurn | None:
        requested = str(requested_session_id or "").strip()
        normalized_turn_id = str(turn_id or "").strip()
        with self._lock:
            if requested and self._session_id and requested != self._session_id:
                return None
            self._generation += 1
            generation = self._generation
            self._active_turn = (normalized_turn_id, generation)
            turn = RecallTurn(
                generation=generation,
                turn_id=normalized_turn_id,
                result=self._result,
                snapshot=self._snapshot,
            )
            self._result = ""
            self._snapshot = None
            return turn

    def carry(
        self,
        snapshot: RecallSnapshot,
        *,
        expected_generation: int,
        expected_session_id: str = "",
    ) -> CarryResult:
        normalized = RecallSnapshot(
            query=str(snapshot.query or ""),
            results=tuple(str(text) for text in snapshot.results),
        )
        expected_session = str(expected_session_id or "").strip()
        with self._lock:
            if (
                expected_session
                and self._session_id
                and expected_session != self._session_id
            ):
                return CarryResult.STALE_SESSION
            if expected_generation != self._generation:
                return CarryResult.STALE_GENERATION
            self._snapshot = normalized
            self._result = render_snapshot(normalized)
            return CarryResult.APPLIED

    def invalidate_timeout(
        self,
        *,
        session_id: str = "",
        turn_id: str = "",
    ) -> bool:
        timed_out_session = str(session_id or "").strip()
        timed_out_turn = str(turn_id or "").strip()
        with self._lock:
            if (
                timed_out_session
                and self._session_id
                and timed_out_session != self._session_id
            ):
                return False
            if self._active_turn is None:
                return False
            active_turn_id, active_generation = self._active_turn
            if timed_out_turn and timed_out_turn != active_turn_id:
                return False
            if active_generation != self._generation:
                return False
            self._generation += 1
            self._result = ""
            self._snapshot = None
            self._active_turn = None
            return True

    def switch_session(self, session_id: str) -> None:
        new_session_id = str(session_id or "").strip()
        with self._lock:
            self._generation += 1
            self._session_id = new_session_id
            self._result = ""
            self._snapshot = None
            self._active_turn = None

    def bind_session(self, session_id: str) -> None:
        """Relabel the cache without consuming or invalidating carried recall."""
        with self._lock:
            self._session_id = str(session_id or "").strip()

    def invalidate(self) -> None:
        with self._lock:
            self._generation += 1
            self._result = ""
            self._snapshot = None
            self._active_turn = None
