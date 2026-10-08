"""Small, backend-neutral service-level evidence for observer hooks.

Only explicit provider fields are evidence; never infer service levels from the
agent's configured mode or model name. Keep this independent of content capture.
"""
from typing import Any


def field(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def service_parameters(value: Any, *, request: bool = False) -> dict:
    result = {}
    extra = field(value, "extra_body") if request else field(value, "usage")
    for key in ("service_tier", "speed"):
        raw = field(value, key)
        if request and isinstance(extra, dict) and key in extra:
            raw = extra[key]  # SDK extra_body overrides a top-level argument.
        elif not request and raw is None:
            raw = field(extra, key)  # Anthropic reports speed in usage.
        if isinstance(raw, str) and raw.strip():
            result[key] = raw
    return result


def stream_chunk_has_token(chunk: Any) -> bool:
    """Generated text/reasoning/tool data, excluding role/lifecycle/usage frames."""
    choices = field(chunk, "choices") or []
    if choices:
        delta = field(choices[0], "delta")
        if any(field(delta, key) for key in ("content", "reasoning_content", "reasoning", "refusal")):
            return True
        return any(field(field(tool, "function"), "name") or field(field(tool, "function"), "arguments")
                   for tool in field(delta, "tool_calls") or [])
    if field(chunk, "type") == "content_block_delta":
        delta = field(chunk, "delta")
        return any(field(delta, key) for key in ("text", "thinking", "partial_json"))
    if field(chunk, "type") == "content_block_start":
        block = field(chunk, "content_block")
        return any(field(block, key) for key in ("text", "thinking", "name"))
    return False
