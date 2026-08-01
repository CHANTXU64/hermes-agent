"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import inspect

from gateway.config import GatewayConfig
from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from gateway.run import GatewayRunner

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"


def test_gateway_runner_freezes_kanban_notifier_profile_at_init(monkeypatch):
    monkeypatch.setattr(
        GatewayRunner,
        "_active_profile_name",
        lambda self: "startup-profile",
    )

    runner = GatewayRunner(GatewayConfig())

    assert runner._kanban_notifier_profile == "startup-profile"


