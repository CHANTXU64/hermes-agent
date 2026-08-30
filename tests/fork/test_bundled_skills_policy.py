"""Unit contract for the fork-owned bundled-skill enablement policy."""

from unittest.mock import patch

from fork_features.bundled_skills_policy import (
    disable_new_bundled_skills_if_configured,
)


def test_policy_disables_only_newly_copied_skills():
    config = {
        "skills": {
            "auto_enable_new_bundled": False,
            "disabled": ["existing-skill"],
        }
    }

    with patch("hermes_cli.config.load_config", return_value=config), patch(
        "hermes_cli.config.save_config"
    ) as save_config:
        disabled = disable_new_bundled_skills_if_configured(
            ["new-skill", "existing-skill"]
        )

    assert disabled == ["new-skill"]
    assert config["skills"]["disabled"] == ["existing-skill", "new-skill"]
    save_config.assert_called_once_with(config)
