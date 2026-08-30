"""Fork-owned policy for Clarify replies that carry attachments.

Gateway Core owns authorization, command ordering, audio transcription, media
placeholder construction, logging, and typing indicators. ``clarify_gateway``
owns pending-entry lookup and choice normalization. This module owns only the
Fork rules that keep attachment context separate from the canonical answer and
decide whether a prepared inbound event resolves, bypasses, or leaves a Clarify
pending.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable


class ClarifyReplyDisposition(Enum):
    """Result of applying the pending-Clarify reply policy."""

    PASS_THROUGH = "pass_through"
    RETAIN_PENDING = "retain_pending"
    RESOLVED = "resolved"


def attach_clarify_response_context(
    user_response: Any,
    response_context: Any,
) -> Any:
    """Wrap a canonical response only when separate attachment context exists."""
    normalized_context = str(response_context or "").strip()
    if not normalized_context:
        return user_response

    from tools.clarify_tool import ClarifyResponsePayload

    return ClarifyResponsePayload(
        user_response=user_response,
        response_context=normalized_context,
    )


def resolve_pending_clarify_reply(
    *,
    session_key: str,
    response_text: Any,
    response_context: Any,
    has_audio: bool,
    resolve_text_response: Callable[..., bool],
) -> ClarifyReplyDisposition:
    """Apply Fork reply rules to already-prepared Gateway inputs.

    An audio event with no usable transcript leaves the Clarify pending even
    though its media placeholder is non-empty. Slash commands bypass Clarify.
    Otherwise text and attachment context are passed separately so the Clarify
    normalizer can canonicalize numeric, label, and multi-select answers before
    :func:`attach_clarify_response_context` wraps the result.
    """
    text = str(response_text or "").strip()
    context = str(response_context or "").strip()

    if has_audio and not text:
        return ClarifyReplyDisposition.RETAIN_PENDING
    if (not text and not context) or text.startswith("/"):
        return ClarifyReplyDisposition.PASS_THROUGH

    resolved = resolve_text_response(
        session_key,
        text,
        response_context=context,
    )
    if resolved:
        return ClarifyReplyDisposition.RESOLVED
    return ClarifyReplyDisposition.PASS_THROUGH
