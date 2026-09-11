"""Fork observer lifecycle, independent of the upstream compression engine."""
import copy
import importlib
import importlib.util
from types import SimpleNamespace

import pytest

from fork_features.request_fork import FrozenCodexRequest


def lifecycle_module():
    spec = importlib.util.find_spec("fork_features.request_fork.compression_lifecycle")
    assert spec is not None, "Fork lifecycle must be usable independently of the compression engine"
    return importlib.import_module(spec.name)


@pytest.mark.parametrize("committed", [True, False])
def test_deferred_finish_is_consumed_once_after_outer_commit(monkeypatch, committed):
    module = lifecycle_module()
    events = []
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda name: True)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda name, **kw: events.append((name, kw)))
    agent = SimpleNamespace(session_id="old", api_mode="chat_completions", tools=[])
    lifecycle = module.CompressionLifecycle(
        agent, attempt_id="attempt", in_place=False, trigger_source="manual",
        request_fork=None, request_fork_rematerializer=None, commit_fence=None,
        defer_finish=True,
    )
    lifecycle.start([{"role": "user", "content": "keep the whole task"}])
    lifecycle.set_outcome("committed")
    agent.session_id = "new"
    lifecycle.finish()
    lifecycle.finish()
    assert module.has_pending_finish(agent)
    assert [name for name, _ in events] == ["on_compression_start"]
    assert module.finalize_pending_finish(agent, committed=committed)
    assert not module.finalize_pending_finish(agent, committed=committed)
    assert not module.has_pending_finish(agent)
    name, payload = events[-1]
    assert name == "on_compression_finish"
    assert payload["old_session_id"] == "old"
    assert payload["session_id"] == "new"
    assert payload["outcome"] == ("committed" if committed else "aborted")


def test_adoption_replaces_frozen_request_and_hook_cannot_mutate_history(monkeypatch):
    module = lifecycle_module()
    events = []
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda name: True)

    def hook(name, **payload):
        events.append((name, copy.deepcopy(payload)))
        if name == "on_compression_start":
            payload["messages"].clear()
            payload["request_messages"].clear()

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", hook)
    from agent.transports.codex import ResponsesApiTransport

    agent = SimpleNamespace(session_id="old", api_mode="codex_responses", tools=[],
                            _get_transport=ResponsesApiTransport)
    body = {"input": [{"role": "user", "content": "adopted request"}], "tools": []}
    frozen = FrozenCodexRequest(body=body, fidelity="prepared_parent", captured_session_id="old")
    lifecycle = module.CompressionLifecycle(
        agent, attempt_id="attempt", in_place=False, trigger_source="auto",
        request_fork=None, request_fork_rematerializer=lambda messages: frozen,
        commit_fence=None, defer_finish=False,
    )
    messages = [{"role": "user", "content": "adopted history"}]
    lifecycle.rematerialize_after_adoption(messages)
    lifecycle.start(messages)
    lifecycle.set_outcome("failed", "summary failed")
    lifecycle.finish()
    lifecycle.finish()
    assert messages == [{"role": "user", "content": "adopted history"}]
    assert frozen.clone_body() == body
    assert [name for name, _ in events] == ["on_compression_start", "on_compression_finish"]
    assert events[0][1]["request_messages"] == body["input"]
    assert events[1][1]["outcome"] == "failed"
