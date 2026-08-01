"""Regression tests for clarify replies while a gateway session is busy."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource, build_session_key


class _ClarifyBypassAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)

    async def connect(self):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="text")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "private"}


def _event(text="custom answer", **kwargs):
    message_type = kwargs.pop("message_type", MessageType.TEXT)
    return MessageEvent(
        text=text,
        message_type=message_type,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="private",
            user_id="user1",
        ),
        message_id="msg1",
        **kwargs,
    )


def _clear_clarify_state():
    from tools import clarify_gateway as cm

    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


@pytest.mark.asyncio
async def test_active_session_routes_typed_choice_clarify_reply_to_runner_not_busy_queue():
    """Typed text must resolve a pending choice clarify even while the agent is busy.

    Telegram button clarifies keep the adapter session active while the agent
    thread blocks on ``wait_for_response``.  If the adapter only bypasses for
    entries already marked ``awaiting_text``, typed replies to the visible
    multi-choice prompt are handled as busy follow-ups and the clarify wait is
    never resolved.
    """
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _ClarifyBypassAdapter()
    adapter._message_handler = AsyncMock(return_value="")
    adapter._busy_session_handler = AsyncMock(return_value=True)
    event = _event("None of those are valid options")
    session_key = build_session_key(
        event.source,
        group_sessions_per_user=adapter.config.extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=adapter.config.extra.get("thread_sessions_per_user", False),
    )
    adapter._active_sessions[session_key] = asyncio.Event()
    cm.register("clarify-1", session_key, "Pick one", ["A", "B"])

    await adapter.handle_message(event)

    adapter._message_handler.assert_awaited_once_with(event)
    adapter._busy_session_handler.assert_not_awaited()
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_text", ["请用这个附件继续处理", ""])
async def test_gateway_clarify_reply_preserves_document_path(reply_text):
    """A document sent as the clarify answer must reach the blocked agent."""
    _clear_clarify_state()
    from gateway.run import GatewayRunner
    from tools import clarify_gateway as cm
    from tools.clarify_tool import ClarifyResponsePayload

    document_path = "/Users/robot/.hermes/cache/documents/doc_123_情况说明.docx"
    agent_path = "/root/.hermes/cache/documents/doc_123_情况说明.docx"
    event = _event(
        reply_text,
        message_type=MessageType.DOCUMENT,
        media_urls=[document_path],
        media_types=[
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ],
    )
    adapter = _ClarifyBypassAdapter()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._startup_restore_in_progress = False
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized = lambda source: True
    runner._session_key_for_source = lambda source: "clarify-document-session"
    runner._adapter_for_source = lambda source: adapter
    runner._update_prompt_pending = {}

    cm.register(
        "clarify-document",
        "clarify-document-session",
        "请发送需要处理的文件",
        None,
    )

    with (
        patch(
            "tools.credential_files.to_agent_visible_cache_path",
            return_value=agent_path,
        ) as to_agent_path,
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = await runner._handle_message(event)

    answer = cm.wait_for_response("clarify-document", timeout=0.1)
    assert result == ""
    assert isinstance(answer, ClarifyResponsePayload)
    assert answer.user_response == reply_text
    assert agent_path in answer.response_context
    to_agent_path.assert_called_once_with(document_path)


@pytest.mark.asyncio
async def test_gateway_clarify_choice_with_document_keeps_canonical_choice():
    """Attachment context must not change typed choice normalization."""
    _clear_clarify_state()
    from gateway.run import GatewayRunner
    from tools import clarify_gateway as cm
    from tools.clarify_tool import ClarifyResponsePayload

    document_path = "/Users/robot/.hermes/cache/documents/doc_456_scope.pdf"
    agent_path = "/root/.hermes/cache/documents/doc_456_scope.pdf"
    event = _event(
        "2",
        message_type=MessageType.DOCUMENT,
        media_urls=[document_path],
        media_types=["application/pdf"],
    )
    adapter = _ClarifyBypassAdapter()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._startup_restore_in_progress = False
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized = lambda source: True
    runner._session_key_for_source = lambda source: "clarify-choice-session"
    runner._adapter_for_source = lambda source: adapter
    runner._update_prompt_pending = {}

    cm.register(
        "clarify-choice",
        "clarify-choice-session",
        "请选择处理方式",
        ["只查看", "继续处理"],
    )

    with (
        patch(
            "tools.credential_files.to_agent_visible_cache_path",
            return_value=agent_path,
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = await runner._handle_message(event)

    answer = cm.wait_for_response("clarify-choice", timeout=0.1)
    assert result == ""
    assert isinstance(answer, ClarifyResponsePayload)
    assert answer.user_response == "继续处理"
    assert agent_path in answer.response_context
