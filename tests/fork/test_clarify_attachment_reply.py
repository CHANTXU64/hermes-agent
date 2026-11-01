"""Contracts for the Fork-owned Clarify attachment-reply policy."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from fork_features.clarify_attachment_reply import (
    ClarifyReplyDisposition,
    attach_clarify_response_context,
    resolve_pending_clarify_reply,
)


def test_empty_audio_transcript_keeps_clarify_pending():
    resolver = Mock(return_value=True)

    disposition = resolve_pending_clarify_reply(
        session_key="telegram:1",
        response_text="",
        response_context="[User sent audio: /cache/voice.ogg]",
        has_audio=True,
        resolve_text_response=resolver,
    )

    assert disposition is ClarifyReplyDisposition.RETAIN_PENDING
    resolver.assert_not_called()


def test_slash_command_with_attachment_bypasses_clarify():
    resolver = Mock(return_value=True)

    disposition = resolve_pending_clarify_reply(
        session_key="telegram:1",
        response_text="/stop",
        response_context="[User sent a file: /cache/doc.pdf]",
        has_audio=False,
        resolve_text_response=resolver,
    )

    assert disposition is ClarifyReplyDisposition.PASS_THROUGH
    resolver.assert_not_called()


def test_empty_non_media_message_passes_through():
    resolver = Mock(return_value=True)

    disposition = resolve_pending_clarify_reply(
        session_key="telegram:1",
        response_text="",
        response_context="",
        has_audio=False,
        resolve_text_response=resolver,
    )

    assert disposition is ClarifyReplyDisposition.PASS_THROUGH
    resolver.assert_not_called()


def test_text_and_media_are_resolved_as_separate_arguments():
    resolver = Mock(return_value=True)
    context = "[User sent a file: /root/.hermes/cache/documents/scope.pdf]"

    disposition = resolve_pending_clarify_reply(
        session_key="telegram:1",
        response_text="2",
        response_context=context,
        has_audio=False,
        resolve_text_response=resolver,
    )

    assert disposition is ClarifyReplyDisposition.RESOLVED
    resolver.assert_called_once_with(
        "telegram:1",
        "2",
        response_context=context,
    )


def test_rejected_choice_reply_continues_as_normal_turn():
    resolver = Mock(return_value=False)

    disposition = resolve_pending_clarify_reply(
        session_key="telegram:1",
        response_text="unrelated prose",
        response_context="",
        has_audio=False,
        resolve_text_response=resolver,
    )

    assert disposition is ClarifyReplyDisposition.PASS_THROUGH


def test_attachment_context_wraps_canonical_response_without_concatenating():
    from tools.clarify_tool import ClarifyResponsePayload

    canonical_multi_select = '["A", "C"]'
    context = "  [User sent a file: /root/.hermes/cache/documents/example.docx]  "

    wrapped = attach_clarify_response_context(canonical_multi_select, context)

    assert isinstance(wrapped, ClarifyResponsePayload)
    assert wrapped.user_response == canonical_multi_select
    assert wrapped.response_context == context.strip()


def test_empty_attachment_context_keeps_plain_response_shape():
    assert attach_clarify_response_context("B", "  ") == "B"


@pytest.mark.asyncio
async def test_gateway_and_clarify_normalizer_use_fork_policy_seams():
    """Real inbound interception, normalization and native-card label must agree.

    This component entry test does not replace the full GatewayRunner busy-route
    suite. Its surrounding media mapper/adapter are local fakes; the interception
    method, pending entry, normalizer and payload wrapper are the real candidate.
    """
    from unittest.mock import AsyncMock
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.session import SessionSource
    from gateway.run_inbound import GatewayInboundMixin
    from tools import clarify_gateway
    from tools.clarify_tool import ClarifyResponsePayload

    class Adapter:
        def __init__(self):
            self.retired_labels = []

        def resume_typing_for_chat(self, chat_id):
            pass

        async def retire_clarify_card(self, clarify_id, label):
            self.retired_labels.append(label)

    adapter = Adapter()
    host = GatewayInboundMixin()
    host._pending_event_audio_paths = lambda event: []
    host._prepare_clarify_reply_text = AsyncMock(return_value="2")
    host._inbound_attachment_display_name = lambda path: ("scope.pdf", "/root/.hermes/cache/documents/scope.pdf")
    setattr(host, "_delivery_adapter_for", lambda source: adapter)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="attachment-test", user_id="user")
    event = MessageEvent(text="2", message_type=MessageType.TEXT, source=source, media_urls=["/host/cache/documents/scope.pdf"])
    entry = clarify_gateway.register("attachment-seam", "attachment-session", "Pick", ["A", "B"])
    try:
        assert await host._hm_clarify_reply(event, source, "attachment-session") == ""
        assert isinstance(entry.response, ClarifyResponsePayload)
        assert entry.response.user_response == "B"
        assert "/root/.hermes/cache/documents/scope.pdf" in entry.response.response_context
        assert entry.event.is_set()
        assert len(adapter.retired_labels) == 1
        assert "B" in adapter.retired_labels[0]
        assert "ClarifyResponsePayload" not in adapter.retired_labels[0]
        assert "response_context" not in adapter.retired_labels[0]
        assert "/root/.hermes" not in adapter.retired_labels[0]
        assert clarify_gateway.attach_clarify_response_context is attach_clarify_response_context
    finally:
        clarify_gateway.resolve_gateway_clarify("attachment-seam", "cleanup")
        clarify_gateway.wait_for_response("attachment-seam", timeout=0.01)
