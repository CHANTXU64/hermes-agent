"""Keep a connected shared browser read-only until a target is chosen.

Use browser-harness's native targets and helpers. No legacy driver, short-ID
registry or daemon-generation mechanism is required for this policy.
"""
from __future__ import annotations


def prepare_browser_code(code: str, tab_id: str | None = None) -> str:
    """Build the code sent to the official harness, not a separate executor."""
    if tab_id is None:
        return (
            "import json as _hermes_json\n"
            "print(_hermes_json.dumps({'selection_required': True, 'tabs': list_tabs()}, ensure_ascii=False))\n"
        )
    if not isinstance(tab_id, str) or not tab_id.strip():
        raise ValueError("tab_id must be an exact targetId from the tab list, or 'new'.")
    return (
        f"_hermes_target = {tab_id!r}\n"
        "if _hermes_target == 'new':\n"
        "    _hermes_target = cdp('Target.createTarget', url='about:blank', background=True).get('targetId')\n"
        "    if not isinstance(_hermes_target, str) or not _hermes_target:\n"
        "        raise RuntimeError('New tab was not confirmed; no payload ran.')\n"
        "elif not any(t.get('targetId') == _hermes_target for t in list_tabs()):\n"
        "    raise RuntimeError('Unknown or closed tab: list tabs again; no payload ran.')\n"
        "switch_tab(_hermes_target)\n"
        "if current_tab().get('targetId') != _hermes_target:\n"
        "    raise RuntimeError('Target selection was not confirmed; no payload ran.')\n"
        "del _hermes_target\n"
        + code
    )
