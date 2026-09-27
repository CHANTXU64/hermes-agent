"""Owned native Messages/Chat Completions calls, without an agent or tool executor."""
from __future__ import annotations

import contextvars
from functools import partial
from typing import Any, Callable


def freeze_anthropic_client_factory(agent: Any) -> Callable[[], Any]:
    """Capture credentials/route now; construct an independent SDK pool in its owner thread.

    Preserve the profile context for adapter TLS/headers and callable token sources.
    Never retain the mutable parent agent or check out its request-client cache.
    """
    from agent.anthropic_adapter import build_anthropic_client

    key = agent._request_anthropic_client_key()
    if key[0] != "direct":
        raise ValueError("Only direct Anthropic Messages SDK clients support request forks")
    build = partial(build_anthropic_client, key[1], key[2], timeout=key[3], drop_context_1m_beta=key[4])
    context = contextvars.copy_context()
    return lambda: context.copy().run(build)


def send_messages(client: Any, body: dict, *, mode: str, progress=None) -> Any:
    """Aggregate native streaming output; tool calls are returned, never dispatched."""
    if mode == "anthropic_messages":
        from agent.anthropic_adapter import create_anthropic_message

        return create_anthropic_message(
            client, body, on_stream_event=(lambda _event: progress()) if progress else None,
        )
    # The SDK accumulator handles split JSON tool arguments and usage-only chunks.
    from openai.lib.streaming.chat import ChatCompletionStreamState

    state = ChatCompletionStreamState()
    kwargs = dict(body)
    kwargs["stream"] = True
    with client.chat.completions.create(**kwargs) as stream:
        for chunk in stream:
            if progress:
                progress()
            state.handle_chunk(chunk)
    return state.get_final_completion()


def build_messages_adopt_rematerializer(
    agent, *, prepared_request, original_messages, current_turn_user_idx, canonicalize_tool_calls,
):
    """Splice only proven new durable rows; keep the native live tail byte-for-byte.

    Locate the actual user anchor, not Anthropic's later user-role tool_result rows.
    Ambiguous/repeated or multimodal anchors fail closed rather than misordering history.
    """
    import copy
    from agent.message_content import flatten_message_text
    from agent.message_sanitization import _sanitize_messages_surrogates
    from fork_features.request_fork import rematerialize_request_after_adopt

    original = copy.deepcopy(original_messages)
    live = original[current_turn_user_idx]
    if not isinstance(live.get("content"), str) or not live["content"].strip():
        return None
    body = prepared_request.clone_body()
    needle = live["content"].strip()
    candidates = [item for item in body["messages"] if item.get("role") == "user"
                  and flatten_message_text(item.get("content"), sep="\n").strip().startswith(needle)]
    if len(candidates) != 1:
        return None
    anchor = copy.deepcopy(candidates[0])
    mode = prepared_request.api_mode
    transport = copy.deepcopy(agent._get_transport())
    base_url = str(getattr(agent, "base_url", "") or "")
    model = str(body.get("model") or "")
    is_oauth = bool(getattr(agent, "_is_anthropic_oauth", False))
    tools = copy.deepcopy(getattr(agent, "tools", None) or [])

    def convert(rows):
        canonicalize_tool_calls(rows)
        _sanitize_messages_surrogates(rows)
        if mode == "anthropic_messages" and is_oauth:
            # Reuse the host's tool aliases/identity handling for ONLY the new rows.
            return transport.build_kwargs(model, rows, tools, is_oauth=True, base_url=base_url)["messages"]
        converted = transport.convert_messages(rows, base_url=base_url, model=model)
        return converted[1] if mode == "anthropic_messages" else converted

    def rebuild(adopted):
        return rematerialize_request_after_adopt(
            prepared_request, original_messages=original, adopted_messages=adopted,
            live_tail_start=current_turn_user_idx, current_user_input=[anchor],
            convert_added_messages=convert,
        )
    return rebuild
