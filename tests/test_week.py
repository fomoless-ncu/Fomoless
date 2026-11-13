import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "webhook"))

from week import TAIPEI, months_between, week_range  # noqa: E402


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
