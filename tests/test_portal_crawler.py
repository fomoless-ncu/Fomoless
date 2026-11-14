"""iNCU 活動報名爬蟲（crawler/portal_activity_crawler.py）的單元測試：只測純函式和 HTML 解析，不連網路。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "crawler"))

import portal_activity_crawler as pac  # noqa: E402


# ---- iso_range --------------------------------------------------------------
def test_iso_range_start_and_end():
    assert pac.iso_range("2026-11-23 13:00 ~ 2026-11-23 17:00") == (
        "2026-11-23T13:00:00+08:00",
        "2026-11-23T17:00:00+08:00",
    )


def test_iso_range_pads_single_digit_month_day_hour():
    assert pac.iso_range("2026-1-5 9:30 ~ 2026-1-5 10:00") == (
        "2026-01-05T09:30:00+08:00",
        "2026-01-05T10:00:00+08:00",
    )


def test_iso_range_only_start():
    assert pac.iso_range("2026-11-23 13:00") == ("2026-11-23T13:00:00+08:00", None)


@pytest.mark.parametrize("value", [None, "", "另行通知"])
def test_iso_range_no_time(value):
    assert pac.iso_range(value) == (None, None)


# ---- parse_hours ------------------------------------------------------------
def test_parse_hours_multiple_categories():
    assert pac.parse_hours("跨域整合 :  1.33小時、科技應用 :  1.33小時") == [
        {"category": "跨域整合", "hours": 1.33},
        {"category": "科技應用", "hours": 1.33},
    ]


def test_parse_hours_ignores_note_in_brackets():
    assert pac.parse_hours("學習護照 : 2小時（※需全程參與）") == [{"category": "學習護照", "hours": 2.0}]


@pytest.mark.parametrize("value", [None, "", "本場次不提供時數"])
def test_parse_hours_none(value):
    assert pac.parse_hours(value) == []


# ---- count_pair -------------------------------------------------------------
def test_count_pair_fullwidth_colon():
    assert pac.count_pair("報名人數", "報名人數：10 / 200 備取人數：0 / 0") == (10, 200)


def test_count_pair_halfwidth_colon_no_spaces():
    assert pac.count_pair("備取人數", "報名人數:1/2 備取人數:3/4") == (3, 4)


def test_count_pair_missing():
    assert pac.count_pair("報名人數", "沒有人數資訊") == (None, None)


# ---- registration_status -----------------------------------------------------
@pytest.mark.parametrize(
    "status, registered, capacity, expected",
    [
        ("已截止", 5, 10, "已截止"),
        ("未開放報名", 0, 10, "尚未開始"),
        ("額滿", 10, 10, "已額滿"),
        ("報名中", 10, 10, "已額滿"),  # 按鈕還是報名，但人數已滿
        ("報名中", 3, 10, "開放報名"),
        ("報名中", 3, None, "開放報名"),
        (None, 3, 10, None),
    ],
)
def test_registration_status(status, registered, capacity, expected):
    assert pac.registration_status(status, registered, capacity) == expected


# ---- registration_method（用假 HTML 走一次內頁解析）---------------------------
def _detail_html(button: str = "", session_desc: str = "", activity_desc: str = "") -> str:
    return f"""
    <div class="container"><dl class="row">
      <dt>活動名稱</dt><dd>測試活動</dd>
      <dt>活動內容</dt><dd></dd><dd class="col-sm-12">{activity_desc}</dd>
    </dl>
    <div id="myTabContent"><div class="tab-pane" id="event115A00001">
      <dl class="row"><h3>第一場</h3>
        <dt>場次時間</dt><dd>2026-12-01 10:00 ~ 2026-12-01 12:00</dd>
        <dd class="col-sm-12">{session_desc}</dd>
      </dl>
      {button} 報名人數：1 / 10
    </div></div></div>
    """


def _record(monkeypatch, html: str) -> dict:
    class FakeResponse:
        text = html

    monkeypatch.setattr(pac, "fetch", lambda session, url: FakeResponse())
    detail = pac.scrape_detail(None, "https://example.test/activity/1")[0]
    item = {"activity_id": "1", "title": "測試活動", "list_status": "報名中",
            "views": 0, "sdgs_tag": [], "images": [], "url": "https://example.test/activity/1"}
    return pac.build_record(item, detail)


FORM = "https://forms.gle/abc123"


def test_portal_link_button_with_form_in_content_is_portal(monkeypatch):
    # 2417 的情況：有 Portal 報名按鈕（<a class="btn">），內文也有校外表單 → 學生走 Portal
    rec = _record(monkeypatch, _detail_html(
        button='<a href="/activity/5673/edit" type="button" class="btn btn-primary">報名</a>',
        activity_desc=f"校外人士請填表單 {FORM}"))
    assert rec["registration_method"] == "portal"
    assert rec["registration_url"] is None
    assert rec["session_id"] == "115A00001"


def test_closed_button_is_still_portal(monkeypatch):
    rec = _record(monkeypatch, _detail_html(button="<button disabled>已截止</button>"))
    assert rec["registration_method"] == "portal"
    assert rec["registration_status"] == "已截止"


def test_no_button_with_form_is_external(monkeypatch):
    rec = _record(monkeypatch, _detail_html(activity_desc=f"請填表單 {FORM}"))
    assert rec["registration_method"] == "external"
    assert rec["registration_url"] == FORM


def test_session_form_link_preferred_over_activity(monkeypatch):
    session_form = "https://forms.gle/session999"
    rec = _record(monkeypatch, _detail_html(session_desc=f"本場表單 {session_form}",
                                            activity_desc=f"總表單 {FORM}"))
    assert rec["registration_url"] == session_form


def test_no_button_no_form_is_none(monkeypatch):
    rec = _record(monkeypatch, _detail_html(activity_desc="免報名，直接到場"))
    assert rec["registration_method"] == "none"
    assert rec["registration_url"] is None
