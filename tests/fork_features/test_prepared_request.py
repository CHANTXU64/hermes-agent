"""All compression triggers share the same frozen request/rebuild pair."""
import copy
import importlib
import importlib.util
from types import SimpleNamespace

import pytest

from fork_features.request_fork import FrozenCodexRequest


def test_adoption_factory_captures_history_and_preserves_request_only_context():
    spec = importlib.util.find_spec("fork_features.request_fork.prepared_request")
    assert spec is not None, "Request reconstruction belongs to the request Fork boundary"
    module = importlib.import_module(spec.name)
    agent = SimpleNamespace(
        provider="openai-codex", base_url="https://chatgpt.com/backend-api/codex",
        _is_codex_backend=lambda: True, _needs_thinking_reasoning_pad=lambda: False,
    )
    original = [{"role": "user", "content": "prior"}, {"role": "user", "content": "live"}]
    body = {"input": [*copy.deepcopy(original), {"role": "developer", "content": "this request only"}],
            "tools": [{"type": "function", "name": "test"}], "prompt_cache_key": "stable"}
    frozen = FrozenCodexRequest(body=body, fidelity="prepared_parent", captured_session_id="s")
    canonicalized = []
    rebuild = module.build_adopt_rematerializer(
        agent, prepared_request=frozen, original_messages=original, current_turn_user_idx=1,
        canonicalize_tool_calls=lambda rows: canonicalized.append(copy.deepcopy(rows)),
    )
    assert callable(rebuild)
    adopted = [original[0], {"role": "assistant", "content": "new durable turn"}, original[1]]
    rebuilt = rebuild(adopted)
    assert rebuilt is not None
    assert canonicalized[0][0]["content"] == "new durable turn"
    result = rebuilt.clone_body()
    assert result["input"][-2:] == body["input"][-2:]
    assert result["tools"] == body["tools"]
    assert result["prompt_cache_key"] == "stable"
    assert frozen.clone_body() == body
    original[0]["content"] = "caller mutated after capture"
    assert rebuild(adopted) is None  # The captured original stays authoritative.


@pytest.mark.parametrize("index", [-1, 3])
def test_invalid_live_anchor_does_not_build_a_reconstruction(index):
    spec = importlib.util.find_spec("fork_features.request_fork.prepared_request")
    assert spec is not None
    module = importlib.import_module(spec.name)
    assert module.build_adopt_rematerializer(
        SimpleNamespace(), prepared_request=object(), original_messages=[],
        current_turn_user_idx=index, canonicalize_tool_calls=lambda rows: None,
    ) is None
