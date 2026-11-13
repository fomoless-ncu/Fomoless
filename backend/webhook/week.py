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


def query_bounds(start: datetime, end: datetime) -> tuple[str, str]:
    """把 [start, end) 轉成 DynamoDB BETWEEN 用的兩個字串（BETWEEN 兩端都包含）。

    上界往前推 1 秒，第 8 天 00:00:00 整的活動就不會被算進「接下來 7 天」。
    start_at 一律存成到秒的 +08:00 字串，所以 23:59:59 涵蓋當天所有活動。
    """
    return start.isoformat(), (end - timedelta(seconds=1)).isoformat()


def has_ended(event: dict, now: datetime) -> bool:
    """活動是否已經結束：有 end_at 且 end_at <= now。

    沒有 end_at 的活動無法判斷，一律視為尚未結束（寧可多列，不要漏掉）。
    """
    end_at = event.get("end_at")
    if not end_at:
        return False
    return datetime.fromisoformat(end_at) <= now
