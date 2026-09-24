"""Fork-owned runtime for extra Telegram bots in one Gateway profile.

``GatewayRunner`` keeps generic adapter creation, transport timeouts, status
persistence, session dispatch, and shutdown primitives. This facade owns only
the named-Telegram registry and its account-specific start/fatal/reconnect/stop
policy. It depends on an explicit host protocol and never imports
``gateway.run``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Protocol

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter

from .identity import normalize_account_id


logger = logging.getLogger(__name__)


class TelegramAccountHost(Protocol):
    """Narrow Gateway seam consumed by the Fork runtime facade."""

    config: Any
    session_store: Any
    _busy_text_mode: str
    _running: bool
    _handle_message: Any
    _handle_adapter_fatal_error: Any
    _handle_active_session_busy_message: Any

    def _create_adapter(self, platform: Platform, config: PlatformConfig): ...

    async def _connect_adapter_with_timeout(
        self,
        adapter: BasePlatformAdapter,
        platform: Platform,
        *,
        is_reconnect: bool = False,
    ) -> bool: ...

    async def _safe_adapter_disconnect(
        self, adapter: BasePlatformAdapter, platform: Platform
    ) -> None: ...

    async def _bounded_adapter_teardown(
        self,
        adapter: BasePlatformAdapter,
        platform: Platform,
        *,
        profile: Optional[str] = None,
    ) -> None: ...

    def _sync_voice_mode_state_to_adapter(self, adapter: BasePlatformAdapter) -> None: ...

    def _update_platform_runtime_status(self, platform: str, **kwargs: Any) -> None: ...

    def _ensure_reconnect_watcher_running(self) -> None: ...

    def _make_adapter_auth_check(self, platform: Platform): ...


class TelegramAccountRuntime:
    """Own live and failed named Telegram adapters for one Gateway runner."""

    def __init__(self, host: Any):
        # Runtime duck-typing stays explicit in ``TelegramAccountHost`` above,
        # while accepting concrete/mixin hosts without forcing their large
        # method signatures to be structurally identical for static checkers.
        self.host: TelegramAccountHost = host
        self.live: Dict[str, BasePlatformAdapter] = {}
        self.failed: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _status_key(account_id: str) -> str:
        return f"telegram[{account_id}]"

    def account_id_for(self, adapter: BasePlatformAdapter) -> Optional[str]:
        """Return the named Telegram slot owned by *adapter*, if any."""
        if getattr(adapter, "platform", None) != Platform.TELEGRAM:
            return None
        try:
            config = getattr(adapter, "config", None)
            raw = (getattr(config, "extra", None) or {}).get("account_id")
            return normalize_account_id(raw)
        except Exception:
            return None

    def resolve_stamped_adapter(self, account_id: Any) -> tuple[bool, Any]:
        """Return ``(valid_named_stamp, live_adapter_or_none)``.

        A valid but currently disconnected named account is recognized and
        resolves to ``None`` so host routing fails closed. An invalid stamp keeps
        the host's historical primary fallback behavior.
        """
        account = normalize_account_id(account_id)
        if not account:
            return False, None
        return True, self.live.get(account)

    def adapter_for(self, account_id: Any):
        """Resolve a named account fail-closed; never returns the primary bot."""
        _recognized, adapter = self.resolve_stamped_adapter(account_id)
        return adapter

    def has_live_or_queued(self) -> bool:
        return bool(self.live or self.failed)

    def has_failed(self) -> bool:
        return bool(self.failed)

    def is_stranded(
        self,
        adapter: BasePlatformAdapter,
        *,
        shutdown_requested: bool,
    ) -> bool:
        """Whether a retryable named adapter has neither a live nor queued owner."""
        account = self.account_id_for(adapter)
        return bool(
            account
            and getattr(adapter, "fatal_error_retryable", False)
            and account not in self.live
            and account not in self.failed
            and not shutdown_requested
        )

    def _fallback_account_config(self, account_id: str) -> PlatformConfig:
        primary = self.host.config.platforms.get(Platform.TELEGRAM)
        accounts = ((primary.extra if primary else {}) or {}).get("accounts") or {}
        token = ""
        if isinstance(accounts.get(account_id), dict):
            token = str(accounts[account_id].get("token") or "").strip()
        return PlatformConfig(
            enabled=True,
            token=token,
            extra={"account_id": account_id},
        )

    def queue_retryable(self, adapter: BasePlatformAdapter) -> bool:
        """Queue a named retryable adapter before any potentially wedged close."""
        account = self.account_id_for(adapter)
        if not account or not getattr(adapter, "fatal_error_retryable", False):
            return False
        if account in self.failed:
            return True
        account_config = getattr(adapter, "config", None)
        if account_config is None:
            account_config = self._fallback_account_config(account)
        now = time.monotonic()
        self.failed[account] = {
            "config": account_config,
            "attempts": 0,
            "next_retry": now,
        }
        logger.info("telegram[%s] queued for background reconnection", account)
        self.host._ensure_reconnect_watcher_running()
        return True

    def _configure_adapter(self, adapter: BasePlatformAdapter) -> None:
        adapter.set_message_handler(self.host._handle_message)
        adapter.set_fatal_error_handler(self.host._handle_adapter_fatal_error)
        adapter.set_session_store(self.host.session_store)
        adapter.set_busy_session_handler(
            self.host._handle_active_session_busy_message
        )
        adapter.set_authorization_check(
            self.host._make_adapter_auth_check(Platform.TELEGRAM)
        )
        adapter._busy_text_mode = self.host._busy_text_mode

    @staticmethod
    def _clone_account_config(
        primary: PlatformConfig,
        account_id: str,
        token: str,
    ) -> PlatformConfig:
        extra = dict(primary.extra or {})
        extra.pop("accounts", None)
        extra["account_id"] = account_id
        return PlatformConfig(
            enabled=True,
            token=token,
            api_key=primary.api_key,
            home_channel=primary.home_channel,
            reply_to_mode=primary.reply_to_mode,
            gateway_restart_notification=getattr(
                primary, "gateway_restart_notification", True
            ),
            typing_indicator=getattr(primary, "typing_indicator", True),
            channel_overrides=dict(
                getattr(primary, "channel_overrides", {}) or {}
            ),
            extra=extra,
        )

    async def start(self) -> int:
        """Start every configured named bot without changing the primary slot."""
        primary = self.host.config.platforms.get(Platform.TELEGRAM)
        if not primary or not getattr(primary, "enabled", False):
            return 0
        raw_accounts = (primary.extra or {}).get("accounts") or {}
        if not isinstance(raw_accounts, dict) or not raw_accounts:
            return 0

        connected = 0
        for raw_account, account_info in sorted(raw_accounts.items()):
            account = normalize_account_id(raw_account)
            if not account:
                logger.warning(
                    "Skipping telegram account %r: invalid account id", raw_account
                )
                continue
            token = (
                str(account_info.get("token") or "").strip()
                if isinstance(account_info, dict)
                else str(account_info or "").strip()
            )
            if not token:
                logger.warning("Skipping telegram account %s: empty token", account)
                continue

            account_config = self._clone_account_config(primary, account, token)
            adapter = self.host._create_adapter(Platform.TELEGRAM, account_config)
            if not adapter:
                logger.warning("No Telegram adapter for account '%s'", account)
                continue
            self._configure_adapter(adapter)
            logger.info("Connecting to telegram[%s]...", account)
            try:
                success = await self.host._connect_adapter_with_timeout(
                    adapter, Platform.TELEGRAM
                )
                if success:
                    self.live[account] = adapter
                    self.host._sync_voice_mode_state_to_adapter(adapter)
                    connected += 1
                    logger.info("✓ telegram[%s] connected", account)
                    continue

                logger.warning("✗ telegram[%s] failed to connect", account)
                await self.host._safe_adapter_disconnect(
                    adapter, Platform.TELEGRAM
                )
                retryable = (
                    not getattr(adapter, "has_fatal_error", False)
                    or getattr(adapter, "fatal_error_retryable", False)
                )
                if retryable:
                    self.failed[account] = {
                        "config": account_config,
                        "attempts": 1,
                        "next_retry": time.monotonic() + 30,
                    }
                self.host._update_platform_runtime_status(
                    self._status_key(account),
                    platform_state="retrying" if retryable else "fatal",
                    error_code=getattr(adapter, "fatal_error_code", None),
                    error_message=(
                        getattr(adapter, "fatal_error_message", None)
                        or "failed to connect"
                    ),
                )
            except Exception as exc:
                logger.error(
                    "✗ telegram[%s] error: %s", account, exc, exc_info=True
                )
                await self.host._safe_adapter_disconnect(
                    adapter, Platform.TELEGRAM
                )
                self.failed[account] = {
                    "config": account_config,
                    "attempts": 1,
                    "next_retry": time.monotonic() + 30,
                }
                self.host._update_platform_runtime_status(
                    self._status_key(account),
                    platform_state="retrying",
                    error_code=None,
                    error_message=str(exc),
                )
        return connected

    async def handle_fatal(self, adapter: BasePlatformAdapter) -> bool:
        """Handle one named fatal error; return False for primary/non-Telegram."""
        account = self.account_id_for(adapter)
        if not account:
            return False
        existing = self.live.get(account)
        if existing is not None and existing is not adapter:
            logger.debug(
                "Ignoring stale fatal error from superseded telegram[%s] adapter: %s",
                account,
                getattr(adapter, "fatal_error_code", None) or "unknown",
            )
            return True
        if existing is None and account in self.failed:
            logger.debug(
                "Ignoring duplicate fatal error from already-queued telegram[%s] adapter",
                account,
            )
            return True

        logger.error(
            "Fatal telegram[%s] adapter error (%s): %s",
            account,
            getattr(adapter, "fatal_error_code", None) or "unknown",
            getattr(adapter, "fatal_error_message", None) or "unknown error",
        )
        if getattr(adapter, "fatal_error_code", None) == "relay_disabled":
            state = "disabled"
        elif getattr(adapter, "fatal_error_retryable", False):
            state = "retrying"
        else:
            state = "fatal"
        self.host._update_platform_runtime_status(
            self._status_key(account),
            platform_state=state,
            error_code=getattr(adapter, "fatal_error_code", None),
            error_message=getattr(adapter, "fatal_error_message", None),
        )

        if existing is adapter:
            self.live.pop(account, None)
        # Queue before close so a wedged transport cannot strand this account.
        self.queue_retryable(adapter)
        if existing is adapter:
            await self.host._safe_adapter_disconnect(adapter, Platform.TELEGRAM)
        return True

    async def reconnect_failed(self, *, now: Optional[float] = None) -> None:
        """Run one account-specific reconnect pass."""
        if not self.failed:
            return
        current_time = time.monotonic() if now is None else float(now)
        for account in list(self.failed):
            if not self.host._running:
                return
            info = self.failed.get(account)
            if info is None:
                continue
            if info.get("paused") or current_time < info["next_retry"]:
                continue
            account_config = info["config"]
            attempt = info["attempts"] + 1
            logger.info(
                "Reconnecting telegram[%s] (attempt %d)...", account, attempt
            )
            adapter = None
            try:
                adapter = self.host._create_adapter(
                    Platform.TELEGRAM, account_config
                )
                if not adapter:
                    logger.warning(
                        "Reconnect telegram[%s]: adapter creation returned None",
                        account,
                    )
                    info["attempts"] = attempt
                    info["next_retry"] = current_time + min(
                        300, 30 * (2 ** min(attempt - 1, 4))
                    )
                    continue
                self._configure_adapter(adapter)
                success = await self.host._connect_adapter_with_timeout(
                    adapter, Platform.TELEGRAM, is_reconnect=True
                )
                if success:
                    self.live[account] = adapter
                    self.host._sync_voice_mode_state_to_adapter(adapter)
                    self.failed.pop(account, None)
                    self.host._update_platform_runtime_status(
                        self._status_key(account),
                        platform_state="connected",
                        error_code=None,
                        error_message=None,
                    )
                    logger.info("✓ telegram[%s] reconnected successfully", account)
                    continue

                await self.host._safe_adapter_disconnect(
                    adapter, Platform.TELEGRAM
                )
                if (
                    getattr(adapter, "has_fatal_error", False)
                    and not getattr(adapter, "fatal_error_retryable", False)
                ):
                    self.host._update_platform_runtime_status(
                        self._status_key(account),
                        platform_state="fatal",
                        error_code=getattr(adapter, "fatal_error_code", None),
                        error_message=(
                            getattr(adapter, "fatal_error_message", None)
                            or "failed to reconnect"
                        ),
                    )
                    self.failed.pop(account, None)
                    logger.warning(
                        "Reconnect telegram[%s]: non-retryable error, "
                        "removing from retry queue",
                        account,
                    )
                    continue
                info["attempts"] = attempt
                info["next_retry"] = current_time + min(
                    300, 30 * (2 ** min(attempt - 1, 4))
                )
                self.host._update_platform_runtime_status(
                    self._status_key(account),
                    platform_state="retrying",
                    error_code=getattr(adapter, "fatal_error_code", None),
                    error_message=(
                        getattr(adapter, "fatal_error_message", None)
                        or "failed to reconnect"
                    ),
                )
            except Exception as exc:
                logger.error(
                    "✗ telegram[%s] reconnect error: %s",
                    account,
                    exc,
                    exc_info=True,
                )
                if adapter is not None:
                    await self.host._safe_adapter_disconnect(
                        adapter, Platform.TELEGRAM
                    )
                info["attempts"] = attempt
                info["next_retry"] = current_time + min(
                    300, 30 * (2 ** min(attempt - 1, 4))
                )

    async def stop(self) -> None:
        """Bounded teardown for live named bots and retirement of retry state."""
        for account, adapter in list(self.live.items()):
            await self.host._bounded_adapter_teardown(
                adapter,
                Platform.TELEGRAM,
                profile=f"telegram:{account}",
            )
        self.live.clear()


def replacement_adapter_for(host: Any, adapter: BasePlatformAdapter):
    """Resolve an old transport's slot without crossing a named-Bot boundary.

    In-flight callbacks retain the old adapter after a fatal rebuild. A named
    stamp must use the named runtime even while its replacement is unavailable;
    absence (or an invalid stamp) is never permission to borrow the primary.
    """
    account = (getattr(getattr(adapter, "config", None), "extra", None) or {}).get("account_id")
    if account:
        runtime = getattr(host, "_telegram_accounts", None)
        return runtime.adapter_for(account) if runtime is not None else None
    return (getattr(host, "adapters", None) or {}).get(Platform.TELEGRAM)


def _runtime_for(host: Any) -> TelegramAccountRuntime:
    runtime = host.__dict__.get("_telegram_accounts")
    if not isinstance(runtime, TelegramAccountRuntime):
        runtime = TelegramAccountRuntime(host)
        host.__dict__["_telegram_accounts"] = runtime
    return runtime


def telegram_runtime_property() -> property:
    """Lazy facade for bare ``GatewayRunner`` instances used by host tests."""

    def getter(host: Any):
        return _runtime_for(host)

    def setter(host: Any, value: TelegramAccountRuntime):
        host.__dict__["_telegram_accounts"] = value

    return property(getter, setter)


def telegram_live_adapters_property() -> property:
    """Compatibility view for old tests/callers without duplicating ownership."""

    def getter(host: Any):
        return _runtime_for(host).live

    def setter(host: Any, value: Dict[str, BasePlatformAdapter]):
        _runtime_for(host).live = value

    return property(getter, setter)


def telegram_failed_accounts_property() -> property:
    """Compatibility view for old tests/callers without duplicating ownership."""

    def getter(host: Any):
        return _runtime_for(host).failed

    def setter(host: Any, value: Dict[str, Dict[str, Any]]):
        _runtime_for(host).failed = value

    return property(getter, setter)


__all__ = [
    "TelegramAccountHost",
    "TelegramAccountRuntime",
    "replacement_adapter_for",
    "telegram_failed_accounts_property",
    "telegram_live_adapters_property",
    "telegram_runtime_property",
]
