import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.adapters.portal import from_portal_crawler
from shared.models import Event, RegistrationMethod, RegistrationStatus

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "crawler" / "samples"


def _load_sample(filename: str) -> dict:
    with open(SAMPLES_DIR / filename, "r", encoding="utf-8") as f:
        return json.load(f)


# 1. 2621（學習護照）：learning_passport_hours 是人文藝術 3h，tags 包含「人文藝術」
def test_learning_passport_2621():
    data = _load_sample("learning_passport_2621.json")
    event = from_portal_crawler(data)

    assert len(event.learning_passport_hours) == 1
    assert event.learning_passport_hours[0].category == "人文藝術"
    assert event.learning_passport_hours[0].hours == 3.0
    assert "人文藝術" in event.tags


# 2. 2590（已額滿）：registration_status == full，is_full 是 True
def test_full_2590():
    data = _load_sample("full_2590.json")
    event = from_portal_crawler(data)

    assert event.registration_status == RegistrationStatus.FULL
    assert event.is_full is True
    assert event.capacity == 50
    assert event.registered_count == 50


# 3. 2417：registration_method 與 registration_url 正確
def test_registration_portal_with_form_2417():
    data = _load_sample("portal_with_form_2417.json")
    event = from_portal_crawler(data)

    assert event.registration_method == RegistrationMethod.PORTAL
    assert event.registration_url is None


# 4. 同一筆轉兩次，event_id 一樣（重複匯入不會重複）
def test_idempotent_event_id():
    data = _load_sample("full_2590.json")
    event1 = from_portal_crawler(data)
    event2 = from_portal_crawler(data)

    assert event1.event_id == event2.event_id


# 5. 中文報名狀態對照全部正確（用 @pytest.mark.parametrize）
@pytest.mark.parametrize(
    "raw_status, expected_status",
    [
        ("開放報名", RegistrationStatus.OPEN),
        ("報名中", RegistrationStatus.OPEN),
        ("已額滿", RegistrationStatus.FULL),
        ("已截止", RegistrationStatus.CLOSED),
        ("尚未開始", RegistrationStatus.NOT_STARTED),
        ("未開放報名", RegistrationStatus.NOT_STARTED),
    ],
)
def test_registration_status_mapping(raw_status: str, expected_status: RegistrationStatus):
    data = _load_sample("full_2590.json")
    data["registration_status"] = raw_status
    event = from_portal_crawler(data)

    assert event.registration_status == expected_status


# 6. to_dynamo_item() → from_dynamo_item() 來回一致
@pytest.mark.parametrize(
    "filename",
    [
        "full_2590.json",
        "learning_passport_2621.json",
        "portal_with_form_2417.json",
    ],
)
def test_dynamo_item_roundtrip(filename: str):
    data = _load_sample(filename)
    event = from_portal_crawler(data)

    dynamo_item = event.to_dynamo_item()
    restored_event = Event.from_dynamo_item(dynamo_item)

    assert event == restored_event


# 7. 標題規則：場次名稱沒包含活動名稱時，title 是「活動名稱｜場次名稱」
def test_title_combines_activity_and_session_when_not_contained():
    data = _load_sample("full_2590.json")

    # 情境 A：場次名稱未包含在活動名稱中 -> 活動名稱｜場次名稱
    data_a = data.copy()
    data_a["title"] = "AI 系列講座"
    data_a["session_name"] = "第一場：入門介紹"
    event_a = from_portal_crawler(data_a)
    assert event_a.title == "AI 系列講座｜第一場：入門介紹"

    # 情境 B：場次名稱已包含在活動名稱中（或兩者相同） -> 不重複拼接
    data_b = data.copy()
    data_b["title"] = "AI 系列講座（第一場：入門介紹）"
    data_b["session_name"] = "第一場：入門介紹"
    event_b = from_portal_crawler(data_b)
    assert event_b.title == "第一場：入門介紹"


# 8. pk 缺少時拋出 KeyError，避免靜默覆蓋
def test_missing_pk_raises_key_error():
    data = _load_sample("full_2590.json")
    del data["pk"]

    with pytest.raises(KeyError):
        from_portal_crawler(data)


# 9. session_name 為 None、空字串或不存在時，使用活動名稱 title
@pytest.mark.parametrize("session_name_value", [None, ""])
def test_session_name_none_or_empty_falls_back_to_title(session_name_value):
    data = _load_sample("full_2590.json")
    data["title"] = "主活動名稱"
    data["session_name"] = session_name_value

    event = from_portal_crawler(data)
    assert event.title == "主活動名稱"


def test_session_name_missing_falls_back_to_title():
    data = _load_sample("full_2590.json")
    data["title"] = "主活動名稱"
    data.pop("session_name", None)

    event = from_portal_crawler(data)
    assert event.title == "主活動名稱"


# 10. registered_count 為 None 或不存在時，不應拋錯且不應誤填為 0
def test_registered_count_none_does_not_raise_and_preserves_capacity():
    data = _load_sample("full_2590.json")
    data["registered_count"] = None
    data["capacity"] = 50

    event = from_portal_crawler(data)
    assert event.registered_count is None
    assert event.capacity == 50


def test_registered_count_missing_is_none():
    data = _load_sample("full_2590.json")
    data.pop("registered_count", None)

    event = from_portal_crawler(data)
    assert event.registered_count is None


# 11. audience 與 raw_fields 為 None 時不拋出 AttributeError
def test_audience_and_raw_fields_none_handled_gracefully():
    data = _load_sample("full_2590.json")
    data["audience"] = None
    data["raw_fields"] = None

    event = from_portal_crawler(data)
    assert event.audience == ""
    assert event.description is None

