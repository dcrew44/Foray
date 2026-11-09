"""Quiet hours (#59): the stretch of the user's day in which nothing is sent. Pure.

The two times are `Settings.quiet_hours_start` and `quiet_hours_end` ("HH:MM"), read on the
user's own clock (`users.timezone`, which the chat view and Settings keep equal to the
browser's). A period may cross midnight (22:00 to 08:00, the default); the same time twice
means there is none. The start is inside the period and the end is not.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["clock", "quiet_until", "zone"]


def zone(name: str | None) -> ZoneInfo:
    """The user's zone, or UTC when the stored name is not one (it is validated on the way in)."""
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError, ValueError:
        return ZoneInfo("UTC")


def clock(value: str) -> time:
    """`"22:00"` as a time of day. The settings model only stores that shape."""
    hours, _, minutes = value.partition(":")
    return time(int(hours), int(minutes))


def quiet_until(now: datetime, start: time, end: time, tz: ZoneInfo) -> datetime | None:
    """When `now` falls in the quiet period, the instant (UTC) the period ends; else None."""
    if start == end:
        return None
    local = now.astimezone(tz)
    at = local.time().replace(tzinfo=None)
    today = local.date()
    if start < end:
        if not start <= at < end:
            return None
        ends_on = today
    elif at >= start:
        ends_on = today + timedelta(days=1)  # this evening's period ends tomorrow morning
    elif at < end:
        ends_on = today  # past midnight, inside the period that began yesterday
    else:
        return None
    # Built on the wall clock of the day it ends, so a change of clocks overnight is honoured.
    until = datetime.combine(ends_on, end, tzinfo=tz).astimezone(UTC)
    # In the hour the clocks go back, a wall time comes twice and the first is already past.
    return until if until > now else None
