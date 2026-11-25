"""LB-2：用真實 Portal 樣本與邊界資料檢查活動卡片與文字回覆格式。"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "webhook"))

import app
from activity_flex import MAX_TITLE_CHARS, build_activity_reply
from shared.adapters.portal import from_portal_crawler

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "portal").glob("*.json"))


def card_of(event: dict) -> dict:
    """只有一個活動時，回傳單張卡片的 dict。"""
    return build_activity_reply([event], "接下來 7 天")[0].to_dict()["contents"]


def body_texts(card: dict) -> list[str]:
    return [part["text"] for part in card["body"]["contents"] if part["type"] == "text"]


def base_event(**fields) -> dict:
    return {"title": "職涯分享", "start_at": "2026-11-12T09:00:00+08:00", "status": "published", **fields}


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_real_portal_samples_format_without_error(path):
    """每個真實樣本經 from_portal_crawler → to_dynamo_item 後，卡片與文字都能產生且在 LINE 限制內。"""
    item = from_portal_crawler(json.loads(path.read_text(encoding="utf-8"))).to_dynamo_item()

    messages = build_activity_reply([item], "接下來 7 天")
    card = messages[0].to_dict()["contents"]
    assert len(json.dumps(card, ensure_ascii=False).encode()) < 10_000  # 單張卡片上限 10 KB
    assert len(body_texts(card)[0]) <= MAX_TITLE_CHARS
    assert len(app.format_events([item])) <= app.LINE_TEXT_LIMIT


def test_long_title_is_truncated():
    """超過 30 字的標題截斷並加「…」。"""
    title = "115學年度生命科學系職涯輔導講座系列-從校園到職場：一個未跳離產業的老兵的告白"
    shown = body_texts(card_of(base_event(title=title)))[0]
    assert len(shown) == MAX_TITLE_CHARS
    assert shown.endswith("…")
    assert title.startswith(shown[:-1])


def test_missing_location_and_end_time():
    """沒有地點就不顯示地點列；沒有結束時間只顯示開始時間。"""
    texts = body_texts(card_of(base_event()))
    assert "時間　11/12 09:00" in texts
    assert not any(text.startswith("地點") for text in texts)


def test_full_capacity_overrides_stale_status():
    """名額已滿時顯示已額滿，即使 registration_status 還是 open。"""
    texts = body_texts(card_of(base_event(capacity=200, registered_count=200, registration_status="open")))
    assert "報名　已額滿" in texts


def test_remaining_seats_are_shown():
    texts = body_texts(card_of(base_event(capacity=200, registered_count=64, registration_status="open")))
    assert "報名　開放報名（64/200 人）" in texts


def test_zero_passport_hours_are_hidden():
    """0 小時的學習護照類別不顯示，避免誤以為有時數。"""
    hours = [{"category": "人文藝術", "hours": 0}, {"category": "服務學習", "hours": 2}]
    texts = body_texts(card_of(base_event(learning_passport_hours=hours)))
    assert "學習護照　服務學習 2 小時" in texts
    assert not any("人文藝術" in text for text in texts)


@pytest.mark.parametrize("url", [
    "https://example.com/" + "a" * 1000,          # 超過 1000 字
    "https://example.com/活動",                     # 非 ASCII
    "https://example.com/a b",                      # 含空白
    "javascript:alert(1)",                          # 非 http(s)
])
def test_invalid_url_omits_button(url):
    """LINE 不接受的網址不放按鈕，避免整則回覆被拒。"""
    assert "footer" not in card_of(base_event(info_url=url))


def test_many_sessions_are_capped():
    """場次太多時只列前 4 場，其餘以「另有 N 場」帶過。"""
    sessions = [
        base_event(activity_id="cpr", source="crawler_portal", start_at=f"2026-11-1{day}T09:00:00+08:00")
        for day in range(2, 8)
    ]
    card = build_activity_reply(sessions, "接下來 7 天")[0].to_dict()["contents"]
    texts = body_texts(card)
    assert "本次查到 6 場" in card["header"]["contents"][0]["text"]
    assert sum(text.startswith("11/1") for text in texts) == 4
    assert "…另有 2 場" in texts


def test_text_reply_respects_line_limit():
    """文字回覆不超過 LINE 5000 字上限。"""
    events = [base_event(title="很長" * 1000, perks="便當" * 1000) for _ in range(8)]
    assert len(app.format_events(events)) <= app.LINE_TEXT_LIMIT
