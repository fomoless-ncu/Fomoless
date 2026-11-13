import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "webhook"))

from week import TAIPEI, has_ended, months_between, query_bounds, week_range  # noqa: E402


def test_week_starts_at_taipei_midnight():
    # 2026-11-04 20:00 UTC is 2026-11-05 04:00 in Taipei
    start, end = week_range(datetime(2026, 11, 4, 20, 0, tzinfo=timezone.utc))
    assert start == datetime(2026, 11, 5, 0, 0, tzinfo=TAIPEI)
    assert end == datetime(2026, 11, 12, 0, 0, tzinfo=TAIPEI)


def test_single_month():
    start, end = week_range(datetime(2026, 11, 4, 12, 0, tzinfo=TAIPEI))
    assert months_between(start, end) == ["2026-11"]


def test_week_crossing_month():
    start, end = week_range(datetime(2026, 11, 28, 12, 0, tzinfo=TAIPEI))
    assert months_between(start, end) == ["2026-11", "2026-12"]


def test_week_crossing_year():
    start, end = week_range(datetime(2026, 12, 29, 9, 0, tzinfo=TAIPEI))
    assert months_between(start, end) == ["2026-12", "2027-01"]


def test_query_bounds_exclude_day_8_midnight():
    start, end = week_range(datetime(2026, 11, 4, 12, 0, tzinfo=TAIPEI))
    low, high = query_bounds(start, end)
    assert low == "2026-11-04T00:00:00+08:00"
    assert high == "2026-11-10T23:59:59+08:00"
    # 第 8 天 00:00 整的活動不在 BETWEEN 範圍內；第 7 天最後一秒仍在
    assert not (low <= "2026-11-11T00:00:00+08:00" <= high)
    assert low <= "2026-11-10T23:59:59+08:00" <= high


def test_has_ended():
    now = datetime(2026, 11, 4, 6, 0, tzinfo=timezone.utc)  # 台北 14:00
    assert has_ended({"end_at": "2026-11-04T12:00:00+08:00"}, now)
    assert has_ended({"end_at": "2026-11-04T14:00:00+08:00"}, now)  # 剛好結束
    assert not has_ended({"end_at": "2026-11-04T16:00:00+08:00"}, now)
    # 沒有 end_at 無法判斷，視為尚未結束
    assert not has_ended({"start_at": "2026-11-04T09:00:00+08:00"}, now)
    assert not has_ended({"end_at": None}, now)
