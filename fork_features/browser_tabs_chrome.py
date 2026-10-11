"""Read-only Chrome window selection, independent of the driver's active tab.

agent-browser 0.26 does not expose CDP IDs. Correlation is display-only and only
used for unique URLs; duplicate URLs stay unknown. Never use it to route actions.
"""
from __future__ import annotations

import asyncio
from collections import Counter


def match_selection(tabs: list[dict], observed: list[dict]) -> dict[str, dict]:
    native_counts = Counter(t.get("url") for t in tabs)
    cdp_counts = Counter(t.get("url") for t in observed)
    visible_counts = Counter(t.get("window_id") for t in observed if t.get("visible") is True)
    result = {}
    for tab in tabs:
        state = {"window_id": None, "chrome_active": None}
        url = tab.get("url")
        if native_counts[url] == cdp_counts[url] == 1:
            page = next(t for t in observed if t.get("url") == url)
            window = page.get("window_id")
            state["window_id"] = window
            if isinstance(page.get("title"), str):
                state["title"] = page["title"]
            # A minimized window reports all pages hidden. It doesn't mean all
            # of its tabs are unselected. Partial or contradictory reads are unknown.
            window_pages = [t for t in observed if t.get("window_id") == window]
            if window is not None and visible_counts[window] == 1 and all(
                isinstance(t.get("visible"), bool) for t in window_pages
            ):
                state["chrome_active"] = page["visible"]
        result[tab["tabId"]] = state
    return result


async def _observe(endpoint: str) -> list[dict]:
    from tools.browser_cdp_tool import _cdp_call
    targets = (await _cdp_call(endpoint, "Target.getTargets", {}, None, 3)).get("targetInfos", [])
    pages = [t for t in targets if t.get("type") == "page"]
    sem = asyncio.Semaphore(4)

    async def inspect(page):
        info = {"url": page.get("url", ""), "title": page.get("title", ""),
                "window_id": None, "visible": None}
        async with sem:
            try:
                window = await _cdp_call(endpoint, "Browser.getWindowForTarget",
                                         {"targetId": page["targetId"]}, None, 2)
                info["window_id"] = window.get("windowId")
                visibility = await _cdp_call(endpoint, "Runtime.evaluate", {
                    "expression": "document.visibilityState === 'visible'", "returnByValue": True,
                }, page["targetId"], 2)
                value = visibility.get("result", {}).get("value")
                if isinstance(value, bool):
                    info["visible"] = value
            except Exception:
                # Discarded tabs, dialogs and non-Chrome endpoints may not answer.
                # Inspection failure must not make tab management unusable.
                pass
        return info

    return await asyncio.gather(*(inspect(page) for page in pages))


def selection_states(info: dict, tabs: list[dict]) -> dict[str, dict]:
    endpoint = info.get("cdp_url")
    if not endpoint:
        return {}
    from tools.browser_cdp_tool import _run_async
    try:
        observed = _run_async(asyncio.wait_for(_observe(endpoint), timeout=6))
        return match_selection(tabs, observed)
    except Exception:
        return {}
