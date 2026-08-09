"""Schema-shape tests for the built-in memory tool.

The memory tool previously used ``allOf: [{if: ..., then: {required: ...}}]``
at the top level of ``parameters`` to hint per-action required fields.  That
form was:

  1. Ignored by every provider (Chat Completions doesn't honour ``if/then``
     on function schemas), so it never actually enforced anything.
  2. **Rejected outright by strict backends** — OpenAI's Codex endpoint
     (``chatgpt.com/backend-api/codex``, gpt-5.x) returns
     ``Invalid schema for function 'memory': schema must have type 'object'
     and not have 'oneOf'/'anyOf'/'allOf'/'enum'/'not' at the top level``.

We now rely on the runtime handler (``memory_tool()`` in ``tools/memory_tool.py``)
to validate required fields per action and return actionable error messages.
These tests guard the schema against regressing back to a shape strict
backends reject.
"""

import json

from tools.memory_tool import MEMORY_SCHEMA


_FORBIDDEN_TOP_LEVEL_KEYS = ("allOf", "anyOf", "oneOf", "enum", "not")


def test_memory_schema_has_no_forbidden_top_level_combinators():
    """OpenAI's Codex backend rejects these at the top level of parameters."""
    params = MEMORY_SCHEMA["parameters"]
    for key in _FORBIDDEN_TOP_LEVEL_KEYS:
        assert key not in params, (
            f"top-level {key!r} in memory tool parameters will break the "
            "Codex backend (chatgpt.com/backend-api/codex). Per-action "
            "required-field checks belong in the runtime handler, not the schema."
        )


def test_memory_schema_is_json_serializable():
    json.dumps(MEMORY_SCHEMA)


def test_memory_schema_exposes_bounded_history_without_full_log_reads():
    assert "history" in MEMORY_SCHEMA["parameters"]["properties"]["action"]["enum"]
    description = MEMORY_SCHEMA["description"]
    assert "For a pure add, do not read history" in description
    assert "memory(action='history'" in description
    assert "never load the full audit log" in description
    assert "MEMORY_CHANGELOG.md" not in description


def test_memory_schema_distinguishes_observed_lessons_from_precautions():
    description = MEMORY_SCHEMA["description"]
    assert "actually observed incident or user correction" in description
    assert "merely preventive concern" in description
    assert "Never label an unobserved concern as a lesson" in description
    assert "generic safety precautions" in description


def test_memory_schema_does_not_duplicate_repository_design_records():
    description = MEMORY_SCHEMA["description"]
    assert "implementation designs, architecture notes, or fork-only behavior" in description
    assert "already documented in repository docs" in description
    assert "short pre-load trigger" in description
