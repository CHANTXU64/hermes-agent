"""Fork regression coverage for launchd gateway resource limits."""

from __future__ import annotations

import plistlib


def test_generated_launchd_plist_raises_open_file_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from hermes_cli.gateway import generate_launchd_plist

    plist = plistlib.loads(generate_launchd_plist().encode("utf-8"))

    assert plist["SoftResourceLimits"]["NumberOfFiles"] == 8192
