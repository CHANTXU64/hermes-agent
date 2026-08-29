"""Generic compression lifecycle contract used by fork-only plugins."""

from hermes_cli.plugins import VALID_HOOKS


def test_generic_compression_lifecycle_hooks_are_supported():
    assert {
        "on_compression_start",
        "on_compression_finish",
    }.issubset(VALID_HOOKS)
    assert {
        "pre_context_checkpoint",
        "context_checkpoint_output",
        "context_compression_committed",
        "context_recovery_delivered",
    }.isdisjoint(VALID_HOOKS)
