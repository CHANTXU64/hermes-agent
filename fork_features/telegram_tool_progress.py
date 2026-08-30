"""Fork-owned delivery policy for literal Telegram tool progress.

Gateway Core owns progress lifecycle, topics, accumulation, rollover, and
message delivery. The Telegram adapter owns generic ``plain_text`` rendering.
This module owns only the Fork decision that dynamic tool-progress messages on
Telegram request that rendering mode.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def _platform_value(platform: Any) -> str:
    """Return a normalized platform value for enums or raw strings."""
    return str(getattr(platform, "value", platform) or "").strip().lower()


def tool_progress_delivery_metadata(
    metadata: Optional[Dict[str, Any]] = None,
    *,
    platform: Any = None,
) -> Optional[Dict[str, Any]]:
    """Mark only Telegram tool-progress text for literal delivery."""
    if _platform_value(platform) != "telegram":
        return metadata
    merged = dict(metadata or {})
    merged["plain_text"] = True
    return merged
