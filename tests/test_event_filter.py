"""使用模擬場次驗證活動條件篩選，無須連線資料庫或 AI。"""

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "webhook"))

from app import activity_has_perk, filter_events
from llm.activity_query import ActivityQuery


@pytest.mark.parametrize(
    "description, tags, perk, expected",
    [
        ("現場提供便當", [], "便當", True),
        ("提供便當與抽獎", [], "抽獎", True),
        (None, ["免費便當"], "便當", True),
        ("不提供便當", ["免費便當"], "便當", False),
        ("無便當", [], "便當", False),
        ("不會提供免費便當", [], "便當", False),
        ("不提供便當", ["免費便當"], "免費便當", False),
        ("不提供便當，提供抽獎", [], "抽獎", True),
        (None, [], "便當", False),
        ("提供便當", [], " ", False),
    ],
)
def test_perk_matching(description, tags, perk, expected):
    """確認福利說明與既有標籤可供比對，否定文字優先排除該福利。"""
    event = {"perks": description, "tags": tags}

    assert activity_has_perk(event, perk) is expected


def test_all_perks_must_match_in_one_session():
    """兩種福利須由同一場次提供，避免將不同活動的福利合併。"""
    events = [
        {"perks": "提供便當及抽獎"},
        {"perks": "提供便當"},
        {"perks": "提供抽獎"},
    ]
    query = ActivityQuery(period="this_week", perks=["便當", "抽獎"])

    assert filter_events(events, query) == [events[0]]


@pytest.mark.parametrize(
    "query_field, hours_field, category",
    [
        ("learning_passport_category", "learning_passport_hours", "人文藝術"),
        ("soft_skill_category", "soft_skill_hours", "跨域整合"),
    ],
)
@pytest.mark.parametrize(
    "entries, expected",
    [
        ("positive", True),
        ("zero", False),
        ("other_category", False),
        ("empty", False),
        ("missing", False),
    ],
)
def test_hour_category_matching(query_field, hours_field, category, entries, expected):
    """確認兩種時數各自比對正確欄位，支援 DynamoDB Decimal，零時數與缺值排除。"""
    values = {
        "positive": [{"category": category, "hours": Decimal("1.33")}],
        "zero": [{"category": category, "hours": Decimal("0")}],
        "other_category": [{"category": "其他類別", "hours": Decimal("3")}],
        "empty": [],
    }
    event = {} if entries == "missing" else {hours_field: values[entries]}
    query = ActivityQuery(period="this_week", **{query_field: category})

    assert filter_events([event], query) == ([event] if expected else [])


def test_combined_filters_preserve_order_and_input():
    """文字、代碼、福利與兩種時數同時生效，並保留符合場次的排序及原始清單。"""
    first = {
        "title": "AI 講座", "host": "文學院", "mode": "online",
        "registration_method": "none", "learning_passport_condition": "both",
        "perks": "提供便當",
        "learning_passport_hours": [{"category": "人文藝術", "hours": 3}],
        "soft_skill_hours": [{"category": "跨域整合", "hours": 1}],
    }
    second = {**first, "title": "AI 工作坊"}
    excluded = {**first, "mode": "onsite"}
    events = [second, excluded, first]
    query = ActivityQuery(
        period="next_week", title="ai", host="文學", mode="online",
        registration_method="none", learning_passport_condition="both",
        perks=["便當"], learning_passport_category="人文藝術",
        soft_skill_category="跨域整合",
    )

    assert filter_events(events, query) == [second, first]
    assert len(events) == 3
    assert filter_events(events, ActivityQuery(period="this_week")) == events
