"""Request-local Fork of one frozen provider-native Codex request.

The service is available to plugins only while Hermes emits
``on_compression_start``. Captured forks keep an immutable request prefix and
final tool schemas, call the current Codex Responses transport directly, and
never write to the parent transcript or execute returned tool calls.
"""

from __future__ import annotations

import contextvars
import copy
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RequestForkResult:
    raw_output: str
    tool_calls: tuple[Any, ...] = ()
    usage: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, init=False)
class FrozenCodexRequest:
    """Mutation-safe provider-native request value captured by the host.

    The body contains only kwargs already prepared for the Codex Responses
    transport.  Runtime objects (agent/client/locks/callbacks/SessionDB) are
    deliberately excluded and are created independently when the Fork sends.
    """

    fidelity: str
    captured_session_id: str
    _body: dict[str, Any] = field(repr=False)

    def __init__(
        self,
        *,
        body: Mapping[str, Any],
        fidelity: str,
        captured_session_id: str = "",
    ) -> None:
        frozen_body = copy.deepcopy(dict(body))
        request_input = frozen_body.get("input")
        if not isinstance(request_input, list):
            raise ValueError("Frozen Codex request requires a list input")
        fidelity_text = str(fidelity or "").strip()
        if not fidelity_text:
            raise ValueError("Frozen Codex request requires fidelity")
        object.__setattr__(self, "fidelity", fidelity_text)
        object.__setattr__(self, "captured_session_id", str(captured_session_id or ""))
        object.__setattr__(self, "_body", frozen_body)

    def clone_body(self) -> dict[str, Any]:
        """Return a fresh mutable body for exactly one transport send."""

        return copy.deepcopy(self._body)


def _usage_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
            return dict(dumped) if isinstance(dumped, Mapping) else {}
        except Exception:
            return {}
    try:
        return {
            key: item
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    except Exception:
        return {}


def _log_request_fork_usage(
    *,
    request_id: str,
    usage: Mapping[str, Any],
    provider: str,
) -> None:
    if not usage:
        logger.warning(
            "request Fork usage request_id=%s usage_unavailable=true",
            request_id,
        )
        return
    try:
        from agent.usage_pricing import normalize_usage

        canonical = normalize_usage(
            usage,
            provider=provider,
            api_mode="codex_responses",
        )
        prompt_tokens = canonical.prompt_tokens
        cache_hit_rate = (
            canonical.cache_read_tokens / prompt_tokens * 100
            if prompt_tokens
            else 0.0
        )
        logger.warning(
            "request Fork usage request_id=%s prompt_tokens=%s "
            "uncached_input_tokens=%s cache_read_tokens=%s "
            "cache_write_tokens=%s cache_hit_rate=%.2f%%",
            request_id,
            prompt_tokens,
            canonical.input_tokens,
            canonical.cache_read_tokens,
            canonical.cache_write_tokens,
            cache_hit_rate,
        )
    except Exception:
        logger.debug(
            "request Fork usage logging failed request_id=%s",
            request_id,
            exc_info=True,
        )


@dataclass(frozen=True)
class _RequestForkTemplate:
    frozen_request: FrozenCodexRequest
    client_factory: Callable[[], Any]
    client_close: Callable[[Any], None]
    normalize_response: Callable[[Any], Any]
    abort_client: Callable[[Any, str], None] | None
    provider: str
    max_attempts: int
    context_length: int
    progress_callback: Callable[[], None] | None = None


@dataclass(frozen=True)
class _FrozenClientOwner:
    provider: str
    log_context: str

    @staticmethod
    def _build_keepalive_http_client(
        base_url: str = "",
        *,
        verify: Any = True,
    ) -> Any:
        from agent.process_bootstrap import build_keepalive_http_client

        return build_keepalive_http_client(base_url, verify=verify)

    def _client_log_context(self) -> str:
        return self.log_context


_CURRENT_TEMPLATE: contextvars.ContextVar[_RequestForkTemplate | None] = (
    contextvars.ContextVar("hermes_current_request_fork", default=None)
)


class CurrentRequestFork:
    """Frozen provider-native request that can be called after the hook returns."""

    def __init__(self, template: _RequestForkTemplate) -> None:
        self._template = template

    def call(
        self,
        *,
        append_message: Mapping[str, Any],
        request_id: str,
    ) -> RequestForkResult:
        from agent.codex_runtime import run_codex_stream
        from agent.error_classifier import FailoverReason, classify_api_error
        from agent.retry_utils import jittered_backoff

        retryable_reasons = {
            FailoverReason.timeout,
            FailoverReason.rate_limit,
            FailoverReason.upstream_rate_limit,
            FailoverReason.overloaded,
            FailoverReason.server_error,
            FailoverReason.unknown,
        }
        max_attempts = max(1, int(self._template.max_attempts))
        for attempt in range(1, max_attempts + 1):
            api_kwargs = self._template.frozen_request.clone_body()
            request_input = api_kwargs.get("input")
            if not isinstance(request_input, list):
                raise RuntimeError("Frozen Codex request input is unavailable")
            request_input.append(copy.deepcopy(dict(append_message)))
            runtime = _ForkCodexRuntime(
                template=self._template,
                request_id=str(request_id or "request-fork"),
                model=str(api_kwargs.get("model") or ""),
            )
            owned_client = self._template.client_factory()
            try:
                response = run_codex_stream(runtime, api_kwargs, client=owned_client)
                normalized = self._template.normalize_response(response)
                content = getattr(normalized, "content", None)
                tool_calls = getattr(normalized, "tool_calls", None)
                usage = _usage_mapping(getattr(normalized, "usage", None))
                if not usage:
                    usage = _usage_mapping(getattr(response, "usage", None))
                _log_request_fork_usage(
                    request_id=str(request_id or "request-fork"),
                    usage=usage,
                    provider=self._template.provider,
                )
                return RequestForkResult(
                    raw_output=(
                        content if isinstance(content, str) else str(content or "")
                    ),
                    tool_calls=tuple(tool_calls or ()),
                    usage=usage,
                )
            except Exception as exc:
                classified = classify_api_error(
                    exc,
                    provider=self._template.provider,
                    model=str(api_kwargs.get("model") or ""),
                    approx_tokens=0,
                    context_length=self._template.context_length,
                    num_messages=len(request_input),
                )
                if (
                    attempt >= max_attempts
                    or not classified.retryable
                    or classified.reason not in retryable_reasons
                ):
                    raise
                wait_time = jittered_backoff(
                    attempt,
                    base_delay=2.0,
                    max_delay=60.0,
                )
                logger.warning(
                    "request Fork API call failed; retrying in %.2fs "
                    "(attempt %s/%s) provider=%s model=%s reason=%s error_type=%s",
                    wait_time,
                    attempt,
                    max_attempts,
                    self._template.provider or "unknown",
                    str(api_kwargs.get("model") or "unknown"),
                    classified.reason.value,
                    type(exc).__name__,
                )
                time.sleep(wait_time)
            finally:
                self._template.client_close(owned_client)

        raise RuntimeError("request Fork attempt loop exited without a result")


class _ForkCodexRuntime:
    """Minimal isolated mutable runtime required by ``run_codex_stream``."""

    def __init__(
        self,
        *,
        template: _RequestForkTemplate,
        request_id: str,
        model: str,
    ) -> None:
        import threading

        self.model = model
        self.provider = template.provider
        self.session_id = template.frozen_request.captured_session_id
        self.is_subagent = False
        self._fallback_index = 0
        self._current_api_request_id = request_id
        self._interrupt_requested = False
        self._codex_streamed_text_parts: list[str] = []
        self._codex_stream_last_event_ts = 0.0
        self._stream_writer_generation = 0
        self._stream_writer_lock = threading.Lock()
        self.interim_assistant_callback = None
        self.show_commentary = False
        self._progress_callback = template.progress_callback
        self._abort_client = template.abort_client

    def _progress(self, *_args: Any, **_kwargs: Any) -> None:
        if self._progress_callback is not None:
            self._progress_callback()

    _fire_stream_delta = _progress
    _fire_reasoning_delta = _progress
    _touch_activity = _progress

    def _fire_streamed_codex_commentary(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def _ensure_primary_openai_client(self, *, reason: str) -> Any:
        raise RuntimeError(
            f"request Fork must pass its owned client explicitly (reason={reason})"
        )

    def _client_log_context(self) -> str:
        return f"provider={self.provider or 'unknown'} model={self.model or 'unknown'} request_fork=true"

    def _abort_request_openai_client(self, client: Any, *, reason: str) -> None:
        if self._abort_client is not None:
            self._abort_client(client, reason)


class RequestForkService:
    """Plugin-facing access to the host's current compression request Fork."""

    def capture_current(self) -> CurrentRequestFork:
        template = _CURRENT_TEMPLATE.get()
        if template is None:
            raise RuntimeError(
                "capture_current() is available only inside on_compression_start"
            )
        return CurrentRequestFork(template)


def compression_request_fork_enabled(agent: Any) -> bool:
    """Return whether an interactive main Codex request needs an exact snapshot."""
    if getattr(agent, "is_subagent", False):
        return False
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False
    platform = str(getattr(agent, "platform", "") or "").strip().lower()
    if platform in {"cron", "subagent", "background_review", "background"}:
        return False
    api_mode = str(
        getattr(agent, "api_mode", "") or "chat_completions"
    ).strip().lower()
    if api_mode != "codex_responses":
        return False
    try:
        from hermes_cli.lifecycle import has_hook

        return has_hook("on_compression_start")
    except Exception:
        return False


def freeze_codex_request_for_compression(
    agent: Any,
    body: Mapping[str, Any],
    *,
    fidelity: str,
) -> FrozenCodexRequest | None:
    """Freeze a prepared Codex request only when a real consumer is active."""

    if not compression_request_fork_enabled(agent):
        return None
    canonical_body = copy.deepcopy(dict(body))
    extra_body = canonical_body.get("extra_body")
    if isinstance(extra_body, Mapping):
        remaining_extra_body = dict(extra_body)
        for field_name in ("input", "tools"):
            if field_name in remaining_extra_body:
                canonical_body[field_name] = remaining_extra_body.pop(field_name)
        if remaining_extra_body:
            canonical_body["extra_body"] = remaining_extra_body
        else:
            canonical_body.pop("extra_body", None)
    return FrozenCodexRequest(
        body=canonical_body,
        fidelity=fidelity,
        captured_session_id=str(getattr(agent, "session_id", "") or ""),
    )


def _comparison_message(message: Mapping[str, Any]) -> dict[str, Any]:
    comparable = copy.deepcopy(dict(message))
    for key in (
        "_row_id",
        "_db_persisted",
        "display_kind",
        "display_metadata",
    ):
        comparable.pop(key, None)
    return comparable


def _contains_image_content(messages: Sequence[Mapping[str, Any]]) -> bool:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, Mapping):
                continue
            if str(part.get("type") or "").strip().lower() in {
                "image",
                "image_url",
                "input_image",
            }:
                return True
    return False


def rematerialize_codex_request_after_adopt(
    prepared_request: FrozenCodexRequest,
    *,
    original_messages: Sequence[Mapping[str, Any]],
    adopted_messages: Sequence[Mapping[str, Any]],
    live_tail_start: int | None = None,
    current_user_input: Sequence[Mapping[str, Any]],
    convert_added_messages: Callable[
        [list[dict[str, Any]]], Sequence[Mapping[str, Any]]
    ],
) -> FrozenCodexRequest | None:
    """Splice a proven append-before-live-tail adoption into a prepared body.

    The already-prepared body remains the authority for request-only context,
    tool schemas, cache identity, headers, and every provider-specific field.
    Only durable rows proven to have appeared immediately before the original
    live tail are converted and inserted. Ambiguous history changes and new
    multimodal rows fail closed instead of rerunning the host request pipeline.
    """

    original = list(original_messages)
    adopted = list(adopted_messages)
    if not original or len(adopted) < len(original):
        return None
    prefix_length = (
        len(original) - 1
        if live_tail_start is None
        else int(live_tail_start)
    )
    if prefix_length < 0 or prefix_length >= len(original):
        return None
    tail_length = len(original) - prefix_length
    if [
        _comparison_message(message) for message in adopted[:prefix_length]
    ] != [
        _comparison_message(message) for message in original[:prefix_length]
    ]:
        return None
    if [
        _comparison_message(message) for message in adopted[-tail_length:]
    ] != [
        _comparison_message(message) for message in original[prefix_length:]
    ]:
        return None

    added_rows = adopted[prefix_length:-tail_length]
    if _contains_image_content(added_rows):
        return None
    prepared_added: list[dict[str, Any]] = []
    for message in added_rows:
        item = _comparison_message(message)
        item.pop("api_content", None)
        item.pop("_request_only_api_content", None)
        item.pop("finish_reason", None)
        item.pop("_length_continuation_fragment", None)
        item.pop("_length_continuation_nudge", None)
        prepared_added.append(item)
    try:
        converted_added = [
            copy.deepcopy(dict(item))
            for item in convert_added_messages(prepared_added)
            if isinstance(item, Mapping)
        ]
    except Exception:
        return None

    body = prepared_request.clone_body()
    request_input = body.get("input")
    anchor = [copy.deepcopy(dict(item)) for item in current_user_input]
    if not isinstance(request_input, list) or not anchor:
        return None
    anchor_index = None
    for index in range(len(request_input) - len(anchor), -1, -1):
        if request_input[index : index + len(anchor)] == anchor:
            anchor_index = index
            break
    if anchor_index is None:
        return None
    body["input"] = [
        *request_input[:anchor_index],
        *converted_added,
        *request_input[anchor_index:],
    ]
    return FrozenCodexRequest(
        body=body,
        fidelity="rematerialized_after_adopt",
        captured_session_id=prepared_request.captured_session_id,
    )


def materialize_codex_request_for_compression(
    agent: Any,
    messages: Sequence[Mapping[str, Any]],
    *,
    fidelity: str,
    system_prompt: str | None = None,
    tools: Sequence[Mapping[str, Any]] | None = None,
) -> FrozenCodexRequest | None:
    """Build a provider-native request when no prepared parent exists.

    This narrow reconstruction path is only for out-of-turn manual compression.
    Automatic compression and provider errors freeze a request body already
    prepared by the conversation loop; durable adoption purely splices proven
    new rows into that frozen body.
    """

    if not compression_request_fork_enabled(agent):
        return None
    if _contains_image_content(messages):
        return None
    request_messages: list[Mapping[str, Any]] = []
    resolved_system_prompt = (
        str(system_prompt)
        if system_prompt is not None
        else str(getattr(agent, "_cached_system_prompt", "") or "")
    )
    if resolved_system_prompt:
        request_messages.append(
            {"role": "system", "content": resolved_system_prompt}
        )
    request_messages.extend(copy.deepcopy(list(messages)))
    tools_for_api = copy.deepcopy(
        list(tools if tools is not None else (getattr(agent, "tools", None) or []))
    )
    body = agent._build_api_kwargs(
        request_messages,
        tools_for_api=tools_for_api,
    )
    from agent.message_sanitization import (
        _sanitize_structure_non_ascii,
        _sanitize_structure_surrogates,
    )

    _sanitize_structure_surrogates(body)
    if bool(getattr(agent, "_force_ascii_payload", False)):
        _sanitize_structure_non_ascii(body)
    body = agent._get_transport().preflight_kwargs(
        body,
        allow_stream=False,
        is_github_responses=agent._is_copilot_url(),
        sanitize_harmony_tokens=agent._is_codex_backend(),
    )
    return FrozenCodexRequest(
        body=body,
        fidelity=fidelity,
        captured_session_id=str(getattr(agent, "session_id", "") or ""),
    )


def build_out_of_turn_compression_request_snapshot(
    agent: Any,
    messages: Sequence[Mapping[str, Any]],
) -> FrozenCodexRequest | None:
    """Reconstruct a truthful provider-native manual-compression request."""

    return materialize_codex_request_for_compression(
        agent,
        messages,
        fidelity="reconstructed_out_of_turn",
    )


@contextmanager
def current_request_fork_scope(
    agent: Any,
    *,
    frozen_request: FrozenCodexRequest,
    progress_callback: Callable[[], None] | None = None,
    client_factory: Callable[[], Any] | None = None,
    client_close: Callable[[Any], None] | None = None,
    normalize_response: Callable[[Any], Any] | None = None,
    abort_client: Callable[[Any, str], None] | None = None,
) -> Iterator[None]:
    """Expose one mutation-safe provider-native request during a hook call."""
    client_kwargs = copy.deepcopy(dict(getattr(agent, "_client_kwargs", {}) or {}))
    provider = str(getattr(agent, "provider", "") or "")
    log_context_fn = getattr(agent, "_client_log_context", None)
    log_context = (
        str(log_context_fn())
        if callable(log_context_fn)
        else f"provider={provider or 'unknown'} request_fork=true"
    )
    owner = _FrozenClientOwner(provider=provider, log_context=log_context)
    try:
        max_attempts = max(1, int(getattr(agent, "_api_max_retries", 3)))
    except (TypeError, ValueError):
        max_attempts = 3
    compressor = getattr(agent, "context_compressor", None)
    try:
        context_length = max(
            1,
            int(getattr(compressor, "context_length", 200000)),
        )
    except (TypeError, ValueError):
        context_length = 200000

    if client_factory is None:

        def _create_owned_client() -> Any:
            from agent.agent_runtime_helpers import create_openai_client

            return create_openai_client(
                owner,
                copy.deepcopy(client_kwargs),
                reason="request_fork",
                shared=False,
            )

        client_factory = _create_owned_client

    if client_close is None:

        def _close_owned_client(client: Any) -> None:
            if client is None:
                return
            from agent.agent_runtime_helpers import force_close_tcp_sockets

            force_close_tcp_sockets(client)
            try:
                client.close()
            except Exception:
                logger.debug("request Fork client close failed", exc_info=True)

        client_close = _close_owned_client

    if abort_client is None:

        def _abort_owned_client(client: Any, _reason: str) -> None:
            from agent.agent_runtime_helpers import force_close_tcp_sockets

            force_close_tcp_sockets(client)

        abort_client = _abort_owned_client

    if normalize_response is None:
        transport = type(agent._get_transport())()
        normalize_response = transport.normalize_response
    if not callable(client_factory) or not callable(client_close):
        raise RuntimeError("request Fork client ownership callbacks are unavailable")
    if not callable(normalize_response):
        raise RuntimeError("request Fork response normalizer is unavailable")

    template = _RequestForkTemplate(
        frozen_request=frozen_request,
        client_factory=client_factory,
        client_close=client_close,
        normalize_response=normalize_response,
        abort_client=abort_client,
        provider=provider,
        max_attempts=max_attempts,
        context_length=context_length,
        progress_callback=progress_callback,
    )
    token = _CURRENT_TEMPLATE.set(template)
    try:
        yield
    finally:
        _CURRENT_TEMPLATE.reset(token)
