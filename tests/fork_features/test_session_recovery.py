"""Generic host recovery contracts; no standalone plugin or provider required."""
import asyncio

import pytest

from fork_features.request_fork.session_recovery import restore_reset_context, recovery_context_message


@pytest.mark.parametrize("result", [None, {}, {"source": "bad/source", "context": "x"},
                                   {"source": "test", "context": "x" * 65_000}])
def test_invalid_or_oversized_context_is_not_accepted(result):
    assert recovery_context_message([result]) == (None, "")


def test_context_selection_and_envelope_are_shared_with_compression():
    row, source = recovery_context_message([
        {"source": "first", "context": " retained "},
        {"source": "second", "context": "not selected"},
    ])
    assert source == "first"
    assert row == {"role": "user", "display_kind": "hidden", "content":
        '<hermes-runtime-context user-authored="false" source="first">\nretained\n</hermes-runtime-context>'}


@pytest.mark.parametrize("write_mode", ["persist", "queued", "raise"])
def test_completion_reports_only_verified_durable_context(monkeypatch, write_mode):
    import hermes_cli.lifecycle as lifecycle
    calls, stored = [], []
    def hook(name, **payload):
        calls.append((name, payload))
        if name == "on_session_reset":
            return [{"source": "test", "context": "saved task"}]
        return []
    monkeypatch.setattr(lifecycle, "invoke_hook", hook)
    class Store:
        async def append_to_transcript(self, sid, message):
            assert sid == "new"
            if write_mode == "raise":
                raise OSError("test failure")
            if write_mode == "persist":
                stored.append(message)
        async def load_transcript(self, sid):
            return stored
    result = asyncio.run(restore_reset_context(Store(), old_session_id="old", new_session_id="new", platform="telegram"))
    assert result is (write_mode == "persist")
    assert calls[0][0] == "on_session_reset"
    assert calls[0][1]["reason"] == "compression_exhausted"
    assert calls[-1][0] == "on_session_reset_complete"
    assert calls[-1][1]["persistent_context_source"] == ("test" if write_mode == "persist" else "")
    assert calls[-1][1]["outcome"] == ("persisted" if write_mode == "persist" else "failed")


def test_no_recovery_plugin_preserves_empty_reset(monkeypatch):
    import hermes_cli.lifecycle as lifecycle
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda *a, **kw: [])
    class Store:
        async def append_to_transcript(self, *a):
            raise AssertionError("no context must not write a synthetic row")
    assert asyncio.run(restore_reset_context(Store(), old_session_id="old", new_session_id="new", platform="telegram")) is None
