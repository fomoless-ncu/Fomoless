"""Date helpers for "this week" queries, in Taiwan time (UTC+8, no DST)."""

from datetime import datetime, timedelta, timezone

TAIPEI = timezone(timedelta(hours=8))


def week_range(now: datetime) -> tuple[datetime, datetime]:
    """Return [start, end) for the next 7 days from the start of today (Taipei time)."""
    local = now.astimezone(TAIPEI)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=7)


def months_between(start: datetime, end: datetime) -> list[str]:
    """Month partition keys ("YYYY-MM") touched by [start, end)."""
    months = []
    cursor = start.replace(day=1)
    last = (end - timedelta(microseconds=1)).replace(day=1)
    while cursor <= last:
        months.append(cursor.strftime("%Y-%m"))
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    return months
