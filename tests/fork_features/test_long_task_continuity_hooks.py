"""Generic compression lifecycle contract used by fork-only plugins."""

from hermes_cli.plugins import SHELL_UNSUPPORTED_HOOKS, VALID_HOOKS


def test_generic_compression_lifecycle_hooks_are_supported():
    assert {
        "on_compression_start",
        "on_compression_prepare_commit",
        "on_compression_finish",
    }.issubset(VALID_HOOKS)
    assert "on_compression_prepare_commit" in SHELL_UNSUPPORTED_HOOKS
    assert {
        "pre_context_checkpoint",
        "context_checkpoint_output",
        "context_compression_committed",
        "context_recovery_delivered",
    }.isdisjoint(VALID_HOOKS)
