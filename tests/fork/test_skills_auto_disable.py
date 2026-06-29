"""Fork-owned tests for disabling newly bundled skills by config."""

from unittest.mock import patch

from tests.tools.test_skills_sync import (
    TestSyncSkills as _SyncSkillsHelpers,
    _dir_hash,
)
from tools.skills_sync import sync_skills


def test_fresh_install_can_auto_disable_new_bundled_skills(tmp_path):
    helper = _SyncSkillsHelpers()
    bundled = helper._setup_bundled(tmp_path)
    skills_dir = tmp_path / "user_skills"
    manifest_file = skills_dir / ".bundled_manifest"
    config = {"skills": {"auto_enable_new_bundled": False, "disabled": ["existing-skill"]}}

    with helper._patches(bundled, skills_dir, manifest_file), patch(
        "hermes_cli.config.load_config", return_value=config
    ), patch("hermes_cli.config.save_config") as save_config:
        result = sync_skills(quiet=True)

    assert sorted(result["copied"]) == ["new-skill", "old-skill"]
    assert sorted(result["auto_disabled"]) == ["new-skill", "old-skill"]
    assert config["skills"]["disabled"] == ["existing-skill", "new-skill", "old-skill"]
    save_config.assert_called_once_with(config)
    assert (skills_dir / "category" / "new-skill" / "SKILL.md").exists()
    assert (skills_dir / "old-skill" / "SKILL.md").exists()


def test_auto_disable_only_applies_to_newly_copied_skills(tmp_path):
    helper = _SyncSkillsHelpers()
    bundled = helper._setup_bundled(tmp_path)
    skills_dir = tmp_path / "user_skills"
    manifest_file = skills_dir / ".bundled_manifest"
    skills_dir.mkdir(parents=True)
    old_hash = _dir_hash(bundled / "old-skill")
    manifest_file.write_text(f"old-skill:{old_hash}\n")
    config = {"skills": {"auto_enable_new_bundled": False, "disabled": []}}

    with helper._patches(bundled, skills_dir, manifest_file), patch(
        "hermes_cli.config.load_config", return_value=config
    ), patch("hermes_cli.config.save_config") as save_config:
        result = sync_skills(quiet=True)

    assert result["copied"] == ["new-skill"]
    assert result["auto_disabled"] == ["new-skill"]
    assert config["skills"]["disabled"] == ["new-skill"]
    save_config.assert_called_once_with(config)
    assert not (skills_dir / "old-skill").exists()
