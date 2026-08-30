"""Contracts for the Fork-owned Clarify attachment-reply policy."""

from __future__ import annotations

from unittest.mock import Mock

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


def test_gateway_and_clarify_normalizer_use_fork_policy_seams():
    import gateway.run as gateway_run
    import tools.clarify_gateway as clarify_gateway

    assert gateway_run.resolve_pending_clarify_reply is resolve_pending_clarify_reply
    assert (
        clarify_gateway.attach_clarify_response_context
        is attach_clarify_response_context
    )
