import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.models import (  # noqa: E402
    Category,
    Event,
    PosterExtraction,
    RegistrationMethod,
    Source,
    Status,
    load_sample_events,
    make_event_id,
)


def base(**overrides):
    data = dict(
        event_id="manual-test",
        title="測試活動",
        host="測試單位",
        start_at="2026-11-06T19:00:00+08:00",
        tags=["講座"],
        source=Source.MANUAL,
    )
    data.update(overrides)
    return data


def test_sample_events_are_valid():
    events = load_sample_events()
    assert len(events) >= 10
    assert len({e.event_id for e in events}) == len(events)
    assert {c for e in events for c in e.categories} == set(Category)  # 涵蓋四大類型


def test_naive_datetime_rejected():
    with pytest.raises(ValidationError, match="時區"):
        Event(**base(start_at=datetime(2026, 11, 6, 19, 0)))


def test_utc_converted_to_taipei_and_month():
    # 2026-11-30 17:00 UTC = 2026-12-01 01:00 台灣時間 → 屬於 12 月
    e = Event(**base(start_at=datetime(2026, 11, 30, 17, 0, tzinfo=timezone.utc)))
    assert e.start_at.isoformat() == "2026-12-01T01:00:00+08:00"
    assert e.month == "2026-12"


def test_unknown_tag_rejected():
    with pytest.raises(ValidationError, match="未知標籤"):
        Event(**base(tags=["免費晚餐"]))


def test_passport_category_becomes_tag():
    e = Event(**base(tags=[], learning_passport_hours=[{"category": "人文藝術", "hours": 3}]))
    assert e.tags == ["人文藝術"]
    assert Category.CREDIT_HOURS in e.categories


def test_needs_tag_or_passport_hours():
    with pytest.raises(ValidationError, match="至少需要一個標籤"):
        Event(**base(tags=[]))


def test_end_before_start_rejected():
    with pytest.raises(ValidationError, match="end_at"):
        Event(**base(end_at="2026-11-06T18:00:00+08:00"))


def test_registered_over_capacity_rejected():
    with pytest.raises(ValidationError, match="capacity"):
        Event(**base(capacity=10, registered_count=11))


def test_is_full():
    assert Event(**base(capacity=120, registered_count=120)).is_full
    assert not Event(**base(capacity=120, registered_count=3)).is_full
    assert not Event(**base()).is_full


def test_external_registration_needs_url():
    with pytest.raises(ValidationError, match="registration_url"):
        Event(**base(registration_method=RegistrationMethod.EXTERNAL))


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        Event(**base(date="11/6"))


def test_dynamo_roundtrip():
    e = Event(**base(
        learning_passport_hours=[{"category": "院週會", "hours": 2}],
        soft_skill_hours=[{"category": "批判思考", "hours": 0.66}],
        capacity=120, registered_count=7, status=Status.PUBLISHED,
    ))
    item = e.to_dynamo_item()
    assert item["month"] == "2026-11"
    assert item["soft_skill_hours"][0]["hours"] == Decimal("0.66")
    assert "end_at" not in item  # 空值不寫入
    assert Event.from_dynamo_item(item) == e


def test_make_event_id_is_stable():
    a = make_event_id(Source.NCU_API, "S20261016001-1")
    assert a == make_event_id(Source.NCU_API, "S20261016001-1")
    assert a != make_event_id(Source.NCU_API, "S20261016001-2")
    assert a.startswith("ncu_api-")


def test_poster_schema_exports():
    schema = PosterExtraction.model_json_schema()
    assert "start_at" in schema["properties"]
    assert "learning_passport_hours" in schema["properties"]
