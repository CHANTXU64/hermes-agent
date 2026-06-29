"""Fork-owned slash-command regressions for /retain and Slack slots."""

from hermes_cli.commands import (
    _SLACK_VIA_HERMES_ONLY,
    resolve_command,
    slack_native_slashes,
    slack_subcommand_map,
)


def test_retain_command_has_only_short_canonical_name():
    retain = resolve_command("retain")
    assert retain is not None
    assert retain.name == "retain"
    assert resolve_command("retain-session") is None
    assert resolve_command("hindsight-retain-session") is None
    assert resolve_command("retain_session") is None


def test_retain_keeps_native_slack_slot_while_low_frequency_commands_use_hermes():
    native_slashes = {name for name, _description, _usage in slack_native_slashes()}
    slack_subcommands = slack_subcommand_map()

    assert "retain" in native_slashes
    assert "retain" not in _SLACK_VIA_HERMES_ONLY

    for command in {"moa", "debug", "blueprint", "credits", "disk-cleanup", "disk_cleanup", "lcm"}:
        assert command in _SLACK_VIA_HERMES_ONLY
        if command in slack_subcommands:
            assert command not in native_slashes
