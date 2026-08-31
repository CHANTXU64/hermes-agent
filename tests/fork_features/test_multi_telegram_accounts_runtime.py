from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from fork_features.multi_telegram_accounts.runtime import TelegramAccountRuntime
from gateway.config import GatewayConfig, Platform, PlatformConfig


class FakeAdapter:
    def __init__(self, config: PlatformConfig):
        self.platform = Platform.TELEGRAM
        self.config = config
        self.has_fatal_error = False
        self.fatal_error_retryable = True
        self.fatal_error_code = None
        self.fatal_error_message = None
        self.wiring: dict[str, object] = {}

    def set_message_handler(self, value):
        self.wiring["message"] = value

    def set_fatal_error_handler(self, value):
        self.wiring["fatal"] = value

    def set_session_store(self, value):
        self.wiring["session_store"] = value

    def set_busy_session_handler(self, value):
        self.wiring["busy"] = value

    def set_authorization_check(self, value):
        self.wiring["auth"] = value


class FakeHost:
    def __init__(self, accounts: dict[str, dict[str, str]]):
        primary = PlatformConfig(
            enabled=True,
            token="primary-token",
            api_key="api-key",
            home_channel=None,
            reply_to_mode="off",
            extra={"accounts": accounts, "shared": "kept"},
        )
        self.config = GatewayConfig(platforms={Platform.TELEGRAM: primary})
        self.session_store = object()
        self._busy_text_mode = "interrupt"
        self._running = True
        self.created: list[FakeAdapter] = []
        self.connect_outcomes: dict[str, list[object]] = {}
        self.events: list[object] = []
        self.statuses: list[tuple[str, dict]] = []
        self.watcher_ensured = 0
        self.runtime: TelegramAccountRuntime | None = None
        self._handle_message = object()
        self._handle_adapter_fatal_error = object()
        self._handle_active_session_busy_message = object()

    def _create_adapter(self, platform, config):
        assert platform == Platform.TELEGRAM
        adapter = FakeAdapter(config)
        self.created.append(adapter)
        return adapter

    async def _connect_adapter_with_timeout(self, adapter, platform, *, is_reconnect=False):
        assert platform == Platform.TELEGRAM
        account_id = adapter.config.extra["account_id"]
        result = self.connect_outcomes.get(account_id, [True]).pop(0)
        self.events.append(("connect", account_id, is_reconnect))
        if isinstance(result, BaseException):
            raise result
        return bool(result)

    async def _safe_adapter_disconnect(self, adapter, platform):
        account_id = adapter.config.extra["account_id"]
        queued = bool(self.runtime and account_id in self.runtime.failed)
        self.events.append(("disconnect", account_id, queued))

    async def _bounded_adapter_teardown(self, adapter, platform, *, profile=None):
        self.events.append(("teardown", adapter.config.extra["account_id"], profile))

    def _sync_voice_mode_state_to_adapter(self, adapter):
        self.events.append(("voice", adapter.config.extra["account_id"]))

    def _update_platform_runtime_status(self, platform, **kwargs):
        self.statuses.append((platform, kwargs))

    def _ensure_reconnect_watcher_running(self):
        self.watcher_ensured += 1

    def _make_adapter_auth_check(self, platform):
        return ("auth", platform)


def _runtime(accounts: dict[str, dict[str, str]]) -> tuple[FakeHost, TelegramAccountRuntime]:
    host = FakeHost(accounts)
    runtime = TelegramAccountRuntime(host)
    host.runtime = runtime
    return host, runtime


@pytest.mark.asyncio
async def test_start_keeps_account_configs_isolated_and_queues_retryable_failure() -> None:
    host, runtime = _runtime(
        {
            "work": {"token": "token-work"},
            "home": {"token": "token-home"},
        }
    )
    host.connect_outcomes = {"home": [False], "work": [True]}

    connected = await runtime.start()

    assert connected == 1
    assert set(runtime.live) == {"work"}
    assert set(runtime.failed) == {"home"}
    by_id = {adapter.config.extra["account_id"]: adapter for adapter in host.created}
    assert by_id["work"].config.token == "token-work"
    assert by_id["home"].config.token == "token-home"
    assert "accounts" not in by_id["work"].config.extra
    assert by_id["work"].config.extra["shared"] == "kept"
    assert by_id["work"].wiring == {
        "message": host._handle_message,
        "fatal": host._handle_adapter_fatal_error,
        "session_store": host.session_store,
        "busy": host._handle_active_session_busy_message,
        "auth": ("auth", Platform.TELEGRAM),
    }
    assert runtime.adapter_for(" WORK ") is by_id["work"]
    assert runtime.adapter_for("missing") is None
    assert runtime.has_live_or_queued() is True
    assert runtime.has_failed() is True


@pytest.mark.asyncio
async def test_retryable_fatal_is_queued_before_disconnect_and_never_touches_primary_slot() -> None:
    host, runtime = _runtime({"work": {"token": "token-work"}})
    host.connect_outcomes = {"work": [True]}
    await runtime.start()
    adapter = runtime.live["work"]
    adapter.fatal_error_retryable = True
    adapter.fatal_error_code = "network"
    adapter.fatal_error_message = "temporary"

    assert await runtime.handle_fatal(adapter) is True

    assert "work" not in runtime.live
    assert runtime.failed["work"]["config"] is adapter.config
    assert ("disconnect", "work", True) in host.events
    assert host.watcher_ensured == 1
    assert runtime.is_stranded(adapter, shutdown_requested=False) is False


@pytest.mark.asyncio
async def test_stale_fatal_does_not_replace_new_live_adapter() -> None:
    host, runtime = _runtime({"work": {"token": "token-work"}})
    stale = FakeAdapter(
        PlatformConfig(enabled=True, token="old", extra={"account_id": "work"})
    )
    current = FakeAdapter(
        PlatformConfig(enabled=True, token="new", extra={"account_id": "work"})
    )
    runtime.live["work"] = current

    assert await runtime.handle_fatal(stale) is True

    assert runtime.live["work"] is current
    assert runtime.failed == {}
    assert not [event for event in host.events if event[0] == "disconnect"]


@pytest.mark.asyncio
async def test_reconnect_uses_failed_accounts_own_config_and_restores_live_slot() -> None:
    host, runtime = _runtime({"work": {"token": "token-work"}})
    account_cfg = PlatformConfig(
        enabled=True,
        token="token-work",
        extra={"account_id": "work", "shared": "kept"},
    )
    runtime.failed["work"] = {
        "config": account_cfg,
        "attempts": 0,
        "next_retry": 0.0,
    }
    host.connect_outcomes = {"work": [True]}

    await runtime.reconnect_failed(now=100.0)

    assert runtime.failed == {}
    assert runtime.live["work"].config is account_cfg
    assert ("connect", "work", True) in host.events
    assert host.statuses[-1] == (
        "telegram[work]",
        {
            "platform_state": "connected",
            "error_code": None,
            "error_message": None,
        },
    )


@pytest.mark.asyncio
async def test_stop_tears_down_every_live_account_and_preserves_retry_queue() -> None:
    host, runtime = _runtime({})
    runtime.live.update(
        {
            "home": FakeAdapter(
                PlatformConfig(enabled=True, token="home", extra={"account_id": "home"})
            ),
            "work": FakeAdapter(
                PlatformConfig(enabled=True, token="work", extra={"account_id": "work"})
            ),
        }
    )
    runtime.failed["queued"] = {
        "config": PlatformConfig(
            enabled=True, token="queued", extra={"account_id": "queued"}
        ),
        "attempts": 1,
        "next_retry": 30.0,
    }

    await runtime.stop()

    assert runtime.live == {}
    assert set(runtime.failed) == {"queued"}
    assert {event[1] for event in host.events if event[0] == "teardown"} == {
        "home",
        "work",
    }
