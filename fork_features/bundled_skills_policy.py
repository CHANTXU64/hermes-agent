"""Fork-owned policy for newly copied bundled skills.

The upstream synchronizer owns discovery, copying, manifest tracking, and user
modification protection.  This module owns only the fork preference that a
newly copied bundled skill may be installed but disabled on its first sync.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

logger = logging.getLogger(__name__)


def disable_new_bundled_skills_if_configured(skill_names: Iterable[str]) -> list[str]:
    """Disable only bundled skills copied by the current sync when configured.

    Missing configuration preserves the normal Hermes behavior: newly bundled
    skills remain enabled. Existing disabled choices are retained.
    """
    copied = [str(name) for name in skill_names]
    if not copied:
        return []

    try:
        from hermes_cli.config import load_config, save_config

        config = load_config()
        skills_config = config.setdefault("skills", {})
        if skills_config.get("auto_enable_new_bundled", True) is not False:
            return []

        disabled_raw = skills_config.get("disabled", [])
        disabled = (
            [str(name) for name in disabled_raw]
            if isinstance(disabled_raw, list)
            else []
        )
        disabled_set = set(disabled)
        added = [name for name in copied if name not in disabled_set]
        if not added:
            return []

        skills_config["disabled"] = sorted(disabled + added)
        save_config(config)
        return added
    except Exception as exc:
        logger.debug(
            "Failed to disable newly bundled skills %s: %s",
            copied,
            exc,
            exc_info=True,
        )
        return []
