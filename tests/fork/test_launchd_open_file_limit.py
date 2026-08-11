"""Fork regression: launchd plist uses official configurable nofile floor."""

from __future__ import annotations

import plistlib


def test_generated_launchd_plist_uses_configured_nofile_soft_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    import hermes_cli.resource_limits as resource_limits
    from hermes_cli.gateway import generate_launchd_plist

    monkeypatch.setattr(
        resource_limits, "configured_nofile_soft_limit", lambda config=None: 65536
    )

    plist = plistlib.loads(generate_launchd_plist().encode("utf-8"))

    assert plist["SoftResourceLimits"]["NumberOfFiles"] == 65536


def test_generated_launchd_plist_omits_nofile_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    import hermes_cli.resource_limits as resource_limits
    from hermes_cli.gateway import generate_launchd_plist

    monkeypatch.setattr(
        resource_limits, "configured_nofile_soft_limit", lambda config=None: None
    )

    plist = plistlib.loads(generate_launchd_plist().encode("utf-8"))

    assert "SoftResourceLimits" not in plist
