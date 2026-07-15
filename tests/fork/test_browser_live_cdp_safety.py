"""Fork-preservation contract for browser tools attached to a live CDP browser."""

from tools.browser_tool import BROWSER_TOOL_SCHEMAS


def _description(tool_name: str) -> str:
    return next(schema["description"] for schema in BROWSER_TOOL_SCHEMAS if schema["name"] == tool_name)


def test_browser_navigate_exposes_live_cdp_tab_replacement_risk():
    """A future merge must not make live-CDP navigation look harmless again."""
    description = _description("browser_navigate")

    for required_contract in (
        "user's live Chromium-family browser via CDP",
        "currently controlled tab",
        "replace preserved user work or login state",
        "never navigate a preserved app or login tab",
        "task-owned safe tab/session",
    ):
        assert required_contract in description


def test_browser_vision_exposes_cross_target_screenshot_risk():
    """A screenshot must not be treated as proof about an unrelated CDP target."""
    description = _description("browser_vision")

    for required_contract in (
        "not guaranteed to be the same tab or target",
        "separately bound by DrissionPage or raw CDP",
        "verify that the current page URL/target matches",
        "capture through that exact target",
    ):
        assert required_contract in description