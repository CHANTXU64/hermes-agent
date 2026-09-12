"""Daily wall-clock policy for the native session heartbeat, not a second scheduler."""
from datetime import datetime, time, timedelta
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DAILY_USAGE = "/heartbeat daily HH:MM,HH:MM <IANA-timezone> <prompt>"
WEEKLY_USAGE = "/heartbeat weekly <Mon-Fri HH:MM,...> <Sat-Sun HH:MM,...> <IANA-timezone> <prompt>"


def validate_daily(times, timezone):
    if not isinstance(times, (list, tuple)) or not times:
        raise ValueError("Daily heartbeat requires at least one HH:MM time.")
    if any(not isinstance(t, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t) for t in times):
        raise ValueError("Daily times must use HH:MM (00:00 through 23:59).")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid heartbeat timezone: {timezone}") from exc
    return tuple(sorted(set(times)))


def next_daily_after(anchor, times, timezone, weekend_times=None, now=None):
    """Next scheduled instant strictly after the anchor. Missed ticks coalesce upstream.

    Spring-forward gaps are skipped; fall-back repeated local times fire only once
    (the earlier occurrence). No dependency on the process/host timezone.
    Weekly schedules coalesce only today's missed ticks, not another day's work.
    """
    zone = ZoneInfo(timezone)
    local_date = datetime.fromtimestamp(anchor, zone).date()
    if weekend_times is not None and now is not None:
        local_date = max(local_date, datetime.fromtimestamp(now, zone).date())
    for offset in range(3):
        day = local_date + timedelta(days=offset)
        for value in (weekend_times if weekend_times is not None and day.weekday() >= 5 else times):
            hour, minute = map(int, value.split(":"))
            candidate = datetime.combine(day, time(hour, minute), zone)
            stamp = candidate.timestamp()
            if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) != candidate.replace(tzinfo=None):
                continue
            if stamp > anchor:
                return stamp
    raise ValueError("No daily heartbeat occurrence in the next three local dates.")


def configure_daily(manager, args):
    """Shared CLI/Gateway parser; None delegates unchanged interval syntax upstream."""
    tokens = args.split(None, 3)
    if tokens and tokens[0].lower() == "weekly":
        tokens = args.split(None, 4)
        if len(tokens) != 5:
            raise ValueError(f"Usage: {WEEKLY_USAGE}")
        return manager.set_daily(tokens[4], tokens[1].split(","), tokens[3],
                                 weekend_times=tokens[2].split(","))
    if not tokens or tokens[0].lower() != "daily":
        return None
    if len(tokens) != 4:
        raise ValueError(f"Usage: {DAILY_USAGE}")
    return manager.set_daily(tokens[3], tokens[1].split(","), tokens[2])
